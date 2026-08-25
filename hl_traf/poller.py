"""Async parallel polling engine.

Fast path: NIC counters are read from the driver's IB sysfs
(/sys/class/infiniband/hbl_<n>/ports/<p+1>/hw_counters/<name>). Each file
read is a ~100 ms firmware query, but the firmware serves them
concurrently, so all 192 ports (8 GPUs x 24) are read in one parallel
wave (~0.2 s) instead of 24 serial queries per GPU (~2.7 s).

Fallback: when the sysfs tree is absent, per-port hlml_nic_get_statistics
calls are used (serial per GPU, parallel across GPUs).

Rates come from cumulative-counter deltas; failures mark ports stale
instead of crashing the sweep.
"""

from __future__ import annotations

import asyncio
import ctypes
import fcntl
import logging
import os
import struct
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .hlml import GpuDevice, Hlml

log = logging.getLogger("hl-traf")

SYSFS_IB_ROOT = "/sys/class/infiniband"

# Counters used for rate computation (cumulative since boot).
RX_OCTETS = "rx_Octets"
TX_OCTETS = "tx_Octets"
RX_PKTS = "rx_Pkts"
TX_PKTS = "tx_Pkts"
RATE_COUNTERS = (RX_OCTETS, TX_OCTETS, RX_PKTS, TX_PKTS)
FAST_COUNTERS = (RX_OCTETS, TX_OCTETS)  # minimal set: rate path reads 2 files/port

# Counter-name pattern considered a NIC error/health signal.
import re

# The NIC firmware occasionally serves a counter read that is offset by an
# exact multiple of 2**32 units (observed on this HLS-2H box: ±13 x 2**32
# bytes = ±55.83 GB on rx/tx_Octets, ~10-15% of reads, on idle ports, both
# backends, sequential and parallel reads alike). Deltas between torn and
# clean reads are therefore exact multiples of this step.
COUNTER_TORN_STEP = 2**32

_ERROR_RE = re.compile(
    r"(err|drop|fragment|jabber|fault|discard|bad_format|out_of_range|out_of_sequence|duplicate_psn)",
    re.IGNORECASE,
)
_ERROR_EXCLUDE_RE = re.compile(r"(corrected|link_restores|toggles)", re.IGNORECASE)


def is_error_counter(name: str) -> bool:
    return bool(_ERROR_RE.search(name)) and not _ERROR_EXCLUDE_RE.search(name)


@dataclass
class GpuHwState:
    """Device-level utilization/memory/PCIe, sampled every sweep."""

    util: int | None = None          # AIP utilization %
    mem_total: int = 0
    mem_used: int | None = None
    pwr_used_mw: int | None = None   # instantaneous power draw (mW)
    pwr_limit_mw: int = 0            # configured power cap (mW)
    pcie_tx_bps: float = 0.0         # instantaneous bytes/s (driver sample)
    pcie_rx_bps: float = 0.0
    pcie_tx_smooth: float = 0.0      # EMA-smoothed, for display/delta color
    pcie_rx_smooth: float = 0.0
    util_spark: deque[float] = field(default_factory=deque)
    mem_spark: deque[float] = field(default_factory=deque)   # used %
    pwr_spark: deque[float] = field(default_factory=deque)   # draw % of cap

    @property
    def mem_pct(self) -> float | None:
        if self.mem_used is None or self.mem_total <= 0:
            return None
        # Guard against transient driver reads during device reset/re-init,
        # where `total` can briefly report only the reserved region (≈ used)
        # instead of full HBM, yielding a bogus ~100%+ reading.
        if self.mem_used > self.mem_total:
            return None
        return 100.0 * self.mem_used / self.mem_total

    @property
    def pwr_pct(self) -> float | None:
        if self.pwr_used_mw is None or self.pwr_limit_mw <= 0:
            return None
        return 100.0 * self.pwr_used_mw / self.pwr_limit_mw


