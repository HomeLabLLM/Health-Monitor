"""The recorder: the only writer to the live samples database.

Runs as a single asyncio task.  Nothing else writes to the samples
database, which is what keeps it from being corrupted by the several
people the server expects to have connected at once.

Recording continues regardless of what anyone is *viewing* -- the
database switcher is per-client, so someone browsing an archive never
stops the live capture.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field

from . import db

log = logging.getLogger("hl-traf.recorder")

# Stop recording while free space is below this, rather than filling the
# filesystem and taking something else down with us.
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3   # 2 GiB
FREE_CHECK_INTERVAL = 30.0             # seconds
FLUSH_INTERVAL = 5.0                   # seconds between commits


@dataclass
class RecorderStatus:
    recording: bool = True
    reason: str = ""
    free_bytes: int = 0
    rows_written: int = 0
    last_flush: float = 0.0
    db_path: str = ""
    db_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "recording": self.recording,
            "reason": self.reason,
            "free_bytes": self.free_bytes,
            "rows_written": self.rows_written,
            "last_flush": self.last_flush,
            "db_path": self.db_path,
            "db_bytes": self.db_bytes,
        }


class Recorder:
    def __init__(self, path: str, catalog: dict, *,
                 min_free: int = DEFAULT_MIN_FREE_BYTES) -> None:
        self.path = path
        self.catalog = catalog
        self.min_free = min_free
        self.status = RecorderStatus(db_path=path)
        self._conn: sqlite3.Connection | None = None
        self._ids: dict[str, int] = {}
        self._pending: list[tuple[int, float, float]] = []
        self._rollup_dirty: dict[tuple[str, int, int], list[float]] = {}
        self._lock = asyncio.Lock()
        self._last_free_check = 0.0
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = db.open_samples(self.path, create=True)
        self._ids = db.sync_catalog(self._conn, self.catalog)
        self._check_free(force=True)
        log.info("recording to %s (%d series)", self.path, len(self._ids))

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._flush()
            finally:
                self._conn.close()
                self._conn = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="recorder")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.close()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            async with self._lock:
                try:
                    self._check_free()
                    self._flush()
                except sqlite3.Error as exc:
                    log.error("flush failed: %s", exc)
                    self.status.recording = False
                    self.status.reason = f"database error: {exc}"

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #
    def record(self, ts: float, values: dict[str, float | None]) -> None:
        """Queue one sweep's worth of readings.  Cheap: no I/O here."""
        if not self.status.recording or self._conn is None:
            return
        for key, val in values.items():
            if val is None:
                continue
            sid = self._ids.get(key)
            if sid is None:
                continue
            self._pending.append((sid, ts, float(val)))
            for table, secs in db.ROLLUPS:
                bucket = int(ts // secs) * secs
                self._rollup_dirty.setdefault((table, sid, bucket), []).append(
                    float(val))

    def _flush(self) -> None:
        if self._conn is None or not self._pending:
            return
        conn = self._conn
        pending, self._pending = self._pending, []
        dirty, self._rollup_dirty = self._rollup_dirty, {}
        lo_ts = min(ts for _sid, ts, _v in pending)
        hi_ts = max(ts for _sid, ts, _v in pending)
        try:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT OR REPLACE INTO samples (series_id, ts, value) "
                "VALUES (?, ?, ?)",
                [(sid, ts, v) for sid, ts, v in pending],
            )
            for (table, sid, bucket), vals in dirty.items():
                lo, hi, tot, n = min(vals), max(vals), sum(vals), len(vals)
                # Merge into whatever the bucket already holds; a bucket
                # spans many flushes.
                conn.execute(
                    f"INSERT INTO {table} (series_id, bucket, vmin, vmax, vavg, n) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(series_id, bucket) DO UPDATE SET "
                    "  vmin = MIN(vmin, excluded.vmin), "
                    "  vmax = MAX(vmax, excluded.vmax), "
                    "  vavg = (vavg * n + excluded.vavg * excluded.n) "
                    "         / (n + excluded.n), "
                    "  n    = n + excluded.n",
                    (sid, bucket, lo, hi, tot / n, n),
                )
            # Keep the extent in meta so listing archives stays O(1).
            conn.execute(
                "INSERT INTO meta (k, v) VALUES ('first_ts', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = MIN(CAST(v AS REAL), ?)",
                (repr(lo_ts), lo_ts),
            )
            conn.execute(
                "INSERT INTO meta (k, v) VALUES ('last_ts', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = MAX(CAST(v AS REAL), ?)",
                (repr(hi_ts), hi_ts),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        self.status.rows_written += len(pending)
        self.status.last_flush = time.time()
        try:
            self.status.db_bytes = os.path.getsize(self.path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # disk guard
    # ------------------------------------------------------------------ #
    def _check_free(self, force: bool = False) -> None:
        """Watch the filesystem the database is actually on.

        On this box `/` sits at 93% while `/home` has hundreds of gigabytes
        free, so checking the wrong mount would either stop recording for
        no reason or never stop at all.
        """
        now = time.monotonic()
        if not force and now - self._last_free_check < FREE_CHECK_INTERVAL:
            return
        self._last_free_check = now
        try:
            usage = shutil.disk_usage(os.path.dirname(self.path) or ".")
        except OSError as exc:
            log.warning("cannot stat filesystem: %s", exc)
            return
        self.status.free_bytes = usage.free
        if usage.free < self.min_free:
            if self.status.recording:
                log.error(
                    "RECORDING STOPPED - DISK FULL (%.1f GiB free, need %.1f GiB)",
                    usage.free / 1024**3, self.min_free / 1024**3)
            self.status.recording = False
            self.status.reason = "DISK FULL"
            self._pending.clear()
            self._rollup_dirty.clear()
        elif not self.status.recording and self.status.reason == "DISK FULL":
            log.warning("recording resumed (%.1f GiB free)", usage.free / 1024**3)
            self.status.recording = True
            self.status.reason = ""


def rotate(path: str) -> str:
    """Rename the live database aside so a fresh one can be started.

    The rotated file keeps its data and shows up in the UI's database
    switcher; nothing is deleted here.  Deleting archives is a
    command-line operation on purpose -- they are multi-gigabyte and
    unrecoverable.
    """
    if not os.path.exists(path):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(path)
    target = f"{base}-{stamp}{ext}"
    for suffix in ("", "-wal", "-shm"):
        src = path + suffix
        if os.path.exists(src):
            os.rename(src, target + suffix)
    log.info("rotated %s -> %s", path, target)
    return target
