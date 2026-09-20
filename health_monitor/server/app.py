"""HTTP server: JSON API for both UIs, plus the web page itself.

The TUI is a client of this API exactly like the browser is, so the
hardware is read once no matter how many people are watching.

Database selection is per *client*, not server state: the source list
arrives with each query.  That is what lets one person study Tuesday's
archive while another watches live, and it is why recording never stops
just because somebody is looking at history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

from aiohttp import web

from ..store import reader
from .profiles import ProfileStore, StaleWrite

log = logging.getLogger("hl-traf.server")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "web")

# A viewer that cannot keep up is dropped rather than allowed to back the
# sweep loop up behind it.
PUSH_QUEUE_DEPTH = 64


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(
        o, allow_nan=False, default=str))


class Server:
    def __init__(self, engine, store: ProfileStore, data_dir: str,
                 live_db: str | None, replay: bool = False,
                 nic=None, wiring=None) -> None:
        self.engine = engine
        self.nic = nic          # PollerEngine, or None when --nic is off
        self.wiring = wiring
        self.store = store
        self.data_dir = data_dir
        self.live_db = live_db
        self.replay = replay
        self.app = web.Application()
        self._sse_listeners: list[asyncio.Queue] = []
        self._routes()

    # ------------------------------------------------------------------ #
    def _routes(self) -> None:
        a = self.app
        a.router.add_get("/api/catalog", self.catalog)
        a.router.add_get("/api/status", self.status)
        a.router.add_get("/api/databases", self.databases)
        a.router.add_post("/api/query", self.query)
        a.router.add_get("/api/live", self.live)
        a.router.add_get("/api/stream", self.stream)
        a.router.add_post("/api/subscribe", self.subscribe)
        a.router.add_get("/api/nic", self.nic_state)

        a.router.add_get("/api/profiles", self.profiles_list)
        a.router.add_get("/api/profiles/{name}", self.profile_get)
        a.router.add_post("/api/profiles/{name}", self.profile_save)
        a.router.add_delete("/api/profiles/{name}", self.profile_delete)

        a.router.add_get("/api/settings", self.settings_get)
        a.router.add_post("/api/settings", self.settings_save)

        a.router.add_get("/", self.index)
        if os.path.isdir(WEB_DIR):
            a.router.add_static("/static/", WEB_DIR, show_index=False)

    # ------------------------------------------------------------------ #
    # metadata
    # ------------------------------------------------------------------ #
    async def catalog(self, request: web.Request) -> web.Response:
        eng = self.engine
        series = []
        for s in (eng.catalog.values() if eng else []):
            series.append({
                "key": s.key, "label": s.label, "group": s.group,
                "unit": s.unit.value, "kind": s.kind.value, "dev": s.dev,
                "core": s.core, "note": s.note,
                "peak": s.peak_key, "crit": s.crit_key,
            })
        groups = [{"name": g.name, "label": g.label, "keys": g.keys,
                   "unit": g.unit.value if g.unit else None, "core": g.core}
                  for g in (eng.groups if eng else [])]
        return _json({
            "series": series,
            "groups": groups,
            "statics": eng.statics() if eng else {},
            "replay": self.replay,
        })

    async def status(self, request: web.Request) -> web.Response:
        base = {"replay": self.replay, "now": time.time(),
                "live_db": self.live_db}
        if self.engine is None:
            base["engine"] = None
            base["note"] = "replay mode: no hardware is being read"
        else:
            base["engine"] = self.engine.status()
        return _json(base)

    async def databases(self, request: web.Request) -> web.Response:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, reader.list_databases, self.data_dir, self.live_db)
        return _json([{
            "path": i.path, "name": i.name, "bytes": i.bytes,
            "start": i.start, "end": i.end, "live": i.live,
            "series": i.series, "error": i.error,
        } for i in infos])

    # ------------------------------------------------------------------ #
    # data
    # ------------------------------------------------------------------ #
    async def query(self, request: web.Request) -> web.Response:
        """Read series over one or more sources.

        Body: {sources: [{dbs: [path], shift: 0, label: ""}],
               keys: [...], t0, t1, max_points, agg}
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "bad JSON"}, 400)

        keys = [k for k in body.get("keys", []) if isinstance(k, str)]
        if not keys:
            return _json({"error": "no keys"}, 400)
        try:
            t0 = float(body["t0"])
            t1 = float(body["t1"])
        except (KeyError, TypeError, ValueError):
            return _json({"error": "t0 and t1 are required"}, 400)
        if t1 <= t0:
            return _json({"error": "t1 must be after t0"}, 400)

        allowed = self._allowed_dbs()
        sources = []
        for spec in body.get("sources") or [{"dbs": [self.live_db]}]:
            paths = [p for p in spec.get("dbs", []) if p in allowed]
            if not paths:
                continue
            sources.append(reader.Source(
                dbs=paths, shift=float(spec.get("shift", 0.0)),
                label=str(spec.get("label", ""))))
        if not sources:
            return _json({"error": "no readable databases selected"}, 400)

        max_points = max(1, min(20000, int(body.get("max_points", 2000))))
        agg = body.get("agg", "avg")
        interval = (self.engine.settings.record_interval
                    if self.engine else 5.0)

        loop = asyncio.get_running_loop()
        out = []
        for src in sources:
            data = await loop.run_in_executor(
                None, lambda s=src: reader.query(
                    s, keys, t0, t1, max_points=max_points, agg=agg,
                    sample_interval=interval))
            out.append({"label": src.label, "shift": src.shift, "data": data})
        return _json({"sources": out, "t0": t0, "t1": t1})

    def _allowed_dbs(self) -> set[str]:
        """Only databases in the data directory may be queried, so a
        crafted request cannot read arbitrary files off the box."""
        infos = reader.list_databases(self.data_dir, self.live_db)
        allowed = {i.path for i in infos}
        if self.live_db:
            allowed.add(self.live_db)
        return allowed

    async def live(self, request: web.Request) -> web.Response:
        """In-memory recent history, so a freshly opened graph fills at
        once instead of waiting for the next flush to reach SQLite."""
        if self.engine is None:
            return _json({"error": "replay mode has no live data"}, 409)
        keys = [k for k in request.query.get("keys", "").split(",") if k]
        since = float(request.query.get("since", 0) or 0)
        return _json({"data": self.engine.live_series(keys, since),
                      "now": time.time()})

    async def subscribe(self, request: web.Request) -> web.Response:
        """Declare what this client is displaying.

        The engine reads the union of recorded series and everything any
        client has subscribed to, so an unwatched sensor costs nothing.
        """
        if self.engine is None:
            return _json({"ok": True, "note": "replay mode"})
        body = await request.json()
        client = str(body.get("client") or uuid.uuid4())
        self.engine.subscribe(client, body.get("keys", []))
        return _json({"ok": True, "client": client,
                      "active": self.engine.status()["active_series"]})

    async def nic_state(self, request: web.Request) -> web.Response:
        """Per-port fabric rates for the matrix and table views.

        Served from the server's own poller so the TUI does not open its
        own device handles -- two readers of the firmware mailbox cost
        everyone latency.
        """
        if self.nic is None:
            return _json({"enabled": False,
                          "note": "start the server with --nic to poll the "
                                  "NIC fabric"}, 200)
        ports = []
        for (gpu, port), st in self.nic.state.items():
            ports.append({
                "gpu": gpu, "port": port, "external": st.external,
                "up": getattr(st, "link_up", None),
                "rx": getattr(st, "rx_smooth", 0.0),
                "tx": getattr(st, "tx_smooth", 0.0),
                "stale": getattr(st, "stale", False),
                "torn": getattr(st, "torn_reads", 0),
            })
        gpus = {}
        for idx, gs in self.nic.gpu_state.items():
            gpus[idx] = {"util": gs.util, "mem_pct": gs.mem_pct,
                         "pwr_pct": gs.pwr_pct, "pwr_mw": gs.pwr_used_mw,
                         "pcie_tx": gs.pcie_tx_smooth,
                         "pcie_rx": gs.pcie_rx_smooth}
        links = []
        if self.wiring is not None:
            # Wiring.links is a list of Link records, not a mapping.
            for lk in getattr(self.wiring, "links", []):
                links.append({"gpu_a": lk.gpu_a, "port_a": lk.port_a,
                              "gpu_b": lk.gpu_b, "port_b": lk.port_b})
        return _json({
            "enabled": True,
            "backend": self.nic.backend,
            "sweep_secs": round(self.nic.last_sweep_secs, 3),
            "sweeps": self.nic.sweep_count,
            "ports": ports, "gpus": gpus, "links": links,
        })

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Server-sent events: live samples and profile-change notices."""
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=PUSH_QUEUE_DEPTH)
        if self.engine is not None:
            self.engine.add_listener(queue)
        self._sse_listeners.append(queue)
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")   # keep proxies honest
                    continue
                await resp.write(
                    f"data: {json.dumps(msg, allow_nan=False)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if self.engine is not None:
                self.engine.drop_listener(queue)
            if queue in self._sse_listeners:
                self._sse_listeners.remove(queue)
        return resp

    def _broadcast(self, payload: dict) -> None:
        """Push a non-sample event (profile saved, settings changed) to
        everyone, so a second editor sees the change as it happens."""
        for q in list(self._sse_listeners):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ #
    # profiles
    # ------------------------------------------------------------------ #
    async def profiles_list(self, request: web.Request) -> web.Response:
        return _json(self.store.names())

    async def profile_get(self, request: web.Request) -> web.Response:
        p = self.store.get(request.match_info["name"])
        if p is None:
            return _json({"error": "no such profile"}, 404)
        return _json(p.as_dict())

    async def profile_save(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await request.json()
        version = body.get("version")
        try:
            p = await self.store.save(
                name, body.get("config") or {},
                version=None if version is None else int(version),
                who=body.get("who") or request.remote or "")
        except StaleWrite as exc:
            # The UI renders this as "<name> was updated, can't change".
            return _json({"error": "stale", "message": str(exc),
                          "name": exc.name, "current_version": exc.have}, 409)
        self._broadcast({"type": "profile", "name": p.name,
                         "version": p.version, "by": p.updated_by})
        return _json(p.as_dict())

    async def profile_delete(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        ok = await self.store.delete(name)
        if ok:
            self._broadcast({"type": "profile_deleted", "name": name})
        return _json({"deleted": ok})

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    async def settings_get(self, request: web.Request) -> web.Response:
        live = self.engine.settings.as_dict() if self.engine else {}
        return _json({"settings": live, "stored": self.store.settings()})

    async def settings_save(self, request: web.Request) -> web.Response:
        if self.engine is None:
            return _json({"error": "replay mode has no engine"}, 409)
        body = await request.json()
        from ..engine import Settings
        new = Settings.from_dict({**self.engine.settings.as_dict(),
                                  **(body or {})})
        self.engine.settings = new
        self.engine.recorder.min_free = new.min_free_bytes
        await self.store.save_settings(new.as_dict())
        self._broadcast({"type": "settings", "settings": new.as_dict()})
        log.info("settings updated: %s", new.as_dict())
        return _json({"settings": new.as_dict(),
                      "recorded_series": len(self.engine.recorded_keys())})

    # ------------------------------------------------------------------ #
    async def index(self, request: web.Request) -> web.Response:
        path = os.path.join(WEB_DIR, "index.html")
        if not os.path.exists(path):
            return web.Response(text="web UI not installed", status=404)
        return web.FileResponse(path)