@dataclass
class PortState:
    gpu: int
    port: int
    external: bool
    link_up: bool | None = None
    # Instantaneous (last-window) rates, bits/s and packets/s.
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_pps: float = 0.0
    tx_pps: float = 0.0
    # Smoothed rates for display (EWMA).
    rx_smooth: float = 0.0
    tx_smooth: float = 0.0
    # Sparkline history of smoothed rx+tx (bits/s), one sample per sweep.
    spark: deque[float] = field(default_factory=deque)
    # Corrupt counter reads dropped by the plausibility screen.
    torn_reads: int = 0
    # Cumulative error counters (sum of watched names).
    errors_total: int = 0
    error_breakdown: dict[str, int] = field(default_factory=dict)
    had_errors_delta: bool = False
    # Health / freshness.
    stale: bool = False
    last_error: str | None = None
    last_update: float = 0.0
    # Raw cumulative counters of the last successful read.
    _prev: dict[str, int] | None = field(default=None, repr=False)
    _prev_ts: float | None = field(default=None, repr=False)

    @property
    def total_bps(self) -> float:
        return self.rx_smooth + self.tx_smooth


class SysfsCounters:
    """Parallel counter reader over the IB hw_counters sysfs tree."""

    def __init__(self, hlml: Hlml):
        # Map hl-smi device index -> hbl_N dir by matching PCI addresses.
        self.base: dict[int, str] = {}
        pci_to_dev = {dev.pci_addr: dev.index for dev in hlml.devices}
        if not os.path.isdir(SYSFS_IB_ROOT):
            raise FileNotFoundError(SYSFS_IB_ROOT)
        for entry in sorted(os.listdir(SYSFS_IB_ROOT)):
            if not entry.startswith("hbl_"):
                continue
            try:
                pci = os.path.basename(os.readlink(f"{SYSFS_IB_ROOT}/{entry}/device"))
            except OSError:
                continue
            if pci in pci_to_dev:
                self.base[pci_to_dev[pci]] = f"{SYSFS_IB_ROOT}/{entry}"
        self.pool = ThreadPoolExecutor(max_workers=256, thread_name_prefix="sysfs-poll")
        self._error_names: list[str] | None = None

    def covers(self, gpu_index: int) -> bool:
        return gpu_index in self.base

    def discover_error_names(self, dev: GpuDevice) -> list[str]:
        """One full hlml stats read to learn which error counters exist."""
        if self._error_names is None:
            port = dev.ports[0].port
            names = dev.stats(port).keys()
            self._error_names = [n for n in names if is_error_counter(n)]
        return self._error_names

    def read_port(self, gpu_index: int, port: int, names: list[str]) -> dict[str, int]:
        base = self.base[gpu_index]
        ib_port = port + 1
        out: dict[str, int] = {}
        for name in names:
            try:
                with open(f"{base}/ports/{ib_port}/hw_counters/{name}") as fh:
                    out[name] = int(fh.read().strip())
            except (OSError, ValueError):
                continue
        return out

    def sweep(
        self,
        engine: "PollerEngine",
        full_gpus: set[int],
    ) -> dict[tuple[int, int], dict]:
        """Read every polled port in one parallel wave.

        Ports on GPUs in `full_gpus` get all rate counters + error
        counters; the rest get only rx/tx octets (2 files), which keeps
        the firmware query pipeline saturated without stalling the sweep.
        """
        jobs = []
        for (gpu, port), _st in engine.state.items():
            base = self.base.get(gpu)
            if base is None:
                continue
            if gpu in full_gpus:
                names = list(RATE_COUNTERS)
                if self._error_names:
                    names += self._error_names
            else:
                names = list(FAST_COUNTERS)
            jobs.append((gpu, port, names))

        results: dict[tuple[int, int], dict] = {}

        def one(job):
            gpu, port, names = job
            try:
                counters = self.read_port(gpu, port, names)
                return (gpu, port), {"ts": time.time(), "counters": counters}
            except Exception as exc:
                return (gpu, port), {"ts": time.time(), "stats_err": str(exc)}

        for key, res in self.pool.map(one, jobs):
            results[key] = res
        return results

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False)


