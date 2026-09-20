"""Graph profiles and engine settings, with single-writer concurrency.

Several people are expected to have the UI open at once.  Two mechanisms
keep that safe, and they do different jobs:

  * every write goes through one asyncio lock, so the database is never
    written concurrently even though aiohttp is happily serving many
    requests at once;

  * every profile carries a version, and a save that quotes a stale
    version is refused rather than silently overwriting.  The UI turns
    that refusal into "<name> was updated, can't change - try again".

The live push (see server/app.py) means the second editor normally sees
the first one's change as it lands, so the stale-version path is the
backstop rather than the common case.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from ..store import db

log = logging.getLogger("hl-traf.profiles")


class StaleWrite(Exception):
    """Raised when a save quotes a version that is no longer current."""

    def __init__(self, name: str, have: int, want: int) -> None:
        super().__init__(f"{name} was updated (version {have}, you sent {want})")
        self.name = name
        self.have = have
        self.want = want


@dataclass
class Profile:
    name: str
    version: int
    updated_at: float
    updated_by: str
    config: dict

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "config": self.config,
        }


DEFAULT_PROFILE = {
    "graphs": [
        {
            "title": "Temperatures",
            "series": ["dev0.temp.t1", "dev0.temp.t5", "dev0.temp.t21",
                       "dev0.temp.t22"],
            "update_ms": 1000,
        },
        {
            "title": "Power and utilization",
            "series": ["dev0.power.total", "dev0.power.54v", "dev0.power.12v",
                       "dev0.util.aip"],
            "update_ms": 1000,
        },
    ],
    "range": {"mode": "last", "seconds": 1800},
}


class ProfileStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = db.open_profiles(path)
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # reads -- no lock needed, SQLite readers do not block in WAL mode
    # ------------------------------------------------------------------ #
    def names(self) -> list[dict]:
        return [
            {"name": r["name"], "version": r["version"],
             "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
            for r in self._conn.execute(
                "SELECT name, version, updated_at, updated_by FROM profiles "
                "ORDER BY name")
        ]

    def get(self, name: str) -> Profile | None:
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return Profile(row["name"], row["version"], row["updated_at"],
                       row["updated_by"], json.loads(row["config"]))

    # ------------------------------------------------------------------ #
    # writes -- serialised
    # ------------------------------------------------------------------ #
    async def save(self, name: str, config: dict, *, version: int | None,
                   who: str = "") -> Profile:
        """Create or update a profile.

        `version` is the version the caller last read.  Pass None only
        when creating something new; if a profile of that name already
        exists, None is refused rather than clobbering it.
        """
        async with self._lock:
            row = self._conn.execute(
                "SELECT version FROM profiles WHERE name = ?", (name,)
            ).fetchone()
            now = time.time()
            if row is None:
                self._conn.execute(
                    "INSERT INTO profiles (name, version, updated_at, "
                    "updated_by, config) VALUES (?, 1, ?, ?, ?)",
                    (name, now, who, json.dumps(config)))
                return Profile(name, 1, now, who, config)

            current = row["version"]
            if version is None or version != current:
                raise StaleWrite(name, current, version or 0)
            self._conn.execute(
                "UPDATE profiles SET version = ?, updated_at = ?, "
                "updated_by = ?, config = ? WHERE name = ?",
                (current + 1, now, who, json.dumps(config), name))
            return Profile(name, current + 1, now, who, config)

    async def delete(self, name: str) -> bool:
        """Remove a profile.

        This deletes graph configuration only.  Recorded samples are never
        touched here -- archives are removed from the command line, on
        purpose, because they are large and unrecoverable.
        """
        async with self._lock:
            cur = self._conn.execute(
                "DELETE FROM profiles WHERE name = ?", (name,))
            return cur.rowcount > 0

    async def ensure_default(self) -> None:
        if not self.names():
            await self.save("default", DEFAULT_PROFILE, version=None,
                            who="system")
            log.info("created the default profile")

    # ------------------------------------------------------------------ #
    # engine settings (single row set)
    # ------------------------------------------------------------------ #
    def settings(self) -> dict:
        return {r["k"]: json.loads(r["v"])
                for r in self._conn.execute("SELECT k, v FROM settings")}

    async def save_settings(self, values: dict) -> None:
        async with self._lock:
            self._conn.executemany(
                "INSERT INTO settings (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                [(k, json.dumps(v)) for k, v in values.items()])
