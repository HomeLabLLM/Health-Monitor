"""SQLite schema and connection helpers.

The same samples schema serves two roles:

  * on a **monitor** it is the outbox: rows wait here until the manager
    acknowledges them, then they are deleted.  No rollups, no retention.
  * on the **manager** it is the archive: rows from every monitor, with
    1-minute and 1-hour rollups carrying min/max/avg, rotated by
    ``health-monitor reset``.

A series is identified by its *ref* ``<monitor>/<gpu_id>/<key>``; every
database carries its own catalog and reads resolve by ref, never by
integer id, so an archive's ``series_id 47`` can never mean a different
sensor than the live database's 47.  The monitor and gpu_id columns are
denormalised out of the ref for filtering.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

log = logging.getLogger("health-monitor.store")

SCHEMA_VERSION = 2

ROLLUPS: list[tuple[str, int]] = [("rollup_1m", 60), ("rollup_1h", 3600)]

_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS series (
    id         INTEGER PRIMARY KEY,
    ref        TEXT UNIQUE NOT NULL,
    monitor    TEXT NOT NULL DEFAULT '',
    gpu_id     TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL DEFAULT '',
    label      TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    unit       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'gauge'
);
CREATE INDEX IF NOT EXISTS series_monitor ON series (monitor, gpu_id);

CREATE TABLE IF NOT EXISTS samples (
    series_id INTEGER NOT NULL,
    ts        REAL    NOT NULL,
    value     REAL,
    batch     INTEGER,            -- outbox only: id of the in-flight batch
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    monitor TEXT NOT NULL,
    kind    TEXT NOT NULL,
    ts      REAL NOT NULL,
    end_ts  REAL,
    detail  TEXT NOT NULL DEFAULT '{}',
    batch   INTEGER
);
CREATE INDEX IF NOT EXISTS events_time ON events (monitor, ts);

-- manager only
CREATE TABLE IF NOT EXISTS monitors (
    id        TEXT PRIMARY KEY,
    nickname  TEXT NOT NULL DEFAULT '',
    version   TEXT NOT NULL DEFAULT '',
    proto     INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL DEFAULT 0,
    skew      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gpus (
    monitor   TEXT NOT NULL,
    gpu_id    TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    vendor    TEXT NOT NULL DEFAULT '',
    model     TEXT NOT NULL DEFAULT '',
    serial    TEXT,
    uuid      TEXT,
    pci       TEXT NOT NULL DEFAULT '',
    state     TEXT NOT NULL DEFAULT 'active',
    last_seen REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (monitor, gpu_id)
);
"""

_ROLLUP_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    series_id INTEGER NOT NULL,
    bucket    INTEGER NOT NULL,
    vmin REAL, vmax REAL, vavg REAL,
    n         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (series_id, bucket)
) WITHOUT ROWID;
"""


def _tune(conn: sqlite3.Connection, *, readonly: bool = False) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if not readonly:
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row


def open_samples(path: str, *, create: bool = False) -> sqlite3.Connection:
    if not create and not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    _tune(conn)
    conn.executescript(_SAMPLES_DDL)
    for table, _ in ROLLUPS:
        conn.executescript(_ROLLUP_DDL.format(table=table))
    conn.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('created_at', ?)",
                 (str(time.time()),))
    return conn


def open_samples_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    _tune(conn, readonly=True)
    return conn


def sync_catalog(conn: sqlite3.Connection, catalog: dict[str, object]) -> dict[str, int]:
    """Write ref -> Series into this database; return ref -> id."""
    rows = []
    for r, s in catalog.items():
        parts = r.split("/", 2)
        mon, gid, key = parts if len(parts) == 3 else ("", "", r)
        rows.append((r, mon, gid, key, s.label, s.group, s.unit.value, s.kind.value))
    conn.executemany(
        "INSERT INTO series (ref, monitor, gpu_id, key, label, group_name, unit, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ref) DO UPDATE SET label=excluded.label, group_name=excluded.group_name, "
        "unit=excluded.unit, kind=excluded.kind", rows)
    return {r["ref"]: r["id"] for r in conn.execute("SELECT id, ref FROM series")}


def series_ids(conn: sqlite3.Connection, refs: list[str]) -> dict[str, int]:
    if not refs:
        return {}
    out: dict[str, int] = {}
    for i in range(0, len(refs), 500):
        chunk = refs[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT id, ref FROM series WHERE ref IN ({marks})", chunk):
            out[row["ref"]] = row["id"]
    return out


def series_rows(conn: sqlite3.Connection, monitor: str | None = None) -> list[dict]:
    q = "SELECT ref, monitor, gpu_id, key, label, group_name, unit, kind FROM series"
    args: tuple = ()
    if monitor:
        q += " WHERE monitor = ?"
        args = (monitor,)
    return [dict(r) for r in conn.execute(q, args)]


def extent(conn: sqlite3.Connection) -> tuple[float | None, float | None]:
    rows = {r["k"]: r["v"] for r in
            conn.execute("SELECT k, v FROM meta WHERE k IN ('first_ts','last_ts')")}
    if "first_ts" in rows and "last_ts" in rows:
        return float(rows["first_ts"]), float(rows["last_ts"])
    row = conn.execute("SELECT MIN(bucket) a, MAX(bucket) b FROM rollup_1m").fetchone()
    if row and row["a"] is not None:
        return float(row["a"]), float(row["b"]) + 60.0
    row = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM samples").fetchone()
    if row and row["a"] is not None:
        return float(row["a"]), float(row["b"])
    return None, None


def open_profiles(path: str) -> sqlite3.Connection:
    """Web-server side: per-user graph profiles."""
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    _tune(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS profiles (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        name       TEXT NOT NULL,
        version    INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL,
        updated_by TEXT NOT NULL DEFAULT '',
        config     TEXT NOT NULL,
        UNIQUE (user_id, name)
    );
    """)
    return conn
