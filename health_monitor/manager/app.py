"""The manager: one mutual-TLS listener, monitors and web servers alike.

The client certificate decides everything -- CN is the identity, OU the
role -- so there is one port to open and one CA to trust.

    monitor  --ws /ws-->   samples, events, gpus, status   -->  archive
    monitor  <--ws /ws--   ack, subscribe, settings, rename, remap
    web      --https-->    /api/* queries (registry, catalog, samples,
                           events, archives, nicknames, settings)
    web      --ws /ws-->   watch(refs)  <--  live sample push

Nothing here knows about users or passwords: that is the web server's
job, and it arrives with a web-role certificate the manager trusts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from .. import __version__, config, proto, tls
from ..metrics.catalog import Series
from ..store import reader
from ..store.writer import rotate
from .store import ManagerStore

log = logging.getLogger("health-monitor.manager")

LIVE_DB = "samples.db"


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, allow_nan=False, default=str))


@dataclass
class MonitorSession:
    id: str
    ws: web.WebSocketResponse
    version: str = ""
    proto: int = 0
    skew: float = 0.0
    connected_at: float = 0.0
    last_frame: float = 0.0
    catalog: dict[str, Series] = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)
    statics: dict = field(default_factory=dict)
    gpus: list[dict] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    engine_status: dict = field(default_factory=dict)
    outbox_status: dict = field(default_factory=dict)
    rows_received: int = 0
    backlog_at_connect: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, t: str, **body) -> bool:
        if self.ws.closed:
            return False
        async with self.send_lock:
            try:
                await self.ws.send_str(proto.encode(t, **body))
                return True
            except (ConnectionResetError, RuntimeError):
                return False


@dataclass
class WebSession:
    cn: str
    ws: web.WebSocketResponse
    watched: set[str] = field(default_factory=set)


class ManagerApp:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.live_db = os.path.join(self.data_dir, LIVE_DB)
        self.store = ManagerStore(self.live_db, min_free=config.parse_size(cfg["min_free"]))
        self.monitors: dict[str, MonitorSession] = {}
        self.webs: list[WebSession] = []
        # last known snapshot per monitor, kept across disconnects for the UI
        self.last_seen: dict[str, float] = {m["id"]: m["last_seen"] for m in self.store.monitors()}
        self.app = web.Application(client_max_size=64 * 1024 * 1024)
        self._routes()
        self._started = time.time()

    # ------------------------------------------------------------------ #
    def _routes(self) -> None:
        r = self.app.router
        r.add_get("/ws", self.ws_handler)
        r.add_get("/api/status", self.api_status)
        r.add_get("/api/monitors", self.api_monitors)
        r.add_get("/api/catalog", self.api_catalog)
        r.add_post("/api/query", self.api_query)
        r.add_get("/api/live", self.api_live)
        r.add_get("/api/databases", self.api_databases)
        r.add_get("/api/events", self.api_events)
        r.add_post("/api/subscribe", self.api_subscribe)
        r.add_post("/api/nickname", self.api_nickname)
        r.add_post("/api/gpu/rename", self.api_gpu_rename)
        r.add_post("/api/gpu/remap", self.api_gpu_remap)
        r.add_get("/api/settings", self.api_settings_get)
        r.add_post("/api/settings", self.api_settings_set)

    def _require(self, request: web.Request, role: str) -> tls.Peer:
        peer = tls.peer_of(request)
        if peer is None:
            raise web.HTTPForbidden(text="client certificate required")
        if peer.role != role:
            raise web.HTTPForbidden(text=f"certificate role {peer.role!r} may not do this")
        allowed = self.cfg["monitors"] if role == "monitor" else self.cfg["web_clients"]
        if allowed and peer.cn not in allowed:
            raise web.HTTPForbidden(text=f"{role} {peer.cn!r} is not registered on this manager")
        return peer

    # ------------------------------------------------------------------ #
    # websocket: both roles
    # ------------------------------------------------------------------ #
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        peer = tls.peer_of(request)
        if peer is None:
            raise web.HTTPForbidden(text="client certificate required")
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=20.0)
        await ws.prepare(request)
        if peer.role == "monitor":
            await self._monitor_session(peer, ws)
        elif peer.role == "web":
            await self._web_session(peer, ws)
        else:
            await ws.send_str(proto.encode(proto.REFUSE, reason=f"unknown role {peer.role!r}"))
            await ws.close()
        return ws

    # -- monitors ------------------------------------------------------- #
    async def _monitor_session(self, peer: tls.Peer, ws: web.WebSocketResponse) -> None:
        mid = peer.cn
        allowed = self.cfg["monitors"]
        if allowed and mid not in allowed:
            await ws.send_str(proto.encode(proto.REFUSE, reason=f"monitor {mid!r} not registered"))
            await ws.close()
            log.warning("refused unregistered monitor %s", mid)
            return
        if mid in self.monitors and not self.monitors[mid].ws.closed:
            await ws.send_str(proto.encode(proto.REFUSE, reason=f"monitor {mid!r} already connected"))
            await ws.close()
            log.warning("refused duplicate monitor %s", mid)
            return

        first = await ws.receive(timeout=30)
        if first.type != WSMsgType.TEXT:
            await ws.close()
            return
        hello = proto.decode(first.data)
        try:
            if hello.get("t") != proto.HELLO:
                raise proto.ProtocolError("first frame must be hello")
            pv = proto.check_proto(hello)
        except proto.ProtocolError as exc:
            await ws.send_str(proto.encode(proto.REFUSE, reason=str(exc)))
            await ws.close()
            log.warning("monitor %s refused: %s", mid, exc)
            return

        now = time.time()
        sess = MonitorSession(id=mid, ws=ws, version=str(hello.get("version", "")), proto=pv,
                              skew=now - float(hello.get("clock", now)),
                              connected_at=now, last_frame=now,
                              backlog_at_connect=int(hello.get("backlog", 0)))
        sess.catalog = {r: Series.from_dict(d) for r, d in (hello.get("catalog") or {}).items()}
        sess.groups = hello.get("groups") or []
        sess.statics = hello.get("statics") or {}
        sess.gpus = hello.get("gpus") or []
        sess.settings = hello.get("settings") or {}
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.store.sync_catalog, sess.catalog)
        await loop.run_in_executor(None, lambda: (
            self.store.upsert_monitor(mid, version=sess.version, proto=pv, skew=sess.skew),
            self.store.upsert_gpus(mid, sess.gpus),
            self.store.add_event(mid, "monitor_connect", now, None,
                                 {"version": sess.version, "skew": round(sess.skew, 3),
                                  "backlog": sess.backlog_at_connect})))
        self.monitors[mid] = sess
        if abs(sess.skew) > 5:
            log.warning("monitor %s clock skew %+.1fs", mid, sess.skew)
        log.info("monitor %s connected: v%s proto %d, %d gpus, %d series, backlog %d",
                 mid, sess.version, pv, len(sess.gpus), len(sess.catalog), sess.backlog_at_connect)
        await sess.send(proto.HELLO_OK, proto=proto.PROTO, server_time=time.time(),
                        skew=sess.skew, refs=sorted(self._wanted_refs(mid)))
        self._broadcast_state(mid)

        try:
            async for frame in ws:
                if frame.type != WSMsgType.TEXT:
                    if frame.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
                    continue
                sess.last_frame = time.time()
                m = proto.decode(frame.data)
                t = m.get("t")
                if t == proto.SAMPLES:
                    rows = m.get("rows") or []
                    try:
                        n = await loop.run_in_executor(None, self.store.write_rows, rows)
                    except Exception as exc:          # noqa: BLE001
                        log.error("monitor %s batch %s not stored: %s", mid, m.get("batch"), exc)
                        continue                      # no ack -> monitor resends later
                    sess.rows_received += n
                    await sess.send(proto.ACK, batch=m.get("batch"))
                    self._push_live(mid, rows)
                elif t == proto.EVENTS:
                    await loop.run_in_executor(None, self.store.write_events, mid, m.get("rows") or [])
                    await sess.send(proto.ACK, batch=m.get("batch"))
                elif t == proto.GPUS:
                    sess.gpus = m.get("gpus") or []
                    await loop.run_in_executor(None, self.store.upsert_gpus, mid, sess.gpus)
                    self._broadcast_state(mid)
                elif t == proto.STATUS:
                    sess.engine_status = m.get("engine") or {}
                    sess.outbox_status = m.get("outbox") or {}
                    self.store.touch_monitor(mid)
                elif t == proto.PONG:
                    pass
        finally:
            if self.monitors.get(mid) is sess:
                del self.monitors[mid]
            self.last_seen[mid] = time.time()
            self.store.add_event(mid, "monitor_disconnect", time.time(), None,
                                 {"rows": sess.rows_received})
            log.info("monitor %s disconnected after %d rows", mid, sess.rows_received)
            self._broadcast_state(mid)

    def _wanted_refs(self, mid: str) -> set[str]:
        """Union of what every web viewer is watching on this monitor."""
        prefix = mid + "/"
        return {r for w in self.webs for r in w.watched if r.startswith(prefix)}

    async def _push_subscriptions(self) -> None:
        for mid, sess in list(self.monitors.items()):
            await sess.send(proto.SUBSCRIBE, refs=sorted(self._wanted_refs(mid)))

    # -- web live push -------------------------------------------------- #
    async def _web_session(self, peer: tls.Peer, ws: web.WebSocketResponse) -> None:
        allowed = self.cfg["web_clients"]
        if allowed and peer.cn not in allowed:
            await ws.send_str(proto.encode(proto.REFUSE, reason=f"web client {peer.cn!r} not registered"))
            await ws.close()
            return
        sess = WebSession(cn=peer.cn, ws=ws)
        self.webs.append(sess)
        log.info("web client %s connected", peer.cn)
        try:
            await ws.send_str(proto.encode(proto.HELLO_OK, proto=proto.PROTO,
                                           server_time=time.time(), monitors=self._monitor_list()))
            async for frame in ws:
                if frame.type != WSMsgType.TEXT:
                    if frame.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
                    continue
                m = proto.decode(frame.data)
                if m.get("t") == proto.WATCH:
                    sess.watched = set(m.get("refs") or [])
                    await self._push_subscriptions()
        finally:
            if sess in self.webs:
                self.webs.remove(sess)
            await self._push_subscriptions()
            log.info("web client %s disconnected", peer.cn)

    def _push_live(self, mid: str, rows: list[list]) -> None:
        if not self.webs:
            return
        # Only the newest timestamp is "live"; backlog rows are history.
        newest = max((r[1] for r in rows), default=0)
        if time.time() - newest > 30:
            return
        for w in list(self.webs):
            vals = {r[0]: r[2] for r in rows if r[1] == newest and r[0] in w.watched and r[2] is not None}
            if vals:
                asyncio.get_running_loop().create_task(
                    self._ws_send(w.ws, proto.SAMPLE, monitor=mid, ts=newest, values=vals))

    def _broadcast_state(self, mid: str) -> None:
        state = self._monitor_info(mid)
        for w in list(self.webs):
            asyncio.get_running_loop().create_task(
                self._ws_send(w.ws, proto.MONITOR_STATE, **state))

    @staticmethod
    async def _ws_send(ws: web.WebSocketResponse, t: str, **body) -> None:
        try:
            if not ws.closed:
                await ws.send_str(proto.encode(t, **body))
        except (ConnectionResetError, RuntimeError):
            pass

    # ------------------------------------------------------------------ #
    # web REST API
    # ------------------------------------------------------------------ #
    def _monitor_info(self, mid: str) -> dict:
        sess = self.monitors.get(mid)
        stored = next((m for m in self.store.monitors() if m["id"] == mid), {})
        nick = (self.cfg["nicknames"] or {}).get(mid) or stored.get("nickname") or ""
        stale = self.cfg["stale_after"]
        now = time.time()
        if sess is None:
            state = "offline"
        elif now - sess.last_frame > stale:
            state = "stale"
        else:
            state = "online"
        info = {"monitor": mid, "nickname": nick, "state": state,
                "last_seen": sess.last_frame if sess else self.last_seen.get(mid, stored.get("last_seen", 0)),
                "version": sess.version if sess else stored.get("version", ""),
                "skew": sess.skew if sess else stored.get("skew", 0.0),
                "gpus": sess.gpus if sess else self.store.gpus(mid)}
        if sess:
            info.update({"connected_at": sess.connected_at, "rows_received": sess.rows_received,
                         "backlog_at_connect": sess.backlog_at_connect,
                         "engine": sess.engine_status, "outbox": sess.outbox_status,
                         "settings": sess.settings})
        return info

    def _monitor_list(self) -> list[dict]:
        ids = set(self.monitors) | {m["id"] for m in self.store.monitors()}
        return [self._monitor_info(m) for m in sorted(ids)]

    async def api_status(self, request):
        self._require(request, "web")
        return _json({"version": __version__, "proto": proto.PROTO, "now": time.time(),
                      "started": self._started, "live_db": self.live_db,
                      "store": self.store.status.as_dict(),
                      "monitors_online": len(self.monitors), "web_clients": len(self.webs)})

    async def api_monitors(self, request):
        self._require(request, "web")
        return _json(self._monitor_list())

    async def api_catalog(self, request):
        self._require(request, "web")
        mid = request.query.get("monitor")
        series, groups, statics = [], [], {}
        for m, sess in self.monitors.items():
            if mid and m != mid:
                continue
            series += [dict(s.as_dict(), ref=r, monitor=m) for r, s in sess.catalog.items()]
            groups += [dict(g, monitor=m) for g in sess.groups]
            statics.update(sess.statics)
        # Offline monitors: whatever the archive knows, so history stays graphable.
        known = {s["ref"] for s in series}
        for row in self.store.catalog_rows(mid):
            if row["ref"] not in known:
                series.append({"ref": row["ref"], "key": row["key"], "monitor": row["monitor"],
                               "gpu_id": row["gpu_id"], "label": row["label"],
                               "group": row["group_name"], "unit": row["unit"],
                               "kind": row["kind"], "core": True, "offline": True})
        return _json({"series": series, "groups": groups, "statics": statics})

    async def api_query(self, request):
        self._require(request, "web")
        body = await request.json()
        refs = [k for k in body.get("refs") or body.get("keys") or [] if isinstance(k, str)]
        if not refs:
            return _json({"error": "no refs"}, 400)
        try:
            t0, t1 = float(body["t0"]), float(body["t1"])
        except (KeyError, TypeError, ValueError):
            return _json({"error": "t0 and t1 are required"}, 400)
        if t1 <= t0:
            return _json({"error": "t1 must be after t0"}, 400)
        allowed = {i.path for i in reader.list_databases(self.data_dir, self.live_db)} | {self.live_db}
        sources = []
        for spec in body.get("sources") or [{"dbs": [self.live_db]}]:
            paths = [p for p in spec.get("dbs", []) if p in allowed]
            if paths:
                sources.append(reader.Source(dbs=paths, shift=float(spec.get("shift", 0.0)),
                                             label=str(spec.get("label", ""))))
        if not sources:
            return _json({"error": "no readable databases selected"}, 400)
        max_points = max(1, min(20000, int(body.get("max_points", 2000))))
        interval = float(body.get("interval", 5.0))
        loop = asyncio.get_running_loop()
        out = []
        for src in sources:
            data = await loop.run_in_executor(None, lambda s=src: reader.query(
                s, refs, t0, t1, max_points=max_points, agg=body.get("agg", "avg"),
                sample_interval=interval))
            out.append({"label": src.label, "shift": src.shift, "data": data})
        return _json({"sources": out, "t0": t0, "t1": t1})

    async def api_live(self, request):
        """Most recent values straight from the sessions (no db round trip)."""
        self._require(request, "web")
        refs = [r for r in request.query.get("refs", "").split(",") if r]
        return _json({"now": time.time(), "monitors": {m: s.engine_status for m, s in self.monitors.items()}})

    async def api_databases(self, request):
        self._require(request, "web")
        infos = await asyncio.get_running_loop().run_in_executor(
            None, reader.list_databases, self.data_dir, self.live_db)
        return _json([{"path": i.path, "name": i.name, "bytes": i.bytes, "start": i.start,
                       "end": i.end, "live": i.live, "series": i.series, "error": i.error}
                      for i in infos])

    async def api_events(self, request):
        self._require(request, "web")
        mid = request.query.get("monitor") or None
        t0 = float(request.query.get("t0", 0))
        t1 = float(request.query.get("t1", time.time()))
        return _json(self.store.events(mid, t0, t1))

    async def api_subscribe(self, request):
        """The web server forwards the union of its viewers' refs; we relay
        per monitor so an unwatched sensor is never read."""
        peer = self._require(request, "web")
        body = await request.json()
        refs = set(body.get("refs") or [])
        # REST subscribers are tracked as a pseudo web session keyed by CN.
        sess = next((w for w in self.webs if w.cn == f"rest:{peer.cn}"), None)
        if sess is None:
            sess = WebSession(cn=f"rest:{peer.cn}", ws=None)  # type: ignore[arg-type]
            self.webs.append(sess)
        sess.watched = refs
        await self._push_subscriptions()
        return _json({"ok": True, "monitors": {m: len(self._wanted_refs(m)) for m in self.monitors}})

    async def api_nickname(self, request):
        self._require(request, "web")
        body = await request.json()
        mid, nick = str(body.get("monitor", "")), str(body.get("nickname", "")).strip()
        if not mid:
            return _json({"error": "monitor required"}, 400)
        nicks = dict(self.cfg["nicknames"] or {})
        nicks[mid] = nick
        self.cfg.values["nicknames"] = nicks
        self.cfg.save()
        self.store.set_nickname(mid, nick)
        self._broadcast_state(mid)
        return _json({"ok": True, "monitor": mid, "nickname": nick})

    async def api_gpu_rename(self, request):
        self._require(request, "web")
        body = await request.json()
        sess = self.monitors.get(str(body.get("monitor", "")))
        if sess is None:
            return _json({"error": "monitor is not connected"}, 409)
        ok = await sess.send(proto.RENAME, gpu_id=body.get("gpu_id"), name=body.get("name"))
        return _json({"ok": ok})

    async def api_gpu_remap(self, request):
        self._require(request, "web")
        body = await request.json()
        sess = self.monitors.get(str(body.get("monitor", "")))
        if sess is None:
            return _json({"error": "monitor is not connected"}, 409)
        ok = await sess.send(proto.REMAP, gpu_id=body.get("gpu_id"), pci=body.get("pci"))
        return _json({"ok": ok})

    async def api_settings_get(self, request):
        self._require(request, "web")
        return _json({m: s.settings for m, s in self.monitors.items()})

    async def api_settings_set(self, request):
        self._require(request, "web")
        body = await request.json()
        sess = self.monitors.get(str(body.get("monitor", "")))
        if sess is None:
            return _json({"error": "monitor is not connected"}, 409)
        settings = body.get("settings") or {}
        ok = await sess.send(proto.SETTINGS, settings=settings)
        if ok:
            sess.settings.update(settings)
        return _json({"ok": ok, "settings": sess.settings})

    # ------------------------------------------------------------------ #
    async def _stale_watch(self) -> None:
        while True:
            await asyncio.sleep(10)
            for mid, sess in list(self.monitors.items()):
                if time.time() - sess.last_frame > self.cfg["stale_after"] * 3 and not sess.ws.closed:
                    log.warning("monitor %s silent for %.0fs; closing", mid,
                                time.time() - sess.last_frame)
                    await sess.ws.close()

    async def run(self) -> None:
        host, _, port = self.cfg["listen"].rpartition(":")
        ssl_ctx = tls.server_context(self.cfg.certs, require_client=True)
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host or "0.0.0.0", int(port), ssl_context=ssl_ctx).start()
        watcher = asyncio.create_task(self._stale_watch())
        log.info("manager listening on %s (mTLS), data %s, monitors allowed: %s",
                 self.cfg["listen"], self.data_dir, ", ".join(self.cfg["monitors"]) or "any")
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
            watcher.cancel()
            for sess in list(self.monitors.values()):
                await sess.ws.close()
            await runner.cleanup()
            self.store.close()


def main(cfg_path: str | None = None) -> None:
    cfg = config.load("manager", cfg_path)
    asyncio.run(ManagerApp(cfg).run())


def reset(cfg_path: str | None = None) -> str:
    cfg = config.load("manager", cfg_path)
    live = os.path.join(cfg.data_dir, LIVE_DB)
    return rotate(live) if os.path.exists(live) else ""
