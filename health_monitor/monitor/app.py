"""The monitor process: sweep the box's GPUs, spool locally, forward
upstream, answer a local status API.

    engine  --on_sample-->  outbox  --batches-->  uplink  --ack-->  outbox
                                                    ^
    manager --subscribe/settings/rename/remap-------+

The local HTTP listener (127.0.0.1:5677 by default) exists so a TUI on
the box itself can see status and the last fifteen minutes without
touching the hardware.  There is no history here: an acked row is gone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time

from aiohttp import web

from .. import __version__, config, proto
from ..engine import Engine, Settings, build as build_engine
from ..store.outbox import Outbox
from .uplink import Uplink

log = logging.getLogger("health-monitor.monitor")


class MonitorApp:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.id: str = cfg["id"]
        self.data_dir = cfg.data_dir
        self.engine: Engine = build_engine(self.id, cfg.values,
                                          os.path.join(config.config_dir(), "gpus.json"))
        catalog = self.engine.full_catalog()
        self.outbox = Outbox(
            os.path.join(self.data_dir, "outbox.db"), catalog,
            max_bytes=config.parse_size(cfg["outbox_max"], of_path=self.data_dir),
            min_free=config.parse_size(cfg["min_free"]))
        self.uplink = Uplink(cfg["manager"], cfg.certs, self.outbox, hello=self.hello,
                             on_message=self.on_manager_message,
                             catchup_batch=int(cfg["catchup_batch"]),
                             catchup_rate=int(cfg["catchup_rate"]))
        self.engine.on_sample = self._on_sample
        self.engine.on_events = self._on_events
        self._flush_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = time.time()
        self._sweeps_since_status = 0

    # ------------------------------------------------------------------ #
    # engine -> outbox
    # ------------------------------------------------------------------ #
    def _on_sample(self, ts: float, values: dict) -> None:
        self.outbox.record(ts, values)
        self._sweeps_since_status += 1

    def _on_events(self, events) -> None:
        for e in events:
            self.outbox.record_event(self.id, e.kind, e.ts, None, e.detail)
        # Identity changed: the manager needs the new card list.  This runs
        # on the engine's executor thread, so hand the send to the loop.
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                lambda: loop.create_task(self.uplink.send(proto.GPUS, gpus=self.engine.gpus())))

    async def _flusher(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(2.0)
            try:
                await loop.run_in_executor(None, self.outbox.flush)
                self.uplink.poke()
                if self._sweeps_since_status >= 6:
                    self._sweeps_since_status = 0
                    await self.uplink.send(proto.STATUS, engine=self.engine.status(),
                                           outbox=self.outbox.status.as_dict())
            except Exception as exc:                  # noqa: BLE001
                log.error("flush failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------ #
    # manager protocol
    # ------------------------------------------------------------------ #
    def hello(self) -> dict:
        cat = self.engine.full_catalog()
        return {
            "proto": proto.PROTO, "role": "monitor", "id": self.id, "version": __version__,
            "clock": time.time(), "started": self._started,
            "gpus": self.engine.gpus(),
            "catalog": {r: s.as_dict() for r, s in cat.items()},
            "groups": [g.as_dict() for g in self.engine.groups()],
            "statics": self.engine.statics(),
            "settings": self.engine.settings.as_dict(),
            "backlog": self.outbox.backlog(),
        }

    async def on_manager_message(self, m: dict) -> None:
        t = m.get("t")
        if t in (proto.HELLO_OK, proto.SETTINGS) and m.get("settings"):
            new = Settings.from_dict({**self.engine.settings.as_dict(), **m["settings"]})
            if new.as_dict() != self.engine.settings.as_dict():
                self.engine.settings = new
                self.cfg.values.update(new.as_dict())
                self.cfg.save()
                log.info("settings from manager: %s", new.as_dict())
        if t in (proto.HELLO_OK, proto.SUBSCRIBE) and "refs" in m:
            self.engine.subscribe("manager", list(m.get("refs") or []))
        if t == proto.RENAME:
            try:
                name = self.engine.registry.rename(m["gpu_id"], m["name"])
                names = dict(self.cfg.values.get("gpu_names") or {})
                names[m["gpu_id"]] = m["name"]
                self.cfg.values["gpu_names"] = names
                self.cfg.save()
                log.info("gpu %s renamed to %s", m["gpu_id"], name)
                await self.uplink.send(proto.GPUS, gpus=self.engine.gpus())
            except KeyError:
                log.warning("rename: unknown gpu %s", m.get("gpu_id"))
        if t == proto.REMAP:
            try:
                self.engine.registry.remap(m["gpu_id"], m["pci"])
                self.engine.discover()
                await self.uplink.send(proto.GPUS, gpus=self.engine.gpus())
            except KeyError:
                log.warning("remap: unknown gpu %s", m.get("gpu_id"))

    # ------------------------------------------------------------------ #
    # local status API
    # ------------------------------------------------------------------ #
    def routes(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/status", self._status)
        app.router.add_get("/api/catalog", self._catalog)
        app.router.add_get("/api/live", self._live)
        app.router.add_get("/api/gpus", self._gpus)
        return app

    def status(self) -> dict:
        return {"id": self.id, "version": __version__, "now": time.time(),
                "started": self._started, "engine": self.engine.status(),
                "outbox": self.outbox.status.as_dict(), "uplink": self.uplink.status(),
                "gpus": self.engine.gpus()}

    async def _status(self, request):
        return web.json_response(self.status())

    async def _catalog(self, request):
        cat = self.engine.full_catalog()
        return web.json_response({"series": [dict(s.as_dict(), ref=r) for r, s in cat.items()],
                                  "groups": [g.as_dict() for g in self.engine.groups()],
                                  "statics": self.engine.statics()})

    async def _live(self, request):
        refs = [r for r in request.query.get("refs", "").split(",") if r]
        since = float(request.query.get("since", 0) or 0)
        return web.json_response({"data": self.engine.live_series(refs, since),
                                  "now": time.time()})

    async def _gpus(self, request):
        return web.json_response(self.engine.gpus())

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        listen = self.cfg["local_listen"]
        host, _, port = listen.rpartition(":")
        runner = web.AppRunner(self.routes(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host or "127.0.0.1", int(port)).start()
        await self.engine.start()
        await self.uplink.start()
        self._flush_task = asyncio.create_task(self._flusher(), name="flusher")
        log.info("monitor %s: %d devices, %d series, local api on %s, manager %s",
                 self.id, len(self.engine.devices), len(self.engine.full_catalog()),
                 listen, self.cfg["manager"])

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        try:
            await stop.wait()
        finally:
            log.info("shutting down")
            if self._flush_task:
                self._flush_task.cancel()
            await self.uplink.stop()
            await self.engine.stop()
            self.outbox.close()
            await runner.cleanup()


def main(cfg_path: str | None = None) -> None:
    cfg = config.load("monitor", cfg_path)
    asyncio.run(MonitorApp(cfg).run())
