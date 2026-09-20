"""The monitoring engine: one sweep loop, one recorder, many viewers.

Only this process reads the hardware.  That is not a stylistic choice --
the firmware mailbox is globally serialised, and a second reader costs
everyone latency.  Measured on this box: 22 temperatures take 236 ms
from one process, 517 ms each when two processes do it at once, and
535 ms when an `hl-smi -q` loop runs alongside.  So the TUI talks to the
server over JSON rather than opening its own handles.

Which series get read each sweep is the union of

    * what the engine settings say to record, and
    * what any connected client is currently displaying,

so nobody pays for the twenty ~817 mV core rails unless someone is
actually looking at them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

from .metrics import discover, sources
from .metrics.catalog import Kind, Series
from .metrics.derive import Deriver
from .store.writer import Recorder

log = logging.getLogger("hl-traf.engine")

LIVE_WINDOW = 900.0        # seconds of in-memory history per active series


@dataclass
class Settings:
    """Engine settings, editable from the web UI's settings page."""

    record_interval: float = 5.0
    record_exotic: bool = False        # core/housekeeping rails and peaks
    record_nic_rates: bool = False
    record_nic_errors: bool = False
    min_free_bytes: int = 2 * 1024**3

    def as_dict(self) -> dict:
        return {
            "record_interval": self.record_interval,
            "record_exotic": self.record_exotic,
            "record_nic_rates": self.record_nic_rates,
            "record_nic_errors": self.record_nic_errors,
            "min_free_bytes": self.min_free_bytes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, type(getattr(s, k))(v))
        s.record_interval = max(0.5, min(3600.0, s.record_interval))
        return s