class PcieReader:
    """Parallel PCIe throughput reader via HL_INFO_PCI_COUNTERS ioctls.

    The driver returns instantaneous rx/tx bytes/s. Keeping the control
    fds open and issuing all ioctls concurrently costs ~30 ms for the
    whole system (vs ~200 ms serial); per-call open/close is the slow
    part, which is also why libhlml's wrapper is slow.
    """

    _ARGS_FMT = "<QIII4x"  # struct hl_info_args
    _IOCTL_NR = (3 << 30) | (struct.calcsize(_ARGS_FMT) << 16) | (ord("D") << 8) | 0x40
    _OP_PCI_COUNTERS = 12

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("rx_throughput", ctypes.c_uint64),
            ("tx_throughput", ctypes.c_uint64),
            ("replay_cnt", ctypes.c_uint32),
        ]

    def __init__(self, hlml: Hlml):
        self.fds: dict[int, int] = {}
        for dev in hlml.devices:
            try:
                minor = dev.minor_number()
                self.fds[dev.index] = os.open(f"/dev/accel/accel_controlD{minor}", os.O_RDWR)
            except (OSError, Exception) as exc:
                log.warning("pcie counters unavailable for GPU %d: %s", dev.index, exc)
        if not self.fds:
            raise RuntimeError("no control devices could be opened")
        self.pool = ThreadPoolExecutor(max_workers=len(self.fds), thread_name_prefix="pcie-poll")

    def sweep(self) -> dict[int, tuple[float, float]]:
        def one(item):
            gpu, fd = item
            buf = self._Counters()
            try:
                fcntl.ioctl(
                    fd,
                    self._IOCTL_NR,
                    struct.pack(
                        self._ARGS_FMT,
                        ctypes.addressof(buf),
                        ctypes.sizeof(buf),
                        self._OP_PCI_COUNTERS,
                        0,
                    ),
                )
            except OSError:
                return gpu, None
            return gpu, (float(buf.tx_throughput), float(buf.rx_throughput))

        out: dict[int, tuple[float, float]] = {}
        for gpu, val in self.pool.map(one, self.fds.items()):
            if val is not None:
                out[gpu] = val
        return out

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False)
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()


