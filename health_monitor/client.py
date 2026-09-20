"""JSON client for the monitoring server.

The TUI is a client of the same API the browser uses, so the hardware is
read exactly once regardless of how many people are watching.  There is
deliberately no fallback to opening libhlml directly: a second reader
would slow the server's sweeps for everybody (measured: 236 ms becomes
517 ms with two readers), so if no server is running the TUI says so and
exits rather than quietly making things worse.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ServerUnavailable(RuntimeError):
    pass


class Client:
    def __init__(self, base: str, timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    def _call(self, path: str, body=None, method: str | None = None):
        url = self.base + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:                       # noqa: BLE001
                payload = {"error": str(exc)}
            payload["_status"] = exc.code
            return payload
        except (urllib.error.URLError, OSError) as exc:
            raise ServerUnavailable(
                f"no server at {self.base} ({exc}).\n"
                f"Start one with:  hl-traf serve --port "
                f"{urllib.parse.urlparse(self.base).port or 5678}") from exc

    # ------------------------------------------------------------------ #
    def catalog(self) -> dict:
        return self._call("/api/catalog")

    def status(self) -> dict:
        return self._call("/api/status")

    def databases(self) -> list:
        return self._call("/api/databases")

    def nic(self) -> dict:
        return self._call("/api/nic")

    def profiles(self) -> list:
        return self._call("/api/profiles")

    def profile(self, name: str) -> dict:
        return self._call("/api/profiles/" + urllib.parse.quote(name))

    def save_profile(self, name: str, config: dict, version, who: str) -> dict:
        return self._call("/api/profiles/" + urllib.parse.quote(name),
                          {"config": config, "version": version, "who": who})

    def delete_profile(self, name: str) -> dict:
        return self._call("/api/profiles/" + urllib.parse.quote(name),
                          None, method="DELETE")

    def settings(self) -> dict:
        return self._call("/api/settings")

    def save_settings(self, values: dict) -> dict:
        return self._call("/api/settings", values)

    def subscribe(self, client: str, keys: list[str]) -> dict:
        return self._call("/api/subscribe", {"client": client, "keys": keys})

    def query(self, keys: list[str], t0: float, t1: float,
              sources: list[dict], max_points: int = 600,
              agg: str = "avg") -> dict:
        return self._call("/api/query", {
            "keys": keys, "t0": t0, "t1": t1, "sources": sources,
            "max_points": max_points, "agg": agg,
        })
