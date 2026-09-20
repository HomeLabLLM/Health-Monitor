"""Per-user graph profiles with optimistic concurrency.

Every user has their own profiles; nothing is shared.  A profile carries
a version, and a save quoting a stale one is refused with a clear message
-- the same user with two tabs open is the realistic case now that
profiles are private.  A new user starts from ``DEFAULT_PROFILE``,
which references no specific monitor so it works everywhere.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from ..store import db

log = logging.getLogger("health-monitor.web.profiles")


class StaleWrite(Exception):
    def __init__(self, name: str, have: int, want: int) -> None:
        super().__init__(f"{name} was updated (version {have}, you sent {want})")
        self.name, self.have, self.want = name, have, want


@dataclass
class Profile:
    name: str
    version: int
    updated_at: float
    updated_by: str
    config: dict

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "updated_at": self.updated_at,
                "updated_by": self.updated_by, "config": self.config}


DEFAULT_PROFILE = {
    "graphs": [
        {"title": "Temperatures", "series": [], "update_ms": 1000},
        {"title": "Power", "series": [], "update_ms": 1000},
    ],
    "range": {"mode": "last", "seconds": 1800},
    "sources": None,
}


class Profiles:
    def __init__(self, path: str) -> None:
        self._conn = db.open_profiles(path)
        self._lock = threading.RLock()

    def names(self, uid: int) -> list[dict]:
        with self._lock:
            return [{"name": r["name"], "version": r["version"], "updated_at": r["updated_at"],
                     "updated_by": r["updated_by"]}
                    for r in self._conn.execute(
                        "SELECT name, version, updated_at, updated_by FROM profiles "
                        "WHERE user_id = ? ORDER BY name", (uid,))]

    def get(self, uid: int, name: str) -> Profile | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM profiles WHERE user_id = ? AND name = ?",
                                   (uid, name)).fetchone()
        if r is None:
            return None
        return Profile(r["name"], r["version"], r["updated_at"], r["updated_by"],
                       json.loads(r["config"]))

    def save(self, uid: int, name: str, config: dict, *, version: int | None,
             who: str = "") -> Profile:
        name = name.strip()[:80]
        if not name:
            raise ValueError("profile name required")
        now = time.time()
        with self._lock:
            r = self._conn.execute("SELECT version FROM profiles WHERE user_id = ? AND name = ?",
                                   (uid, name)).fetchone()
            if r is None:
                self._conn.execute(
                    "INSERT INTO profiles (user_id, name, version, updated_at, updated_by, config) "
                    "VALUES (?, ?, 1, ?, ?, ?)", (uid, name, now, who, json.dumps(config)))
                return Profile(name, 1, now, who, config)
            current = r["version"]
            if version is None or version != current:
                raise StaleWrite(name, current, version or 0)
            self._conn.execute(
                "UPDATE profiles SET version = ?, updated_at = ?, updated_by = ?, config = ? "
                "WHERE user_id = ? AND name = ?",
                (current + 1, now, who, json.dumps(config), uid, name))
            return Profile(name, current + 1, now, who, config)

    def delete(self, uid: int, name: str) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM profiles WHERE user_id = ? AND name = ?",
                                      (uid, name)).rowcount > 0

    def delete_user(self, uid: int) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM profiles WHERE user_id = ?", (uid,)).rowcount

    def ensure_default(self, uid: int, who: str) -> None:
        if not self.names(uid):
            self.save(uid, "default", DEFAULT_PROFILE, version=None, who=who)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