class PollerEngine:
    """Polls every GPU's NIC ports in parallel; publishes PortState for the UI."""

    def __init__(
        self,
        hlml: Hlml,
        interval: float = 0.3,
        history: int = 60,
        ema_alpha: float = 0.4,
        ports: str = "all",  # all | internal | external
        errors_every: int = 40,
        line_rate_gbps: float = 100.0,
    ):
        self.hlml = hlml
        self.interval = max(interval, 0.2)
        self.ema_alpha = ema_alpha
        self.history = history
        self.ports_filter = ports
        self.errors_every = max(1, errors_every)
        self._stop = asyncio.Event()
        self.paused = False
        self._task: asyncio.Task | None = None
        self._executor: ThreadPoolExecutor | None = None
        self.state: dict[tuple[int, int], PortState] = {}
        self.gpu_state: dict[int, GpuHwState] = {d.index: GpuHwState() for d in hlml.devices}
        self.sweep_count = 0
        self.last_sweep_secs = 0.0
        self.backend = "hlml"
        # Physical caps for one window of counter delta, used to screen out
        # corrupt firmware reads (see _screen_counters). Octets get margin
        # for preamble/IFG counted at MAC level; the packet cap assumes
        # minimum-size frames.
        self._max_octets_per_s = line_rate_gbps * 1e9 / 8.0 * 1.5
        self._max_pkts_per_s = line_rate_gbps * 1e9 / 64.0 * 1.5

        for dev in hlml.devices:
            for p in dev.ports:
                if ports == "internal" and p.external:
                    continue
                if ports == "external" and not p.external:
                    continue
                self.state[(dev.index, p.port)] = PortState(
                    gpu=dev.index, port=p.port, external=p.external
                )

        try:
            self.pcie = PcieReader(hlml)
        except Exception as exc:
            log.warning("pcie counters disabled: %s", exc)
            self.pcie = None

        try:
            self.sysfs = SysfsCounters(hlml)
            if all(self.sysfs.covers(d.index) for d in hlml.devices):
                self.backend = "sysfs"
                self.sysfs.discover_error_names(hlml.devices[0])
            else:
                self.sysfs.shutdown()
                self.sysfs = None
        except Exception as exc:
            log.warning("sysfs counters unavailable (%s); using hlml fallback", exc)
            self.sysfs = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(4, thread_name_prefix="hlml-poll")
        loop.set_default_executor(self._executor)
        self._task = asyncio.create_task(self._run_sweeps(), name="poll-sweeps")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.sysfs:
            self.sysfs.shutdown()
        if self.pcie:
            self.pcie.shutdown()
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None

    # ------------------------------------------------------------------ #
    # sweep loop
    # ------------------------------------------------------------------ #
    async def _run_sweeps(self) -> None:
        while not self._stop.is_set():
            if self.paused:
                await asyncio.sleep(0.25)
                continue
            t0 = time.monotonic()
            n = self.sweep_count
            try:
                results, hw = await asyncio.to_thread(self._sweep_all, n)
            except Exception as exc:
                log.error("sweep failed: %s", exc)
                results = None
            if results is not None:
                for (gpu, port), entry in results.items():
                    self._apply_port(gpu, port, entry)
                self._apply_hw(hw)
                self.sweep_count = n + 1
                self.last_sweep_secs = time.monotonic() - t0
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, self.interval - elapsed))

    def _sweep_all(self, n: int) -> tuple[dict[tuple[int, int], dict], dict[int, dict]]:
        """Blocking: run in executor thread. One parallel wave over all ports."""
        ngpus = len(self.hlml.devices)
        # Error/rate-full counters rotate across GPUs: one GPU per sweep,
        # plus a whole-system full sweep every errors_every sweeps.
        full_gpus = {n % ngpus}
        if n % self.errors_every == 0:
            full_gpus = {d.index for d in self.hlml.devices}
        results: dict[tuple[int, int], dict] = {}

        # Link states: cheap ioctls, serial is fine (~ms for all ports).
        links: dict[tuple[int, int], bool] = {}
        for dev in self.hlml.devices:
            for p in dev.ports:
                if (dev.index, p.port) not in self.state:
                    continue
                try:
                    links[(dev.index, p.port)] = dev.link_up(p.port)
                except Exception:
                    pass

        if self.sysfs is not None:
            counters = self.sysfs.sweep(self, full_gpus=full_gpus)
        else:
            counters = {}

        # hlml fallback for GPUs not covered by sysfs (or no sysfs at all).
        fallback_devs = [
            d for d in self.hlml.devices
            if self.sysfs is None or not self.sysfs.covers(d.index)
        ]
        if fallback_devs:
            with ThreadPoolExecutor(max(len(fallback_devs), 1)) as ex:
                for dev, res in zip(
                    fallback_devs, ex.map(self._sweep_gpu_hlml, fallback_devs)
                ):
                    for port, entry in res.items():
                        counters[(dev.index, port)] = entry

        for key, entry in counters.items():
            if key in links:
                entry["link"] = links[key]
            results[key] = entry

        # Device-level utilization / HBM usage: cheap ioctls.
        hw: dict[int, dict] = {}
        for dev in self.hlml.devices:
            entry_hw: dict = {}
            try:
                entry_hw["util"], _memctl = dev.utilization()
            except Exception:
                pass
            try:
                total, used, _free = dev.memory_info()
                entry_hw["mem_total"], entry_hw["mem_used"] = total, used
            except Exception:
                pass
            try:
                entry_hw["pwr_used_mw"], entry_hw["pwr_limit_mw"] = dev.power_info()
            except Exception:
                pass
            hw[dev.index] = entry_hw

        # PCIe throughput: one parallel ioctl wave (~30 ms system-wide).
        if self.pcie is not None:
            try:
                for gpu, (tx, rx) in self.pcie.sweep().items():
                    hw.setdefault(gpu, {})["pcie"] = (tx, rx)
            except Exception as exc:
                log.warning("pcie sweep failed: %s", exc)
        return results, hw

    def _apply_hw(self, hw: dict[int, dict]) -> None:
        for gpu, entry in hw.items():
            gs = self.gpu_state.get(gpu)
            if gs is None:
                continue
            if "util" in entry:
                gs.util = entry["util"]
                gs.util_spark.append(float(entry["util"]))
            if "mem_total" in entry:
                new_total = entry["mem_total"]
                new_used = entry.get("mem_used")
                # Reject transient reads from a resetting device: total collapsing
                # to <50% of the last good value (driver reports only the
                # reserved region during re-init) or used > total.
                if gs.mem_total > 0 and 0 < new_total < gs.mem_total // 2:
                    log.debug("GPU%d mem_total transient: %d -> %d, skipping",
                              gpu, gs.mem_total, new_total)
                elif new_used is not None and new_used > new_total:
                    log.debug("GPU%d mem_used>total (%d > %d), skipping",
                              gpu, new_used, new_total)
                else:
                    gs.mem_total = new_total
                    gs.mem_used = new_used
                    if gs.mem_pct is not None:
                        gs.mem_spark.append(gs.mem_pct)
            if "pwr_used_mw" in entry:
                gs.pwr_used_mw = entry["pwr_used_mw"]
                gs.pwr_limit_mw = entry["pwr_limit_mw"]
                if gs.pwr_pct is not None:
                    gs.pwr_spark.append(gs.pwr_pct)
            if "pcie" in entry:
                gs.pcie_tx_bps, gs.pcie_rx_bps = entry["pcie"]
                alpha = self.ema_alpha
                if gs.pcie_tx_smooth == 0.0 and gs.pcie_rx_smooth == 0.0:
                    gs.pcie_tx_smooth, gs.pcie_rx_smooth = entry["pcie"]
                else:
                    gs.pcie_tx_smooth += alpha * (gs.pcie_tx_bps - gs.pcie_tx_smooth)
                    gs.pcie_rx_smooth += alpha * (gs.pcie_rx_bps - gs.pcie_rx_smooth)
            while len(gs.util_spark) > self.history:
                gs.util_spark.popleft()
            while len(gs.mem_spark) > self.history:
                gs.mem_spark.popleft()
            while len(gs.pwr_spark) > self.history:
                gs.pwr_spark.popleft()

    def _sweep_gpu_hlml(self, dev: GpuDevice) -> dict[int, dict]:
        """Blocking hlml fallback: serial per port on one GPU."""
        out: dict[int, dict] = {}
        for p in dev.ports:
            if (dev.index, p.port) not in self.state:
                continue
            entry: dict = {"ts": time.time()}
            try:
                entry["counters"] = dev.stats(p.port)
            except Exception as exc:
                entry["stats_err"] = str(exc)
            out[p.port] = entry
        return out

    # ------------------------------------------------------------------ #
    # rate computation
    # ------------------------------------------------------------------ #
    def _apply_port(self, gpu: int, port: int, entry: dict) -> None:
        st = self.state.get((gpu, port))
        if st is None:
            return
        st.last_update = entry["ts"]

        if "link" in entry:
            st.link_up = entry["link"]

        if "stats_err" in entry:
            st.stale = True
            st.last_error = entry["stats_err"]
            log.warning("GPU %d port %d stats failed: %s", gpu, port, entry["stats_err"])
            return

        counters = entry.get("counters", {})
        if not counters:
            return
        st.stale = False
        st.last_error = None

        prev, prev_ts = st._prev, st._prev_ts
        st._prev_ts = entry["ts"]
        dt = entry["ts"] - prev_ts if prev_ts is not None else None
        if prev is not None and dt is not None and dt > 0:
            counters = self._screen_counters(counters, prev, dt, st)
        # Merge into stored counters: partial reads (rate-only sweeps) must
        # not clobber error counters read on the slower cadence.
        if prev is not None:
            merged = dict(prev)
            merged.update(counters)
            st._prev = merged
        else:
            st._prev = dict(counters)
            prev = None

        if prev is None or prev_ts is None:
            return
        if dt is None or dt <= 0:
            return

        def rate(name: str) -> float:
            a, b = prev.get(name), counters.get(name)
            if a is None or b is None:
                return 0.0
            d = b - a
            if d < 0:  # counter reset (fw reset/reboot): re-baseline
                return 0.0
            return d / dt

        rx_bps = rate(RX_OCTETS) * 8.0
        tx_bps = rate(TX_OCTETS) * 8.0
        rx_pps = rate(RX_PKTS)
        tx_pps = rate(TX_PKTS)

        st.rx_bps, st.tx_bps = rx_bps, tx_bps
        st.rx_pps, st.tx_pps = rx_pps, tx_pps
        alpha = self.ema_alpha
        if st.rx_smooth == 0.0 and st.tx_smooth == 0.0:
            st.rx_smooth, st.tx_smooth = rx_bps, tx_bps
        else:
            st.rx_smooth += alpha * (rx_bps - st.rx_smooth)
            st.tx_smooth += alpha * (tx_bps - st.tx_smooth)

        st.spark.append(st.rx_smooth + st.tx_smooth)
        while len(st.spark) > self.history:
            st.spark.popleft()

        # Error counters: cumulative totals + delta flag.
        breakdown = {
            n: v for n, v in counters.items() if is_error_counter(n) and v > 0
        }
        if breakdown:
            total = sum(breakdown.values())
            st.had_errors_delta = total > st.errors_total
            st.errors_total = total
            st.error_breakdown = breakdown

    def _screen_counters(
        self,
        counters: dict[str, int],
        prev: dict[str, int],
        dt: float,
        st: PortState,
    ) -> dict[str, int]:
        """Drop rate counters whose window delta is physically impossible.

        The firmware counter-read defect (see COUNTER_TORN_STEP) turns one
        bad read into a naive delta of ~55 GB/window, which at sweep pace
        surfaces as tens of TB/s of phantom fabric traffic. Real traffic on
        one port can never exceed line rate, so:

        - a positive delta above the line-rate cap is dropped;
        - a negative delta is treated as a genuine counter reset (re-
          baselined to 0 by rate()) unless it is an exact multiple of
          2**32, i.e. the torn-read signature.

        Dropped counters keep their previous baseline, so the next clean
        read resumes without emitting a spike; the window simply reports 0
        for them. May return `counters` unchanged or a filtered copy.
        """
        caps = (
            (RX_OCTETS, self._max_octets_per_s, "GB/s"),
            (TX_OCTETS, self._max_octets_per_s, "GB/s"),
            (RX_PKTS, self._max_pkts_per_s, "Mpps"),
            (TX_PKTS, self._max_pkts_per_s, "Mpps"),
        )
        out = counters
        for name, cap_per_s, unit in caps:
            a, b = prev.get(name), counters.get(name)
            if a is None or b is None:
                continue
            d = b - a
            if 0 <= d <= cap_per_s * dt:
                continue  # idle or plausible line-rate traffic
            if d < 0 and (-d) % COUNTER_TORN_STEP != 0:
                continue  # genuine reset; rate() clamps and re-baselines
            if out is counters:
                out = dict(counters)
            out.pop(name, None)
            st.torn_reads += 1
            log.debug(
                "GPU %d port %d %s: implausible delta %+d over %.2fs "
                "(cap %.1f %s%s) — read dropped",
                st.gpu, st.port, name, d, dt,
                cap_per_s / 1e9 if unit == "GB/s" else cap_per_s / 1e6, unit,
                ", 2^32 multiple" if (-d if d < 0 else d) % COUNTER_TORN_STEP == 0 else "",
            )
        return out

    # ------------------------------------------------------------------ #
    # accessors for UI / discovery
    # ------------------------------------------------------------------ #
    def ports_for_gpu(self, gpu: int) -> list[PortState]:
        return sorted(
            (st for (g, _), st in self.state.items() if g == gpu),
            key=lambda s: s.port,
        )

    def gpu_count(self) -> int:
        return len(self.hlml.devices)
