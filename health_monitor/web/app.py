"""The web server: HTTPS, users, per-user profiles, and a client of the
manager.

Everything a browser or the TUI needs comes through here.  Data requests
are proxied to the manager over mutual TLS with this server's web-role
certificate; live samples arrive on one WebSocket from the manager and
fan out to browsers as server-sent events.  The manager never sees a
user -- it trusts this server's certificate, and this server decides
who may do what.

    browser/TUI --https--> web --mTLS--> manager
                           web <--ws---- manager (live samples, monitor state)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time

import aiohttp
from aiohttp import web

from .. import __version__, config, proto, tls
from . import auth
from .profiles import Profiles, StaleWrite
from .users import AuthError, Users

log = logging.getLogger("health-monitor.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PUSH_QUEUE_DEPTH = 128


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, allow_nan=False, default=str))


def _build() -> dict:
    try:
        from .. import _build
        return {"hash": _build.BUILD_HASH, "date": _build.BUILD_DATE}
    except Exception:                                # noqa: BLE001
        return {"hash": "unknown", "date": ""}


class WebApp:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.users = Users(os.path.join(self.data_dir, "users.db"))
        self.profiles = Profiles(os.path.join(self.data_dir, "profiles.db"))
        self.manager = cfg["manager"].rstrip("/")
        self._ssl = tls.client_context(cfg.certs)
        self._session: aiohttp.ClientSession | None = None
        self._listeners: list[asyncio.Queue] = []
        self._watched: dict[str, set[str]] = {}       # client id -> refs
        self._manager_ws: aiohttp.ClientWebSocketResponse | None = None
        self._monitors: list[dict] = []
        self._ws_task: asyncio.Task | None = None
        self.app = web.Application(middlewares=[auth.auth_middleware],
                                   client_max_size=64 * 1024 * 1024)
        self.app["users"] = self.users
        self.app["cfg"] = cfg
        self._routes()

    # ------------------------------------------------------------------ #
    def _routes(self) -> None:
        r = self.app.router
        # auth
        r.add_post("/api/login", auth.do_login)
        r.add_post("/api/logout", auth.do_logout)
        r.add_post("/api/setup", auth.do_setup)
        r.add_post("/api/password", auth.do_change_password)
        r.add_get("/api/me", self.api_me)
        # users (admin)
        r.add_get("/api/users", self.api_users)
        r.add_post("/api/users", self.api_users_add)
        r.add_post("/api/users/{name}/password", self.api_users_passwd)
        r.add_post("/api/users/{name}/role", self.api_users_role)
        r.add_post("/api/users/{name}/disable", self.api_users_disable)
        r.add_delete("/api/users/{name}", self.api_users_del)
        r.add_post("/api/tokens", self.api_token_new)
        # profiles (per user)
        r.add_get("/api/profiles", self.api_profiles)
        r.add_get("/api/profiles/{name}", self.api_profile_get)
        r.add_post("/api/profiles/{name}", self.api_profile_save)
        r.add_delete("/api/profiles/{name}", self.api_profile_del)
        # data (proxied to the manager)
        r.add_get("/api/status", self.api_status)
        r.add_get("/api/monitors", self.api_monitors)
        r.add_get("/api/catalog", self.api_catalog)
        r.add_post("/api/query", self.api_query)
        r.add_get("/api/databases", self.api_databases)
        r.add_get("/api/events", self.api_events)
        r.add_post("/api/subscribe", self.api_subscribe)
        r.add_get("/api/stream", self.api_stream)
        # admin-only manager operations
        r.add_post("/api/nickname", self.api_nickname)
        r.add_post("/api/gpu/rename", self.api_gpu_rename)
        r.add_post("/api/gpu/remap", self.api_gpu_remap)
        r.add_get("/api/settings", self.api_settings_get)
        r.add_post("/api/settings", self.api_settings_set)
        # pages
        r.add_get("/", self.page_index)
        r.add_get("/login", self.page_login)
        r.add_get("/setup", self.page_setup)
        r.add_get("/healthz", lambda req: web.Response(text="ok"))
        if os.path.isdir(STATIC_DIR):
            r.add_static("/static/", STATIC_DIR, show_index=False)

    # ------------------------------------------------------------------ #
    # manager client
    # ------------------------------------------------------------------ #
    async def _mgr(self, method: str, path: str, **kw):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60), connector=aiohttp.TCPConnector(ssl=self._ssl))
        try:
            async with self._session.request(method, self.manager + path, **kw) as resp:
                data = await resp.json(content_type=None)
                return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 502, {"error": f"manager unreachable: {exc}"}

    async def _proxy(self, request: web.Request, method: str, path: str, *,
                     body=None, query: bool = True) -> web.Response:
        kw = {}
        if body is not None:
            kw["json"] = body
        if query and request.query_string:
            path = f"{path}?{request.query_string}"
        status, data = await self._mgr(method, path, **kw)
        return _json(data, status)

    async def _manager_ws_loop(self) -> None:
        """Hold one WebSocket to the manager for live samples and monitor
        state; fan every frame out to the SSE listeners."""
        backoff = 2.0
        while True:
            try:
                url = self.manager.replace("https://", "wss://") + "/ws"
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(url, ssl=self._ssl, heartbeat=20,
                                            max_msg_size=64 * 1024 * 1024) as ws:
                        self._manager_ws = ws
                        backoff = 2.0
                        log.info("live link to manager up")
                        await self._send_watch()
                        async for frame in ws:
                            if frame.type != aiohttp.WSMsgType.TEXT:
                                if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                    break
                                continue
                            m = proto.decode(frame.data)
                            t = m.get("t")
                            if t == proto.HELLO_OK:
                                self._monitors = m.get("monitors") or []
                            elif t == proto.SAMPLE:
                                self._fanout({"type": "sample", "monitor": m.get("monitor"),
                                              "ts": m.get("ts"), "values": m.get("values") or {}})
                            elif t == proto.MONITOR_STATE:
                                self._fanout({"type": "monitor", **{k: v for k, v in m.items() if k != "t"}})
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001
                log.warning("live link to manager: %s", exc)
            self._manager_ws = None
            self._fanout({"type": "manager", "state": "disconnected"})
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def _send_watch(self) -> None:
        refs = set()
        for s in self._watched.values():
            refs |= s
        if self._manager_ws is not None and not self._manager_ws.closed:
            try:
                await self._manager_ws.send_str(proto.encode(proto.WATCH, refs=sorted(refs)))
            except (aiohttp.ClientError, ConnectionResetError):
                pass

    def _fanout(self, payload: dict) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ #
    # auth / users
    # ------------------------------------------------------------------ #
    async def api_me(self, request):
        return _json({"user": auth.current_user(request), "version": __version__,
                      "build": _build(), "https": bool(self.cfg["https"])})

    async def api_users(self, request):
        auth.require_admin(request)
        return _json(self.users.list())

    async def api_users_add(self, request):
        auth.require_admin(request)
        b = await request.json()
        try:
            uid = self.users.add(str(b.get("name", "")), str(b.get("password", "")),
                                 str(b.get("role", "user")))
        except AuthError as exc:
            return _json({"error": str(exc)}, 400)
        self.profiles.ensure_default(uid, "system")
        return _json({"ok": True, "id": uid})

    async def api_users_passwd(self, request):
        auth.require_admin(request)
        b = await request.json()
        try:
            self.users.set_password(request.match_info["name"], str(b.get("password", "")))
        except AuthError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": True})

    async def api_users_role(self, request):
        me = auth.require_admin(request)
        name = request.match_info["name"]
        b = await request.json()
        if name == me["name"] and b.get("role") != "admin":
            return _json({"error": "you cannot demote yourself"}, 400)
        try:
            self.users.set_role(name, str(b.get("role", "user")))
        except AuthError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": True})

    async def api_users_disable(self, request):
        me = auth.require_admin(request)
        name = request.match_info["name"]
        b = await request.json()
        if name == me["name"]:
            return _json({"error": "you cannot disable yourself"}, 400)
        try:
            self.users.set_disabled(name, bool(b.get("disabled", True)))
        except AuthError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": True})

    async def api_users_del(self, request):
        me = auth.require_admin(request)
        name = request.match_info["name"]
        if name == me["name"]:
            return _json({"error": "you cannot delete yourself"}, 400)
        try:
            uid = self.users.delete(name)
        except AuthError as exc:
            return _json({"error": str(exc)}, 404)
        n = self.profiles.delete_user(uid)
        return _json({"ok": True, "profiles_deleted": n})

    async def api_token_new(self, request):
        me = auth.current_user(request)
        b = await request.json()
        tok = self.users.new_token(me["name"], str(b.get("label", "tui"))[:40])
        return _json({"token": tok})

    # ------------------------------------------------------------------ #
    # profiles
    # ------------------------------------------------------------------ #
    async def api_profiles(self, request):
        me = auth.current_user(request)
        self.profiles.ensure_default(me["id"], me["name"])
        return _json(self.profiles.names(me["id"]))

    async def api_profile_get(self, request):
        me = auth.current_user(request)
        p = self.profiles.get(me["id"], request.match_info["name"])
        return _json(p.as_dict()) if p else _json({"error": "no such profile"}, 404)

    async def api_profile_save(self, request):
        me = auth.current_user(request)
        b = await request.json()
        v = b.get("version")
        try:
            p = self.profiles.save(me["id"], request.match_info["name"], b.get("config") or {},
                                   version=None if v is None else int(v), who=me["name"])
        except StaleWrite as exc:
            return _json({"error": "stale", "message": str(exc), "name": exc.name,
                          "current_version": exc.have}, 409)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        return _json(p.as_dict())

    async def api_profile_del(self, request):
        me = auth.current_user(request)
        return _json({"deleted": self.profiles.delete(me["id"], request.match_info["name"])})

    # ------------------------------------------------------------------ #
    # data: proxied
    # ------------------------------------------------------------------ #
    async def api_status(self, request):
        auth.current_user(request)
        status, data = await self._mgr("GET", "/api/status")
        return _json({"web": {"version": __version__, "build": _build(), "now": time.time(),
                              "manager": self.manager, "live_link": self._manager_ws is not None},
                      "manager": data if status == 200 else None,
                      "error": None if status == 200 else data.get("error")})

    async def api_monitors(self, request):
        auth.current_user(request)
        return await self._proxy(request, "GET", "/api/monitors")

    async def api_catalog(self, request):
        auth.current_user(request)
        return await self._proxy(request, "GET", "/api/catalog")

    async def api_query(self, request):
        auth.current_user(request)
        return await self._proxy(request, "POST", "/api/query", body=await request.json(),
                                 query=False)

    async def api_databases(self, request):
        auth.current_user(request)
        return await self._proxy(request, "GET", "/api/databases")

    async def api_events(self, request):
        auth.current_user(request)
        return await self._proxy(request, "GET", "/api/events")

    async def api_subscribe(self, request):
        """Each browser declares what it shows; we forward the union."""
        auth.current_user(request)
        b = await request.json()
        client = str(b.get("client") or id(request))
        self._watched[client] = set(b.get("refs") or [])
        await self._send_watch()
        refs = set()
        for s in self._watched.values():
            refs |= s
        status, data = await self._mgr("POST", "/api/subscribe", json={"refs": sorted(refs)})
        return _json({"ok": status == 200, "client": client, "watching": len(refs)})

    async def api_stream(self, request):
        auth.current_user(request)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=PUSH_QUEUE_DEPTH)
        self._listeners.append(q)
        client = request.query.get("client", "")
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                await resp.write(f"data: {json.dumps(msg, allow_nan=False)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if q in self._listeners:
                self._listeners.remove(q)
            if client and client in self._watched:
                del self._watched[client]
                await self._send_watch()
        return resp

    # ------------------------------------------------------------------ #
    # admin operations on the manager
    # ------------------------------------------------------------------ #
    async def api_nickname(self, request):
        auth.require_admin(request)
        return await self._proxy(request, "POST", "/api/nickname", body=await request.json(), query=False)

    async def api_gpu_rename(self, request):
        auth.require_admin(request)
        return await self._proxy(request, "POST", "/api/gpu/rename", body=await request.json(), query=False)

    async def api_gpu_remap(self, request):
        auth.require_admin(request)
        return await self._proxy(request, "POST", "/api/gpu/remap", body=await request.json(), query=False)

    async def api_settings_get(self, request):
        auth.current_user(request)
        return await self._proxy(request, "GET", "/api/settings")

    async def api_settings_set(self, request):
        auth.require_admin(request)
        return await self._proxy(request, "POST", "/api/settings", body=await request.json(), query=False)

    # ------------------------------------------------------------------ #
    # pages
    # ------------------------------------------------------------------ #
    async def page_index(self, request):
        return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))

    async def page_login(self, request):
        if self.users.count() == 0:
            raise web.HTTPFound("/setup")
        return web.FileResponse(os.path.join(STATIC_DIR, "login.html"))

    async def page_setup(self, request):
        if self.users.count() > 0:
            raise web.HTTPFound("/login")
        return web.FileResponse(os.path.join(STATIC_DIR, "setup.html"))

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        host, _, port = self.cfg["listen"].rpartition(":")
        ssl_ctx = tls.server_context(self.cfg.certs, require_client=False) if self.cfg["https"] else None
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host or "0.0.0.0", int(port), ssl_context=ssl_ctx).start()
        self._ws_task = asyncio.create_task(self._manager_ws_loop(), name="manager-live")
        log.info("web listening on %s://%s (manager %s, users %d)",
                 "https" if ssl_ctx else "http", self.cfg["listen"], self.manager, self.users.count())
        if self.users.count() == 0:
            log.warning("no users yet: open the site to create the admin account")
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
            if self._ws_task:
                self._ws_task.cancel()
            if self._session:
                await self._session.close()
            await runner.cleanup()
            self.users.close()
            self.profiles.close()


def main(cfg_path: str | None = None) -> None:
    cfg = config.load("web", cfg_path)
    asyncio.run(WebApp(cfg).run())
