"""Manager-side storage: every monitor's samples in one archive.

Writes are batch-shaped because that is how they arrive: a monitor sends
``[[ref, ts, value], ...]`` tagged with a batch id, and the ack that
lets it delete those rows goes out only after ``write_rows`` returns --
i.e. after the commit.  Inserts are ``INSERT OR REPLACE`` on (series, ts)
so a resend after a lost ack is a no-op, and the rollup merge is
order-independent (min/max/running mean) so backlog from last Tuesday
can interleave with live rows from now.

Every public method takes ``self._lock``: calls arrive from several
monitor sessions via run_in_executor threads, and a transaction must not
interleave with another session's.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass

from ..metrics.catalog import Series
from ..store import db

log = logging.getLogger("health-monitor.manager.store")


@dataclass
class StoreStatus:
    recording: bool = True
    reason: str = ""
    free_bytes: int = 0
    rows_written: int = 0
    db_bytes: int = 0
    db_path: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class ManagerStore:
    def __init__(self, path: str, *, min_free: int) -> None:
        self.path = path
        self.min_free = min_free
        self.status = StoreStatus(db_path=path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = db.open_samples(path, create=True)
        self._ids: dict[str, int] = {r["ref"]: r["id"] for r in
                                     self._conn.execute("SELECT id, ref FROM series")}
        self._last_check = 0.0
        self._check_free(force=True)

    def _tx(self, fn):
        """Run fn(conn) inside BEGIN/COMMIT under the lock."""
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                out = fn(self._conn)
                self._conn.execute("COMMIT")
                return out
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ #
    # catalog / monitors / gpus
    # ------------------------------------------------------------------ #
    def sync_catalog(self, catalog: dict[str, Series]) -> None:
        def go(conn):
            self._ids.update(db.sync_catalog(conn, catalog))
        self._tx(go)

    def upsert_monitor(self, mid: str, *, version: str, proto: int, skew: float,
                       nickname: str | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO monitors (id, nickname, version, proto, first_seen, last_seen, skew) "
                "VALUES (?, COALESCE(?, ''), ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, proto=excluded.proto, "
                "last_seen=excluded.last_seen, skew=excluded.skew, "
                "nickname=CASE WHEN ? IS NULL THEN nickname ELSE excluded.nickname END",
                (mid, nickname, version, proto, now, now, skew, nickname))

    def touch_monitor(self, mid: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE monitors SET last_seen = ? WHERE id = ?", (time.time(), mid))

    def set_nickname(self, mid: str, nickname: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE monitors SET nickname = ? WHERE id = ?", (nickname, mid))

    def upsert_gpus(self, mid: str, gpus: list[dict]) -> None:
        now = time.time()

        def go(conn):
            conn.execute("UPDATE gpus SET state = 'missing' WHERE monitor = ?", (mid,))
            for g in gpus:
                conn.execute(
                    "INSERT INTO gpus (monitor, gpu_id, name, vendor, model, serial, uuid, pci, "
                    "state, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(monitor, gpu_id) DO UPDATE SET name=excluded.name, "
                    "vendor=excluded.vendor, model=excluded.model, serial=excluded.serial, "
                    "uuid=excluded.uuid, pci=excluded.pci, state=excluded.state, "
                    "last_seen=excluded.last_seen",
                    (mid, g["gpu_id"], g.get("name", g["gpu_id"]), g.get("vendor", ""),
                     g.get("model", ""), g.get("serial"), g.get("uuid"), g.get("pci", ""),
                     g.get("state", "active"), now))
        self._tx(go)

    def monitors(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM monitors ORDER BY id")]

    def gpus(self, mid: str | None = None) -> list[dict]:
        with self._lock:
            if mid:
                cur = self._conn.execute("SELECT * FROM gpus WHERE monitor = ? ORDER BY gpu_id", (mid,))
            else:
                cur = self._conn.execute("SELECT * FROM gpus ORDER BY monitor, gpu_id")
            return [dict(r) for r in cur]

    # ------------------------------------------------------------------ #
    # samples
    # ------------------------------------------------------------------ #
    def write_rows(self, rows: list[list]) -> int:
        """Commit one batch.  Returns rows written; raises on failure so
        the caller withholds the ack."""
        if not self.status.recording:
            raise RuntimeError(f"recording stopped: {self.status.reason}")
        samples: list[tuple[int, float, float]] = []
        unknown = 0
        for ref, ts, val in rows:
            sid = self._ids.get(ref)
            if sid is None:
                unknown += 1
                continue
            if val is None:
                continue
            samples.append((sid, float(ts), float(val)))
        if unknown:
            log.debug("%d rows for refs not in catalog (dropped)", unknown)
        if not samples:
            return 0
        rollup: dict[tuple[str, int, int], list[float]] = {}
        for sid, ts, v in samples:
            for table, secs in db.ROLLUPS:
                rollup.setdefault((table, sid, int(ts // secs) * secs), []).append(v)
        lo = min(ts for _, ts, _ in samples)
        hi = max(ts for _, ts, _ in samples)

        def go(conn):
            conn.executemany(
                "INSERT OR REPLACE INTO samples (series_id, ts, value) VALUES (?, ?, ?)", samples)
            for (table, sid, bucket), vals in rollup.items():
                n = len(vals)
                conn.execute(
                    f"INSERT INTO {table} (series_id, bucket, vmin, vmax, vavg, n) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(series_id, bucket) DO UPDATE SET "
                    "vmin = MIN(vmin, excluded.vmin), vmax = MAX(vmax, excluded.vmax), "
                    "vavg = (vavg * n + excluded.vavg * excluded.n) / (n + excluded.n), "
                    "n = n + excluded.n",
                    (sid, bucket, min(vals), max(vals), sum(vals) / n, n))
            conn.execute("INSERT INTO meta (k, v) VALUES ('first_ts', ?) "
                         "ON CONFLICT(k) DO UPDATE SET v = MIN(CAST(v AS REAL), ?)", (repr(lo), lo))
            conn.execute("INSERT INTO meta (k, v) VALUES ('last_ts', ?) "
                         "ON CONFLICT(k) DO UPDATE SET v = MAX(CAST(v AS REAL), ?)", (repr(hi), hi))
        self._tx(go)
        self.status.rows_written += len(samples)
        self._check_free()
        return len(samples)

    def write_events(self, mid: str, rows: list[dict]) -> int:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO events (monitor, kind, ts, end_ts, detail) VALUES (?, ?, ?, ?, ?)",
                [(mid, r.get("kind", "?"), float(r.get("ts", 0)),
                  None if r.get("end") is None else float(r["end"]),
                  json.dumps(r.get("detail") or {})) for r in rows])
        return len(rows)

    def add_event(self, mid: str, kind: str, ts: float, end: float | None = None,
                  detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (monitor, kind, ts, end_ts, detail) VALUES (?, ?, ?, ?, ?)",
                (mid, kind, ts, end, json.dumps(detail or {})))

    def events(self, mid: str | None, t0: float, t1: float) -> list[dict]:
        q = ("SELECT monitor, kind, ts, end_ts, detail FROM events "
             "WHERE ts <= ? AND COALESCE(end_ts, ts) >= ?")
        args: list = [t1, t0]
        if mid:
            q += " AND monitor = ?"
            args.append(mid)
        q += " ORDER BY ts"
        with self._lock:
            return [{"monitor": r["monitor"], "kind": r["kind"], "ts": r["ts"], "end": r["end_ts"],
                     "detail": json.loads(r["detail"])} for r in self._conn.execute(q, args)]

    def catalog_rows(self, mid: str | None = None) -> list[dict]:
        with self._lock:
            return db.series_rows(self._conn, mid)

    # ------------------------------------------------------------------ #
    def _check_free(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_check < 30.0:
            return
        self._last_check = now
        try:
            usage = shutil.disk_usage(os.path.dirname(self.path) or ".")
            self.status.db_bytes = os.path.getsize(self.path)
        except OSError:
            return
        self.status.free_bytes = usage.free
        if usage.free < self.min_free:
            if self.status.recording:
                log.error("RECORDING STOPPED - DISK FULL (%.1f GiB free)", usage.free / 1024**3)
            self.status.recording, self.status.reason = False, "DISK FULL"
        elif not self.status.recording and self.status.reason == "DISK FULL":
            self.status.recording, self.status.reason = True, ""

    def close(self) -> None:
        with self._lock:
            self._conn.close()