class Engine:
    def __init__(self, catalog: dict[str, Series], groups: list,
                 readers: dict[tuple[str, int], object],
                 scrapers: dict[str, sources.VllmScraper],
                 recorder: Recorder, settings: Settings) -> None:
        self.catalog = catalog
        self.groups = groups
        self.readers = readers                  # (source, dev) -> reader
        self.scrapers = scrapers                # tag -> VllmScraper
        self.recorder = recorder
        self.settings = settings
        self.deriver = Deriver(catalog)

        self._subs: dict[str, set[str]] = {}    # client id -> keys
        self._live: dict[str, deque] = {}       # key -> deque[(ts, value)]
        self._latest: dict[str, float | None] = {}
        self._statics: dict[str, float | None] = {}
        self._missing_readers: set[tuple[str, int]] = set()
        self._last_sweep_ms = 0.0
        self._sweeps = 0
        self._task: asyncio.Task | None = None
        self._listeners: list = []              # asyncio.Queue for SSE push

    # ------------------------------------------------------------------ #
    # subscriptions
    # ------------------------------------------------------------------ #
    def subscribe(self, client: str, keys: list[str]) -> None:
        self._subs[client] = {k for k in keys if k in self.catalog}

    def unsubscribe(self, client: str) -> None:
        self._subs.pop(client, None)

    def recorded_keys(self) -> set[str]:
        """Series written to the database, per the engine settings."""
        st = self.settings
        out = set()
        for key, s in self.catalog.items():
            if s.kind is Kind.STATIC:
                continue
            if s.group.startswith("nic.rate"):
                if st.record_nic_rates:
                    out.add(key)
                continue
            if s.group.startswith("nic.err"):
                if st.record_nic_errors:
                    out.add(key)
                continue
            if s.core or st.record_exotic:
                out.add(key)
        return out

    def active_keys(self) -> set[str]:
        keys = self.recorded_keys()
        for sub in self._subs.values():
            keys |= sub
        return keys

    # ------------------------------------------------------------------ #
    # sweeping
    # ------------------------------------------------------------------ #
    def _read_statics(self) -> None:
        """Read the constants once: HBM size, power and clock limits, and
        every sensor's critical threshold.

        They are not recorded as time series -- writing an unchanging
        number every five seconds for a year is waste -- but they must be
        *read*, because derived series divide by them and the UI draws
        them as threshold lines.
        """
        by_source: dict[tuple[str, int], list[Series]] = {}
        for s in self.catalog.values():
            if s.kind is Kind.STATIC and s.source not in ("derived", "vllm"):
                by_source.setdefault((s.source, s.dev), []).append(s)
        for (name, dev), series in by_source.items():
            reader = self.readers.get((name, dev))
            if reader is None:
                # A catalogued source with no reader silently produces no
                # points at all -- not nulls, nothing -- which is very hard
                # to spot on a graph. Say so once.
                if (name, dev) not in self._missing_readers:
                    self._missing_readers.add((name, dev))
                    log.error("no reader for source %r (dev %d); %d series "
                              "will have no data: %s", name, dev, len(series),
                              ", ".join(sorted(s.key for s in series)[:4]))
                continue
            try:
                self._statics.update(reader.read(series))
            except Exception as exc:              # noqa: BLE001
                log.warning("static %s (dev %d) failed: %s", name, dev, exc)
        got = sum(1 for v in self._statics.values() if v is not None)
        log.info("read %d/%d static values", got, len(self._statics))

    def _sweep(self) -> tuple[float, dict[str, float | None]]:
        """One full read.  Runs in a worker thread: it is all blocking I/O."""
        if not self._statics:
            self._read_statics()
        wanted = self.active_keys()
        t_start = time.perf_counter()
        ts = time.time()
        values: dict[str, float | None] = {}

        # Group by (source, device): with eight cards a single reader per
        # source would read card 0 eight times over.
        by_source: dict[tuple[str, int], list[Series]] = {}
        vllm_series: list[Series] = []
        for key in wanted:
            s = self.catalog[key]
            if s.kind is Kind.DERIVED:
                continue
            if s.source == "vllm":
                vllm_series.append(s)
                continue
            by_source.setdefault((s.source, s.dev), []).append(s)

        for (name, dev), series in by_source.items():
            reader = self.readers.get((name, dev))
            if reader is None:
                # A catalogued source with no reader silently produces no
                # points at all -- not nulls, nothing -- which is very hard
                # to spot on a graph. Say so once.
                if (name, dev) not in self._missing_readers:
                    self._missing_readers.add((name, dev))
                    log.error("no reader for source %r (dev %d); %d series "
                              "will have no data: %s", name, dev, len(series),
                              ", ".join(sorted(s.key for s in series)[:4]))
                continue
            try:
                values.update(reader.read(series))
            except Exception as exc:              # noqa: BLE001
                log.warning("%s reader (dev %d) failed: %s", name, dev, exc)

        scrapes: dict[str, object] = {}
        for tag, scraper in self.scrapers.items():
            got = scraper.scrape()
            if got is not None:
                scrapes[tag] = got
        # Counters are read straight out of the scrape exactly like
        # gauges; only DERIVED series are computed later.  Filtering
        # to GAUGE alone silently dropped num_preemptions_total.
        raw_vllm = [s for s in vllm_series
                    if s.kind in (Kind.GAUGE, Kind.COUNTER)]
        for tag, scrape in scrapes.items():
            mine = [s for s in raw_vllm if s.key.startswith(f"vllm.{tag}.")]
            values.update(sources.vllm_gauges(mine, scrape))

        for key, val in self._statics.items():
            values.setdefault(key, val)
        values = self.deriver.compute(ts, values, scrapes)
        self.deriver.note_scrapes(scrapes)
        self._last_sweep_ms = (time.perf_counter() - t_start) * 1000.0
        self._sweeps += 1
        return ts, values

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                ts, values = await loop.run_in_executor(None, self._sweep)
            except asyncio.CancelledError:
                raise
            except Exception as exc:              # noqa: BLE001
                log.error("sweep failed: %s", exc, exc_info=True)
                await asyncio.sleep(self.settings.record_interval)
                continue

            self._latest = values
            self._remember(ts, values)
            recorded = self.recorded_keys()
            self.recorder.record(ts, {k: v for k, v in values.items()
                                      if k in recorded})
            self._push(ts, values)

            # Sweeps that overrun the interval must not pile up; pace from
            # the end of the sweep rather than the start.
            delay = self.settings.record_interval
            if self._last_sweep_ms / 1000.0 > delay:
                log.debug("sweep took %.0f ms, longer than the %.1fs interval",
                          self._last_sweep_ms, delay)
            await asyncio.sleep(max(0.05, delay))

    def _remember(self, ts: float, values: dict[str, float | None]) -> None:
        cutoff = ts - LIVE_WINDOW
        for key, val in values.items():
            ring = self._live.setdefault(key, deque())
            ring.append((ts, val))
            while ring and ring[0][0] < cutoff:
                ring.popleft()
        # Drop rings for series nobody reads any more.
        active = self.active_keys()
        for key in list(self._live):
            if key not in active and key not in values:
                del self._live[key]

    # ------------------------------------------------------------------ #
    # live push
    # ------------------------------------------------------------------ #
    def add_listener(self, queue) -> None:
        self._listeners.append(queue)

    def drop_listener(self, queue) -> None:
        if queue in self._listeners:
            self._listeners.remove(queue)

    def _push(self, ts: float, values: dict[str, float | None]) -> None:
        if not self._listeners:
            return
        payload = {"type": "sample", "ts": ts,
                   "values": {k: v for k, v in values.items() if v is not None}}
        for q in list(self._listeners):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A viewer that cannot keep up is dropped rather than
                # allowed to stall the sweep loop.
                log.debug("listener queue full; dropping frame")

    # ------------------------------------------------------------------ #
    def live_series(self, keys: list[str], since: float = 0.0) -> dict:
        out = {}
        for key in keys:
            ring = self._live.get(key)
            if not ring:
                out[key] = []
                continue
            out[key] = [[ts, v] for ts, v in ring if ts > since]
        return out

    def statics(self) -> dict[str, float | None]:
        """Constants for threshold lines and axis bounds."""
        return dict(self._statics)

    def status(self) -> dict:
        return {
            "sweeps": self._sweeps,
            "sweep_ms": round(self._last_sweep_ms, 1),
            "interval": self.settings.record_interval,
            "active_series": len(self.active_keys()),
            "recorded_series": len(self.recorded_keys()),
            "catalog_series": len(self.catalog),
            "viewers": len(self._subs),
            "vllm": {tag: sc.up for tag, sc in self.scrapers.items()},
            "recorder": self.recorder.status.as_dict(),
        }

    async def start(self) -> None:
        await self.recorder.start()
        self._task = asyncio.create_task(self._run(), name="engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.recorder.stop()
        for shared in _pcie_shared:
            shared.shutdown()
        _pcie_shared.clear()


# ---------------------------------------------------------------------- #
# construction
# ---------------------------------------------------------------------- #
_pcie_shared: list = []


def build(hlml, vllm_urls: list[str], db_path: str,
          settings: Settings) -> Engine:
    """Wire up catalog, readers and recorder for the attached hardware."""
    from . import hlml_ext
    hlml_ext.install()

    devices = [d.index for d in hlml.devices]
    catalog, groups = discover.build(devices, vllm_endpoints=vllm_urls)

    readers: dict[tuple[str, int], object] = {}
    for dev in hlml.devices:
        i = dev.index
        accel = f"/sys/class/accel/accel{i}"
        hw = discover.hwmon_dir(accel)
        if hw:
            readers[("hwmon", i)] = sources.HwmonReader(i, hw)
        else:
            log.warning("dev%d: no hwmon node; temperatures and rails "
                        "will be unavailable", i)
        readers[("hlml", i)] = sources.HlmlReader(dev)
        readers[("sysfs", i)] = sources.SysfsReader(i, f"{accel}/device")
        readers[("aer", i)] = sources.AerReader(i, f"{accel}/device")

    # PCIe throughput comes from the driver ioctl, one sweep shared by all
    # devices.  If the control devices cannot be opened the series simply
    # report no data rather than the engine failing to start.
    try:
        pcie = sources.PcieShared(hlml)
        for dev in hlml.devices:
            readers[("pcie", dev.index)] = sources.PcieReaderAdapter(
                pcie, dev.index)
        _pcie_shared.append(pcie)
    except Exception as exc:                          # noqa: BLE001
        log.warning("pcie throughput unavailable: %s", exc)

    scrapers = {discover._endpoint_tag(u): sources.VllmScraper(u)
                for u in vllm_urls}

    recorder = Recorder(db_path, catalog, min_free=settings.min_free_bytes)
    recorder.open()
    return Engine(catalog, groups, readers, scrapers, recorder, settings)
