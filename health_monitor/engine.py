"""The sweep engine: backends in, ref-keyed samples out.

Only one process on a box reads the hardware -- firmware mailboxes are
serialised and a second reader costs everyone latency (measured on a
Gaudi2: 236 ms for 22 temperatures alone, 517 ms each with two readers).
Which series get read each sweep is the union of what the settings say
to record and what any viewer upstream is currently displaying, so
nobody pays for a sensor nobody is looking at.

Output of a sweep: ``(ts, {ref: value})`` with refs of the form
``<monitor>/<gpu_id>/<key>``, plus identity events.  The monitor process
hands those to the outbox and the uplink; nothing here knows about
storage or the network.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .backends import load_backends
from .backends.base import Backend, DeviceInfo
from .identity import Event, Registry
from .metrics import vllm
from .metrics.catalog import Group, Kind, Series, build_groups, ref

log = logging.getLogger("health-monitor.engine")

LIVE_WINDOW = 900.0
HOST = "host"


@dataclass
class Settings:
    record_interval: float = 5.0
    record_exotic: bool = False
    record_nic_rates: bool = False
    record_nic_errors: bool = False

    def as_dict(self) -> dict:
        return {"record_interval": self.record_interval, "record_exotic": self.record_exotic,
                "record_nic_rates": self.record_nic_rates,
                "record_nic_errors": self.record_nic_errors}

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, type(getattr(s, k))(v))
        s.record_interval = max(0.5, min(3600.0, s.record_interval))
        return s


@dataclass
class Device:
    """One (backend, DeviceInfo) with its gpu_id and local catalog."""

    backend: Backend
    info: DeviceInfo
    gpu_id: str
    catalog: dict[str, Series]
    labels: dict[str, str]
    prefix: str = ""
    statics: dict[str, float | None] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.gpu_id


class Engine:
    def __init__(self, monitor_id: str, backends: list[Backend], registry: Registry,
                 settings: Settings, vllm_urls: list[str] | None = None) -> None:
        self.monitor_id = monitor_id
        self.backends = backends
        self.registry = registry
        self.settings = settings
        self.devices: dict[str, Device] = {}          # gpu_id -> Device
        self.host_catalog: dict[str, Series] = {}
        self.scrapers = {vllm.tag_for(u): vllm.Scraper(u) for u in (vllm_urls or [])}
        for u in (vllm_urls or []):
            for s in vllm.series_for(u):
                self.host_catalog[s.key] = s
        self.host_prefix = ref(monitor_id, HOST, "")
        from .metrics.derive import Deriver
        self.deriver = Deriver()

        self._subs: dict[str, set[str]] = {}          # client -> refs
        self._live: dict[str, deque] = {}
        self._latest: dict[str, float | None] = {}
        self._events: list[Event] = []
        self._listeners: list = []
        self._last_sweep_ms = 0.0
        self._sweeps = 0
        self._task: asyncio.Task | None = None
        self.on_sample = None                          # callable(ts, values)
        self.on_events = None                          # callable(list[Event])
        self.discover()

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    def discover(self) -> list[Event]:
        """(Re)enumerate devices, resolve identities, build catalogs."""
        found: list[tuple[Backend, DeviceInfo]] = []
        for b in self.backends:
            try:
                found += [(b, d) for d in b.devices()]
            except Exception as exc:                  # noqa: BLE001
                log.warning("backend %s devices() failed: %s", b.vendor, exc)
        infos = [d for _b, d in found]
        mapping, events = self.registry.resolve(infos)
        new_devices: dict[str, Device] = {}
        for i, (b, info) in enumerate(found):
            gid = mapping[i]
            if gid in self.devices and self.devices[gid].info.pci_addr == info.pci_addr:
                dev = self.devices[gid]
                dev.info = info
            else:
                cat, labels = b.catalog(info)
                dev = Device(backend=b, info=info, gpu_id=gid, catalog=cat, labels=labels,
                             prefix=ref(self.monitor_id, gid, ""))
                self._read_statics(dev)
            new_devices[gid] = dev
        self.devices = new_devices
        self._events += events
        if self.on_events and events:
            self.on_events(events)
        return events

    def _read_statics(self, dev: Device) -> None:
        statics = [s for s in dev.catalog.values() if s.kind is Kind.STATIC]
        if not statics:
            return
        try:
            got = dev.backend.read_static(dev.info, statics)
        except Exception as exc:                      # noqa: BLE001
            log.warning("%s statics failed: %s", dev.gpu_id, exc)
            got = {}
        dev.statics = {dev.prefix + k: v for k, v in got.items()}

    # ------------------------------------------------------------------ #
    # catalog as the manager / UI sees it
    # ------------------------------------------------------------------ #
    def full_catalog(self) -> dict[str, Series]:
        out: dict[str, Series] = {}
        for dev in self.devices.values():
            for k, s in dev.catalog.items():
                out[dev.prefix + k] = s
        for k, s in self.host_catalog.items():
            out[self.host_prefix + k] = s
        return out

    def groups(self) -> list[Group]:
        """Groups per device, so ADD GROUP offers 'aibox1/nv-1 · Temps'."""
        out: list[Group] = []
        for dev in self.devices.values():
            by_ref = {dev.prefix + k: s for k, s in dev.catalog.items()}
            for g in build_groups(by_ref, dev.labels):
                g.name = f"{dev.gpu_id}:{g.name}"
                g.label = f"{self.registry.cards[dev.gpu_id].name} · {g.label}"
                out.append(g)
        if self.host_catalog:
            by_ref = {self.host_prefix + k: s for k, s in self.host_catalog.items()}
            for g in build_groups(by_ref, vllm.GROUP_LABELS):
                g.name = f"host:{g.name}"
                out.append(g)
        return out

    def statics(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for dev in self.devices.values():
            out.update(dev.statics)
        return out

    def gpus(self) -> list[dict]:
        rows = []
        for gid, dev in self.devices.items():
            c = self.registry.cards[gid]
            rows.append({"gpu_id": gid, "name": c.name, "vendor": dev.info.vendor,
                         "model": dev.info.model, "serial": dev.info.serial,
                         "uuid": dev.info.uuid, "pci": dev.info.pci_addr,
                         "driver": dev.info.driver, "state": c.state, "note": c.note,
                         "series": len(dev.catalog)})
        return rows

    # ------------------------------------------------------------------ #
    # subscriptions
    # ------------------------------------------------------------------ #
    def subscribe(self, client: str, refs: list[str]) -> None:
        self._subs[client] = set(refs)

    def unsubscribe(self, client: str) -> None:
        self._subs.pop(client, None)

    def recorded_refs(self) -> set[str]:
        st = self.settings
        out: set[str] = set()
        for r, s in self.full_catalog().items():
            if s.kind is Kind.STATIC:
                continue
            if s.group.startswith("nic.rate"):
                if st.record_nic_rates:
                    out.add(r)
                continue
            if s.group.startswith("nic.err"):
                if st.record_nic_errors:
                    out.add(r)
                continue
            if s.core or st.record_exotic:
                out.add(r)
        return out

    def active_refs(self) -> set[str]:
        refs = self.recorded_refs()
        for sub in self._subs.values():
            refs |= sub
        return refs

    # ------------------------------------------------------------------ #
    # sweep
    # ------------------------------------------------------------------ #
    def _sweep(self) -> tuple[float, dict[str, float | None]]:
        t_start = time.perf_counter()
        ts = time.time()
        wanted = self.active_refs()
        values: dict[str, float | None] = {}

        for dev in self.devices.values():
            plen = len(dev.prefix)
            local = [dev.catalog[r[plen:]] for r in wanted
                     if r.startswith(dev.prefix) and r[plen:] in dev.catalog
                     and dev.catalog[r[plen:]].kind not in (Kind.DERIVED, Kind.STATIC)]
            if local:
                try:
                    got = dev.backend.read(dev.info, local)
                except Exception as exc:              # noqa: BLE001
                    log.warning("%s read failed: %s", dev.gpu_id, exc)
                    got = {}
                for k, v in got.items():
                    values[dev.prefix + k] = v
            merged = {**dev.statics, **values}
            values.update({r: v for r, v in
                           self.deriver.compute(ts, dev.prefix, dev.catalog, merged).items()
                           if r.startswith(dev.prefix)})

        scrapes: dict[str, object] = {}
        if self.scrapers:
            for tag, sc in self.scrapers.items():
                got = sc.scrape()
                if got is not None:
                    scrapes[tag] = got
            hp = self.host_prefix
            raw = [s for s in self.host_catalog.values()
                   if s.kind in (Kind.GAUGE, Kind.COUNTER) and hp + s.key in wanted]
            for tag, scrape in scrapes.items():
                mine = [s for s in raw if s.key.startswith(f"vllm.{tag}.")]
                for k, v in vllm.gauges(mine, scrape).items():
                    values[hp + k] = v
            values.update({r: v for r, v in
                           self.deriver.compute(ts, hp, self.host_catalog, values, scrapes).items()
                           if r.startswith(hp)})
            self.deriver.note_scrapes(scrapes)

        self._last_sweep_ms = (time.perf_counter() - t_start) * 1000.0
        self._sweeps += 1
        return ts, values

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        rediscover_every = 60
        while True:
            try:
                ts, values = await loop.run_in_executor(None, self._sweep)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001
                log.error("sweep failed: %s", exc, exc_info=True)
                await asyncio.sleep(self.settings.record_interval)
                continue
            self._latest = values
            self._remember(ts, values)
            if self.on_sample:
                try:
                    self.on_sample(ts, values)
                except Exception as exc:              # noqa: BLE001
                    log.error("on_sample failed: %s", exc, exc_info=True)
            self._push(ts, values)
            if self._sweeps % rediscover_every == 0:
                try:
                    await loop.run_in_executor(None, self.discover)
                except Exception as exc:              # noqa: BLE001
                    log.warning("rediscover failed: %s", exc)
            await asyncio.sleep(max(0.05, self.settings.record_interval))

    def _remember(self, ts: float, values: dict[str, float | None]) -> None:
        cutoff = ts - LIVE_WINDOW
        for r, v in values.items():
            ring = self._live.setdefault(r, deque())
            ring.append((ts, v))
            while ring and ring[0][0] < cutoff:
                ring.popleft()
        active = self.active_refs()
        for r in list(self._live):
            if r not in active and r not in values:
                del self._live[r]

    # ------------------------------------------------------------------ #
    def add_listener(self, q) -> None:
        self._listeners.append(q)

    def drop_listener(self, q) -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    def _push(self, ts: float, values: dict[str, float | None]) -> None:
        if not self._listeners:
            return
        payload = {"type": "sample", "ts": ts,
                   "values": {k: v for k, v in values.items() if v is not None}}
        for q in list(self._listeners):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def live_series(self, refs: list[str], since: float = 0.0) -> dict:
        return {r: [[ts, v] for ts, v in self._live.get(r, ()) if ts > since] for r in refs}

    def drain_events(self) -> list[Event]:
        ev, self._events = self._events, []
        return ev

    def status(self) -> dict:
        return {"sweeps": self._sweeps, "sweep_ms": round(self._last_sweep_ms, 1),
                "interval": self.settings.record_interval,
                "active_series": len(self.active_refs()),
                "recorded_series": len(self.recorded_refs()),
                "catalog_series": len(self.full_catalog()),
                "devices": len(self.devices), "viewers": len(self._subs),
                "vllm": {t: s.up for t, s in self.scrapers.items()}}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for b in self.backends:
            try:
                b.shutdown()
            except Exception:                         # noqa: BLE001
                pass


def build(monitor_id: str, cfg: dict[str, Any], registry_path: str) -> Engine:
    backends = load_backends(list(cfg.get("backends") or ["auto"]), cfg)
    if not backends:
        log.warning("no GPU backends loaded on this box")
    registry = Registry(registry_path, cfg.get("gpu_names") or {})
    settings = Settings.from_dict(cfg)
    return Engine(monitor_id, backends, registry, settings, list(cfg.get("vllm") or []))
