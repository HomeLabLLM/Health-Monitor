"""Authentication for the web server.

Every request is resolved to a user via the session cookie (browser) or
a bearer API token (TUI) before any handler runs.  With no users at all
the site shows the first-run page that creates the admin; with users but
no session it shows the login page.  ``/login`` is throttled per address
so a LAN box cannot be brute-forced in an afternoon.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from .users import AuthError, Users

log = logging.getLogger("health-monitor.web.auth")

COOKIE = "hm_session"
# The pages *and* the endpoints they post to; everything else needs a user.
PUBLIC = {"/login", "/setup", "/api/login", "/api/setup", "/static", "/healthz"}


def _public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PUBLIC)


def _client_addr(request: web.Request) -> str:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else "?"


@web.middleware
async def auth_middleware(request: web.Request, handler):
    users: Users = request.app["users"]
    request["user"] = None

    # API token (TUI, scripts) takes precedence over cookies.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        request["user"] = users.token_user(auth[7:].strip())
        if request["user"] is None:
            return web.json_response({"error": "invalid token"}, status=401)
    else:
        request["user"] = users.session_user(request.cookies.get(COOKIE, ""))

    path = request.path
    if request["user"] is None and not _public(path):
        if users.count() == 0:
            if path.startswith("/api/"):
                return web.json_response({"error": "setup required", "setup": True}, status=401)
            raise web.HTTPFound("/setup")
        if path.startswith("/api/"):
            return web.json_response({"error": "login required"}, status=401)
        raise web.HTTPFound(f"/login?next={request.path_qs}" if path != "/" else "/login")
    return await handler(request)


def require_admin(request: web.Request) -> dict:
    u = request["user"]
    if u is None:
        raise web.HTTPUnauthorized(text="login required")
    if u["role"] != "admin":
        raise web.HTTPForbidden(text="admin only")
    return u


def current_user(request: web.Request) -> dict:
    u = request["user"]
    if u is None:
        raise web.HTTPUnauthorized(text="login required")
    return u


# ---------------------------------------------------------------------- #
# handlers
# ---------------------------------------------------------------------- #
async def do_login(request: web.Request) -> web.Response:
    users: Users = request.app["users"]
    cfg = request.app["cfg"]
    addr = _client_addr(request)
    limit = int(cfg["login_attempts"])
    if users.recent_attempts(addr) >= limit:
        log.warning("login throttled for %s", addr)
        return web.json_response({"error": "too many attempts; wait ten minutes"}, status=429)
    body = await _body(request)
    name, password = str(body.get("name", "")), str(body.get("password", ""))
    user = users.check_password(name, password)
    if user is None:
        users.note_attempt(addr)
        log.info("failed login for %r from %s", name, addr)
        return web.json_response({"error": "wrong user name or password"}, status=401)
    users.clear_attempts(addr)
    tok = users.new_session(user["id"], float(cfg["session_hours"]), addr,
                            request.headers.get("User-Agent", ""))
    resp = web.json_response({"ok": True, "user": user})
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="Lax",
                    secure=bool(cfg["https"]), max_age=int(float(cfg["session_hours"]) * 3600))
    log.info("login: %s from %s", user["name"], addr)
    return resp


async def do_logout(request: web.Request) -> web.Response:
    users: Users = request.app["users"]
    tok = request.cookies.get(COOKIE, "")
    if tok:
        users.end_session(tok)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE)
    return resp


async def do_setup(request: web.Request) -> web.Response:
    """First run: create the admin.  Refused once any user exists."""
    users: Users = request.app["users"]
    if users.count() > 0:
        return web.json_response({"error": "setup already done"}, status=409)
    body = await _body(request)
    name = str(body.get("name", "admin")).strip() or "admin"
    try:
        uid = users.add(name, str(body.get("password", "")), "admin")
    except AuthError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    cfg = request.app["cfg"]
    tok = users.new_session(uid, float(cfg["session_hours"]), _client_addr(request))
    resp = web.json_response({"ok": True, "user": {"id": uid, "name": name, "role": "admin"}})
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="Lax", secure=bool(cfg["https"]))
    log.warning("first-run setup: admin %r created from %s", name, _client_addr(request))
    return resp


async def do_change_password(request: web.Request) -> web.Response:
    users: Users = request.app["users"]
    me = current_user(request)
    body = await _body(request)
    if users.check_password(me["name"], str(body.get("current", ""))) is None:
        return web.json_response({"error": "current password is wrong"}, status=401)
    try:
        users.set_password(me["name"], str(body.get("new", "")))
    except AuthError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # set_password revoked every session including this one; issue a new one.
    cfg = request.app["cfg"]
    tok = users.new_session(me["id"], float(cfg["session_hours"]), _client_addr(request))
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="Lax", secure=bool(cfg["https"]))
    return resp


async def _body(request: web.Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:                                # noqa: BLE001
        return {}
