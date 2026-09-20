"""Readers that turn hardware and vLLM into catalog values.

Cost matters here and shapes the whole design.  Measured on this box:

    hwmon channel        ~3.6-6 ms each, serialised in the driver
    all 22 temperatures  ~80 ms
    all 63 channels      ~190-285 ms
    clk_cur_freq_mhz     ~4 ms
    PCI link/status      ~0.05 ms  (kernel-cached, effectively free)
    vLLM /metrics        ~6-11 ms  for the whole 64 KB document

Threading does *not* help the hwmon path: 8, 16, 32 and 63 workers all
land at 190-220 ms because the driver serialises firmware mailbox
access.  A second process reading concurrently doubles everyone's
latency.  So readers take an explicit key subset and the engine only ever
asks for what is being recorded or displayed.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("hl-traf.sources")


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------- #
# hwmon
# ---------------------------------------------------------------------- #
class HwmonReader:
    """Temperatures, voltages, currents and the 54V power channel."""

    def __init__(self, dev: int, hwmon_dir: str) -> None:
        self.dev = dev
        self.dir = hwmon_dir

    def read(self, series: list) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for s in series:
            raw = _read_int(os.path.join(self.dir, s.path))
            out[s.key] = None if raw is None else raw * s.scale
        return out


# ---------------------------------------------------------------------- #
# libhlml
# ---------------------------------------------------------------------- #
class HlmlReader:
    """Device-level values via the existing ctypes bindings.

    Only calls verified to work on this stack are wired up; MME/TPC/IC
    clocks, the VRM and CTEMP sensor types and PCIe link geometry all
    return HLML_ERROR_NOT_SUPPORTED here and are served from hwmon or PCI
    sysfs instead.
    """

    def __init__(self, dev_handle) -> None:
        self.dev = dev_handle
        self._unsupported: set[str] = set()

    def read(self, series: list) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        want = {s.path for s in series}
        keys = {s.path: s.key for s in series}

        def put(path: str, value):
            key = keys.get(path)
            if key is not None:
                out[key] = value

        if {"util_aip"} & want:
            try:
                aip, _mem = self.dev.utilization()
                put("util_aip", float(aip))
            except Exception as exc:                     # noqa: BLE001
                self._note("utilization", exc); put("util_aip", None)

        if {"mem_used", "mem_total"} & want:
            try:
                total, used, _free = self.dev.memory_info()
                put("mem_used", float(used)); put("mem_total", float(total))
            except Exception as exc:                     # noqa: BLE001
                self._note("memory_info", exc)
                put("mem_used", None); put("mem_total", None)

        if {"power_used", "power_limit"} & want:
            try:
                used, limit = self.dev.power_info()
                put("power_used", float(used)); put("power_limit", float(limit))
            except Exception as exc:                     # noqa: BLE001
                self._note("power_info", exc)
                put("power_used", None); put("power_limit", None)

        for path, fn in (
            ("energy", self.dev.energy),
            ("clock_soc", self.dev.clock_soc),
            ("clock_soc_max", self.dev.clock_soc_max),
            ("pcie_replay", self.dev.pcie_replay),
            ("throttle_reasons", self.dev.throttle_reasons),
            ("viol_power", self.dev.violation_power),
            ("viol_thermal", self.dev.violation_thermal),
        ):
            if path in want:
                try:
                    put(path, float(fn()))
                except Exception as exc:                 # noqa: BLE001
                    self._note(path, exc); put(path, None)
        return out

    def _note(self, what: str, exc: Exception) -> None:
        if what not in self._unsupported:
            self._unsupported.add(what)
            log.warning("hlml %s unavailable: %s", what, exc)


# ---------------------------------------------------------------------- #
# PCI sysfs (free) and AER
# ---------------------------------------------------------------------- #
_GTS = re.compile(r"([\d.]+)\s*GT/s")


class SysfsReader:
    def __init__(self, dev: int, device_dir: str) -> None:
        self.dev = dev
        self.dir = device_dir

    def read(self, series: list) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for s in series:
            path = os.path.join(self.dir, s.path)
            try:
                with open(path) as fh:
                    text = fh.read().strip()
            except OSError:
                out[s.key] = None
                continue
            m = _GTS.search(text)
            if m:
                out[s.key] = float(m.group(1))
                continue
            try:
                out[s.key] = float(text)
            except ValueError:
                out[s.key] = None
        return out


class AerReader:
    """Sum the counters in an aer_dev_* file into one number.

    The files are `Name count` per line; the total is what you want on a
    graph -- any non-zero value at all is the signal.
    """

    def __init__(self, dev: int, device_dir: str) -> None:
        self.dir = device_dir

    def read(self, series: list) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for s in series:
            try:
                with open(os.path.join(self.dir, s.path)) as fh:
                    total = 0
                    for line in fh:
                        parts = line.split()
                        if len(parts) == 2 and parts[1].isdigit():
                            total += int(parts[1])
                out[s.key] = float(total)
            except OSError:
                out[s.key] = None
        return out


# ---------------------------------------------------------------------- #
# vLLM /metrics
# ---------------------------------------------------------------------- #
_SAMPLE = re.compile(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


@dataclass
class Scrape:
    """One parsed /metrics document."""

    ts: float
    values: dict[str, float] = field(default_factory=dict)
    # metric -> list of (labels, value), for label-filtered lookups
    labelled: dict[str, list[tuple[dict[str, str], float]]] = field(
        default_factory=dict)
    # histogram name -> {le -> cumulative count}, plus _sum/_count
    buckets: dict[str, dict[float, float]] = field(default_factory=dict)

    def get(self, metric: str, labels: dict[str, str] | None = None
            ) -> float | None:
        name = f"vllm:{metric}"
        if not labels:
            return self.values.get(name)
        for got, val in self.labelled.get(name, []):
            if all(got.get(k) == v for k, v in labels.items()):
                return val
        return None


class VllmScraper:
    """Scrapes a vLLM Prometheus endpoint.

    This replaces parsing vLLM's log line entirely.  /metrics carries the
    same fields plus the latency histograms, updates at engine-step
    granularity rather than on the 10-second logger tick, and -- because
    we timestamp it at scrape time inside the same sweep that reads the
    sensors -- shares one clock with the hardware samples.  Correlating a
    throughput change against a rail spike is then exact rather than
    +/-5 s.
    """

    def __init__(self, url: str, timeout: float = 4.0) -> None:
        self.url = url.rstrip("/")
        if not self.url.endswith("/metrics"):
            self.url += "/metrics"
        self.timeout = timeout
        self.last: Scrape | None = None
        self.up = False
        self._logged_down = False

    def scrape(self) -> Scrape | None:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if not self._logged_down:
                log.warning("vllm %s unreachable: %s", self.url, exc)
                self._logged_down = True
            self.up = False
            return None
        if self._logged_down:
            log.info("vllm %s back up", self.url)
            self._logged_down = False
        self.up = True
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> Scrape:
        s = Scrape(ts=time.time())
        for line in text.splitlines():
            if not line or line[0] == "#":
                continue
            m = _SAMPLE.match(line)
            if not m:
                continue
            name, labelstr, raw = m.groups()
            try:
                val = float(raw)
            except ValueError:
                continue
            labels = dict(_LABEL.findall(labelstr)) if labelstr else {}
            if name.endswith("_bucket") and "le" in labels:
                base = name[: -len("_bucket")]
                try:
                    le = float(labels["le"])
                except ValueError:
                    le = float("inf")
                s.buckets.setdefault(base, {})[le] = val
                continue
            s.values.setdefault(name, val)
            s.labelled.setdefault(name, []).append((labels, val))
        return s


def vllm_gauges(series: list, scrape: Scrape | None
                ) -> dict[str, float | None]:
    """Plain gauge series read straight out of a scrape.

    Paths are `metric` or `metric|label=value` for the label-selected
    ones (vLLM splits waiting requests by reason that way).
    """
    out: dict[str, float | None] = {}
    for s in series:
        if scrape is None:
            out[s.key] = None
            continue
        metric, _, sel = s.path.partition("|")
        labels = dict(p.split("=", 1) for p in sel.split(",") if "=" in p)
        val = scrape.get(metric, labels or None)
        out[s.key] = None if val is None else val * s.scale
    return out

# ---------------------------------------------------------------------- #
# PCIe throughput
# ---------------------------------------------------------------------- #
class PcieShared:
    """Continuously samples PCIe throughput, so the engine records an
    interval statistic rather than a point sample.

    The driver's HL_INFO_PCI_COUNTERS gives an *instantaneous* rate, and
    it is extremely bursty: adjacent readings 5 s apart were measured at
    0 and 52,939 B/s on an otherwise idle card.  Sampling that once per
    engine sweep aliases badly -- a burst between two sweeps simply never
    happened as far as the graph is concerned.

    So a background thread polls at SAMPLE_INTERVAL and accumulates mean
    and peak per device; each engine sweep drains the accumulator.  The
    recorded mean is then a real average over the interval and the peak
    catches bursts the mean smooths away.  The ioctl is cheap (the fds
    stay open; per-call open/close is what makes libhlml's wrapper slow),
    so this costs very little.
    """

    SAMPLE_INTERVAL = 0.5

    def __init__(self, hlml, sample_interval: float | None = None) -> None:
        from ..poller import PcieReader
        self._reader = PcieReader(hlml)
        self._interval = sample_interval or self.SAMPLE_INTERVAL
        self._lock = threading.Lock()
        # dev -> [tx_sum, rx_sum, n, tx_peak, rx_peak, tx_last, rx_last]
        self._acc: dict[int, list[float]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="pcie-sampler",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                got = self._reader.sweep()
            except Exception as exc:                 # noqa: BLE001
                log.debug("pcie sweep failed: %s", exc)
                continue
            with self._lock:
                for dev, (tx, rx) in got.items():
                    a = self._acc.get(dev)
                    if a is None:
                        self._acc[dev] = [tx, rx, 1, tx, rx, tx, rx]
                    else:
                        a[0] += tx
                        a[1] += rx
                        a[2] += 1
                        a[3] = max(a[3], tx)
                        a[4] = max(a[4], rx)
                        a[5], a[6] = tx, rx

    def drain(self, dev: int) -> dict[str, float] | None:
        """Mean and peak since the last call, then reset the accumulator."""
        with self._lock:
            a = self._acc.pop(dev, None)
        if a is None:
            return None
        tx_sum, rx_sum, n, tx_peak, rx_peak, tx_last, rx_last = a
        if n <= 0:
            return None
        return {"tx": tx_sum / n, "rx": rx_sum / n,
                "tx_peak": tx_peak, "rx_peak": rx_peak,
                "tx_last": tx_last, "rx_last": rx_last, "n": n}

    def shutdown(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._reader.shutdown()


class PcieReaderAdapter:
    """Per-device view onto the shared PCIe sampler."""

    def __init__(self, shared: PcieShared, dev: int) -> None:
        self.shared = shared
        self.dev = dev
        self._last: dict[str, float] | None = None

    def read(self, series: list) -> dict[str, float | None]:
        got = self.shared.drain(self.dev)
        if got is None:
            # No samples accumulated since the last sweep (the sampler
            # only just started, or the interval is shorter than its
            # tick).  Reuse the previous reading rather than punching a
            # hole in an otherwise continuous line.
            got = self._last
        else:
            self._last = got
        out: dict[str, float | None] = {}
        for s in series:
            out[s.key] = None if got is None else got.get(s.path, 0.0) * s.scale
        return out
