"""The monitor's outbox: store-and-forward with acknowledgement.

Every sweep is appended here first.  Rows are sent to the manager in
batches, each tagged with a batch id; the manager acknowledges a batch
only after *its* commit, and only then are those rows deleted.  A row
that was sent but never acked (connection dropped, manager crashed
mid-batch) has its batch tag cleared on reconnect and is sent again.
The manager's inserts are idempotent on (ref, ts), so a resend is a
no-op there.

Live rows and catch-up rows interleave freely: acknowledgement is per
batch, not "everything up to time T", so acking a fresh live batch can
never trim an older un-acked one.

There is no retention: an acked row is gone.  What bounds the file is
``outbox_max`` -- past it the *oldest un-acked* rows are dropped, and a
``data_dropped`` event covering their time span is queued so the gap is
explained on every graph rather than silently present.

The engine thread appends, the flusher thread commits and the uplink's
executor threads tag/ack, so every method takes ``self._lock``.
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

from . import db

log = logging.getLogger("health-monitor.outbox")


@dataclass
class OutboxStatus:
    rows_pending: int = 0
    rows_inflight: int = 0
    rows_dropped: int = 0
    events_pending: int = 0
    db_bytes: int = 0
    free_bytes: int = 0
    recording: bool = True
    reason: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Outbox:
    def __init__(self, path: str, catalog: dict, *, max_bytes: int, min_free: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.min_free = min_free
        self.status = OutboxStatus()
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = db.open_samples(path, create=True)
        self._ids = db.sync_catalog(self._conn, catalog)
        self._pending: list[tuple[int, float, float]] = []
        self._pending_events: list[dict] = []
        self._next_batch = int(time.time() * 1000) % 1_000_000_000
        # Anything tagged from a previous run was never acked: resend it.
        self._conn.execute("UPDATE samples SET batch = NULL WHERE batch IS NOT NULL")
        self._conn.execute("UPDATE events SET batch = NULL WHERE batch IS NOT NULL")
        self._last_check = 0.0
        self._refresh_status()

    # ------------------------------------------------------------------ #
    def update_catalog(self, catalog: dict) -> None:
        with self._lock:
            self._ids = db.sync_catalog(self._conn, catalog)

    def record(self, ts: float, values: dict[str, float | None]) -> None:
        if not self.status.recording:
            return
        rows = []
        for r, v in values.items():
            if v is None:
                continue
            sid = self._ids.get(r)
            if sid is not None:
                rows.append((sid, ts, float(v)))
        with self._lock:
            self._pending.extend(rows)

    def record_event(self, monitor: str, kind: str, ts: float, end_ts: float | None = None,
                     detail: dict | None = None) -> None:
        with self._lock:
            self._pending_events.append({"monitor": monitor, "kind": kind, "ts": ts,
                                         "end_ts": end_ts, "detail": json.dumps(detail or {})})

    def flush(self) -> None:
        with self._lock:
            if self._pending or self._pending_events:
                rows, self._pending = self._pending, []
                evs, self._pending_events = self._pending_events, []
                try:
                    self._conn.execute("BEGIN")
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO samples (series_id, ts, value, batch) "
                        "VALUES (?, ?, ?, NULL)", rows)
                    self._conn.executemany(
                        "INSERT INTO events (monitor, kind, ts, end_ts, detail) VALUES "
                        "(:monitor, :kind, :ts, :end_ts, :detail)", evs)
                    self._conn.execute("COMMIT")
                except sqlite3.Error:
                    self._conn.execute("ROLLBACK")
                    raise
            now = time.monotonic()
            if now - self._last_check > 15.0:
                self._last_check = now
                self._enforce_limits()
                self._refresh_status()

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    def next_batch(self, limit: int) -> tuple[int, list[list]] | None:
        """Tag up to `limit` oldest untagged rows and return them as
        [[ref, ts, value], ...] with their batch id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.series_id, s.ts, s.value, se.ref FROM samples s "
                "JOIN series se ON se.id = s.series_id "
                "WHERE s.batch IS NULL ORDER BY s.ts LIMIT ?", (limit,)).fetchall()
            if not rows:
                return None
            batch = self._next_batch
            self._next_batch += 1
            try:
                self._conn.execute("BEGIN")
                self._conn.executemany(
                    "UPDATE samples SET batch = ? WHERE series_id = ? AND ts = ?",
                    [(batch, r["series_id"], r["ts"]) for r in rows])
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            return batch, [[r["ref"], r["ts"], r["value"]] for r in rows]

    def next_events(self, limit: int = 200) -> tuple[int, list[dict]] | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, ts, end_ts, detail FROM events WHERE batch IS NULL "
                "ORDER BY ts LIMIT ?", (limit,)).fetchall()
            if not rows:
                return None
            batch = self._next_batch
            self._next_batch += 1
            self._conn.execute("UPDATE events SET batch = ? WHERE id IN (%s)" %
                               ",".join(str(r["id"]) for r in rows), (batch,))
            return batch, [{"kind": r["kind"], "ts": r["ts"], "end": r["end_ts"],
                            "detail": json.loads(r["detail"])} for r in rows]

    def ack(self, batch: int) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE batch = ?", (batch,))
            n = cur.rowcount
            self._conn.execute("DELETE FROM events WHERE batch = ?", (batch,))
            return n

    def reset_inflight(self) -> None:
        """Connection lost: everything tagged but un-acked goes back to
        the queue."""
        with self._lock:
            self._conn.execute("UPDATE samples SET batch = NULL WHERE batch IS NOT NULL")
            self._conn.execute("UPDATE events SET batch = NULL WHERE batch IS NOT NULL")

    def backlog(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    # ------------------------------------------------------------------ #
    # limits (called under the lock)
    # ------------------------------------------------------------------ #
    def _enforce_limits(self) -> None:
        try:
            size = os.path.getsize(self.path)
            wal = self.path + "-wal"
            if os.path.exists(wal):
                size += os.path.getsize(wal)
        except OSError:
            return
        if size > self.max_bytes:
            target = int(self.max_bytes * 0.9)
            dropped = 0
            first = last = None
            while size > target:
                rows = self._conn.execute(
                    "SELECT ts FROM samples WHERE batch IS NULL ORDER BY ts LIMIT 20000").fetchall()
                if not rows:
                    break
                lo, hi = rows[0]["ts"], rows[-1]["ts"]
                first = lo if first is None else min(first, lo)
                last = hi if last is None else max(last, hi)
                cur = self._conn.execute(
                    "DELETE FROM samples WHERE batch IS NULL AND ts <= ?", (hi,))
                dropped += cur.rowcount
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                size = os.path.getsize(self.path)
            if dropped:
                self.status.rows_dropped += dropped
                log.error("OUTBOX FULL: dropped %d oldest un-acked rows (%s .. %s)",
                          dropped, time.strftime("%F %T", time.localtime(first)),
                          time.strftime("%F %T", time.localtime(last)))
                self._pending_events.append({
                    "monitor": "", "kind": "data_dropped", "ts": first, "end_ts": last,
                    "detail": json.dumps({"rows": dropped, "reason": "outbox_max"})})
        try:
            usage = shutil.disk_usage(os.path.dirname(self.path) or ".")
        except OSError:
            return
        self.status.free_bytes = usage.free
        if usage.free < self.min_free:
            if self.status.recording:
                log.error("RECORDING STOPPED - DISK FULL (%.1f GiB free)", usage.free / 1024**3)
            self.status.recording, self.status.reason = False, "DISK FULL"
        elif not self.status.recording and self.status.reason == "DISK FULL":
            self.status.recording, self.status.reason = True, ""
            log.warning("recording resumed")

    def _refresh_status(self) -> None:
        c = self._conn
        self.status.rows_pending = c.execute(
            "SELECT COUNT(*) FROM samples WHERE batch IS NULL").fetchone()[0]
        self.status.rows_inflight = c.execute(
            "SELECT COUNT(*) FROM samples WHERE batch IS NOT NULL").fetchone()[0]
        self.status.events_pending = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        try:
            self.status.db_bytes = os.path.getsize(self.path)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            try:
                self.flush()
            finally:
                self._conn.close()
