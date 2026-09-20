"""SQLite schema and connection helpers.

Two databases, deliberately separate:

  profiles.db    graph profiles + engine settings.  Small, written rarely,
                 guarded by a single writer task so concurrent editors
                 cannot corrupt it.
  samples-*.db   recorded sensor data.  One writer (the recorder), many
                 readers.  WAL mode so readers never block the writer.
                 Rotated by `hl-traf serve reset`; old files stay readable
                 and are offered in the UI's database switcher.

Every samples database carries its **own** copy of the series catalog, and
reads resolve by series *key*, never by integer id.  Without this an
archive's `series_id 47` could silently mean a different sensor than the
live database's 47, and switching databases would plot the wrong line
under the right label.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

log = logging.getLogger("hl-traf.store")

SCHEMA_VERSION = 1

# Rollup tiers.  min/max are kept alongside avg because averaging away a
# thermal spike is exactly the information you opened the graph to find.
ROLLUPS: list[tuple[str, int]] = [
    ("rollup_1m", 60),
    ("rollup_1h", 3600),
]

_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    id         INTEGER PRIMARY KEY,
    key        TEXT UNIQUE NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    unit       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'gauge'
);

CREATE TABLE IF NOT EXISTS samples (
    series_id INTEGER NOT NULL,
    ts        REAL    NOT NULL,
    value     REAL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
"""

_ROLLUP_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    series_id INTEGER NOT NULL,
    bucket    INTEGER NOT NULL,
    vmin      REAL,
    vmax      REAL,
    vavg      REAL,
    n         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (series_id, bucket)
) WITHOUT ROWID;
"""

_PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS profiles (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    config     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def _tune(conn: sqlite3.Connection, *, readonly: bool = False) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if not readonly:
        # NORMAL is the right trade for a monitoring recorder: a power cut
        # can lose the last WAL frames, never the database.
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row


def open_samples(path: str, *, create: bool = False) -> sqlite3.Connection:
    """Open a samples database for writing (or create it)."""
    if not create and not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    _tune(conn)
    conn.executescript(_SAMPLES_DDL)
    for table, _secs in ROLLUPS:
        conn.executescript(_ROLLUP_DDL.format(table=table))
    conn.execute(
        "INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta (k, v) VALUES ('created_at', ?)",
        (str(time.time()),),
    )
    return conn


def open_samples_ro(path: str) -> sqlite3.Connection:
    """Open a samples database read-only.  Used for archives and for every
    read of the live database, so a reader can never write to it."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    _tune(conn, readonly=True)
    return conn


def open_profiles(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    _tune(conn)
    conn.executescript(_PROFILES_DDL)
    return conn


def sync_catalog(conn: sqlite3.Connection, catalog: dict) -> dict[str, int]:
    """Write the catalog into this database and return key -> id.

    Ids are assigned per-database and are only ever meaningful *within*
    that file; callers always work in keys.
    """
    rows = [
        (s.key, s.label, s.group, s.unit.value, s.kind.value)
        for s in catalog.values()
    ]
    conn.executemany(
        "INSERT INTO series (key, label, group_name, unit, kind) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET label=excluded.label, "
        "group_name=excluded.group_name, unit=excluded.unit, kind=excluded.kind",
        rows,
    )
    return {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM series")}


def series_ids(conn: sqlite3.Connection, keys: list[str]) -> dict[str, int]:
    """Resolve series keys to this database's ids.  Keys absent from an
    archive simply do not appear -- the caller renders them as empty."""
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    cur = conn.execute(f"SELECT id, key FROM series WHERE key IN ({marks})", keys)
    return {r["key"]: r["id"] for r in cur}


def extent(conn: sqlite3.Connection) -> tuple[float | None, float | None]:
    """(first, last) sample timestamp, for the database switcher listing.

    The recorder maintains first_ts/last_ts in `meta` so this is O(1): the
    UI enumerates every archive on the box, and scanning MIN/MAX over the
    raw samples of a multi-gigabyte file would make that listing crawl.
    Older or interrupted files may lack the meta rows, so fall back to the
    minute rollup (exact enough for a listing) and only then to a scan.
    """
    rows = {
        r["k"]: r["v"] for r in
        conn.execute("SELECT k, v FROM meta WHERE k IN ('first_ts','last_ts')")
    }
    if "first_ts" in rows and "last_ts" in rows:
        return float(rows["first_ts"]), float(rows["last_ts"])

    row = conn.execute(
        "SELECT MIN(bucket) a, MAX(bucket) b FROM rollup_1m").fetchone()
    if row and row["a"] is not None:
        return float(row["a"]), float(row["b"]) + 60.0

    row = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM samples").fetchone()
    if row and row["a"] is not None:
        return float(row["a"]), float(row["b"])
    return None, None
