"""HTTP client for the web server, used by the TUI.

The TUI is a client of the *web server* -- never of the manager or a
monitor -- so the user database decides who sees what, exactly as it
does for a browser.  Authentication is a bearer API token (see
`health-monitor users token` or the burger menu).

TLS: the web server's certificate is signed by the fleet CA.  Pass the
CA file to verify it, or --insecure to skip verification on a trusted
LAN; the default is Python's system trust store, which will *not* know
the fleet CA.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request


class ServerUnavailable(RuntimeError):
    pass


class Unauthorized(RuntimeError):
    pass


class Client:
    def __init__(self, base: str, *, token: str | None = None, cafile: str | None = None,
                 insecure: bool = False, timeout: float = 15.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        if insecure:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        else:
            self._ctx = ssl.create_default_context(cafile=cafile)

    def _call(self, path: str, body=None, method: str | None = None):
        url = self.base + path
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:                        # noqa: BLE001
                payload = {"error": str(exc)}
            if exc.code == 401:
                raise Unauthorized(payload.get("error", "unauthorized")) from None
            payload["_status"] = exc.code
            return payload
        except (urllib.error.URLError, OSError) as exc:
            raise ServerUnavailable(
                f"no web server at {self.base} ({exc}).\n"
                f"Start one with:  health-monitor web") from exc

    # ------------------------------------------------------------------ #
    def me(self) -> dict:
        return self._call("/api/me")

    def status(self) -> dict:
        return self._call("/api/status")

    def monitors(self) -> list:
        return self._call("/api/monitors")

    def catalog(self, monitor: str | None = None) -> dict:
        q = f"?monitor={urllib.parse.quote(monitor)}" if monitor else ""
        return self._call("/api/catalog" + q)

    def databases(self) -> list:
        return self._call("/api/databases")

    def events(self, t0: float, t1: float, monitor: str | None = None) -> list:
        q = f"?t0={t0}&t1={t1}" + (f"&monitor={urllib.parse.quote(monitor)}" if monitor else "")
        return self._call("/api/events" + q)

    def profiles(self) -> list:
        return self._call("/api/profiles")

    def profile(self, name: str) -> dict:
        return self._call("/api/profiles/" + urllib.parse.quote(name))

    def save_profile(self, name: str, config: dict, version) -> dict:
        return self._call("/api/profiles/" + urllib.parse.quote(name),
                          {"config": config, "version": version})

    def delete_profile(self, name: str) -> dict:
        return self._call("/api/profiles/" + urllib.parse.quote(name), None, method="DELETE")

    def subscribe(self, client: str, refs: list[str]) -> dict:
        return self._call("/api/subscribe", {"client": client, "refs": refs})

    def query(self, refs: list[str], t0: float, t1: float, sources: list[dict],
              max_points: int = 600, agg: str = "avg") -> dict:
        return self._call("/api/query", {"refs": refs, "t0": t0, "t1": t1, "sources": sources,
                                         "max_points": max_points, "agg": agg})

    def settings(self) -> dict:
        return self._call("/api/settings")

    def set_settings(self, monitor: str, settings: dict) -> dict:
        return self._call("/api/settings", {"monitor": monitor, "settings": settings})

    def set_nickname(self, monitor: str, nickname: str) -> dict:
        return self._call("/api/nickname", {"monitor": monitor, "nickname": nickname})

    def rename_gpu(self, monitor: str, gpu_id: str, name: str) -> dict:
        return self._call("/api/gpu/rename", {"monitor": monitor, "gpu_id": gpu_id, "name": name})
