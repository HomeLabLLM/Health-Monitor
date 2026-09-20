"""`hl-traf serve` -- run the monitoring server.

Two ways to look at old data, deliberately distinct:

  --replay PATH   start with no hardware access at all.  Nothing is
                  polled, nothing is recorded.  This is for reviewing an
                  archive copied to a machine with no Gaudi in it.

  the UI switcher on a normal server.  Per client, so one person can
                  study Tuesday while another watches live, and recording
                  carries on regardless.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiohttp import web

from .engine import Settings, build as build_engine
from .server.app import Server
from .server.profiles import ProfileStore
from .store import reader
from .store.writer import rotate

log = logging.getLogger("hl-traf.serve")

LIVE_DB_NAME = "samples.db"
PROFILE_DB_NAME = "profiles.db"


def data_paths(data_dir: str) -> tuple[str, str]:
    os.makedirs(data_dir, exist_ok=True)
    return (os.path.join(data_dir, LIVE_DB_NAME),
            os.path.join(data_dir, PROFILE_DB_NAME))


async def _serve(host: str, port: int, data_dir: str, vllm: list[str],
                 replay: str | None, nic: bool = False,
                 nic_interval: float = 0.5) -> None:
    live_db, profile_db = data_paths(data_dir)
    store = ProfileStore(profile_db)
    await store.ensure_default()

    engine = None
    hlml = None
    poller = None
    wiring = None
    if replay:
        if not os.path.exists(replay):
            raise SystemExit(f"replay database not found: {replay}")
        log.warning("REPLAY MODE - no hardware will be read, nothing recorded")
        data_dir = os.path.dirname(os.path.abspath(replay)) or data_dir
        live_db = None
    else:
        from .hlml import Hlml, HlmlError
        try:
            hlml = Hlml()
        except HlmlError as exc:
            raise SystemExit(f"cannot open libhlml: {exc}")
        settings = Settings.from_dict(store.settings())
        engine = build_engine(hlml, vllm, live_db, settings)
        await engine.start()
        log.info("engine: %d series catalogued, %d recorded, interval %.1fs",
                 len(engine.catalog), len(engine.recorded_keys()),
                 settings.record_interval)
        if nic:
            # The fabric poller shares this Hlml handle, so the NIC
            # counters are still read by exactly one process.
            from .poller import PollerEngine
            from .topology import Wiring, wiring_from_qual
            poller = PollerEngine(hlml, interval=nic_interval)
            await poller.start()
            wiring = Wiring.load()
            if wiring is None:
                try:
                    wiring = wiring_from_qual(hlml)
                except (OSError, ValueError) as exc:
                    log.info("no NIC topology (%s); run 'hl-traf discover'", exc)
            log.info("nic fabric poller started (%s backend, %d ports)",
                     poller.backend, len(poller.state))

    server = Server(engine, store, data_dir, live_db, replay=bool(replay),
                    nic=poller, wiring=wiring)
    runner = web.AppRunner(server.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("listening on http://%s:%d/  (data: %s)", host, port, data_dir)
    if replay:
        log.info("serving archive %s", replay)

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
        if poller is not None:
            await poller.stop()
        if engine is not None:
            await engine.stop()
        if hlml is not None:
            hlml.shutdown()
        store.close()
        await runner.cleanup()


def serve(host: str, port: int, data_dir: str, vllm: list[str],
          replay: str | None = None, nic: bool = False,
          nic_interval: float = 0.5) -> None:
    asyncio.run(_serve(host, port, data_dir, vllm, replay, nic, nic_interval))


def reset(data_dir: str) -> str:
    """Rotate the live samples database and start a fresh one.

    Nothing is deleted: the rotated file keeps every sample and shows up
    in the UI's database switcher.  Removing archives is a command-line
    job on purpose, since they are large and unrecoverable.
    """
    live_db, _ = data_paths(data_dir)
    if not os.path.exists(live_db):
        return ""
    moved = rotate(live_db)
    return moved


def describe(data_dir: str) -> list[str]:
    """Human-readable listing of the databases, for `serve-list`."""
    live_db, _ = data_paths(data_dir)
    out = []
    for i in reader.list_databases(data_dir, live_db):
        if i.error:
            out.append(f"{i.name:32s} UNREADABLE  {i.error}")
            continue
        import datetime as dt
        fmt = (lambda t: dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
               if t else "-")
        mib = i.bytes / 1024**2
        tag = " (live)" if i.live else ""
        out.append(f"{i.name:32s} {mib:9.1f} MiB  {i.series:4d} series  "
                   f"{fmt(i.start)} -> {fmt(i.end)}{tag}")
    return out
