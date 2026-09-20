"""Reading recorded samples.

A *source* is an ordered list of databases plus a time shift.  That single
idea covers everything the UI offers:

    live            -> one database,  shift 0
    stitched week   -> [archive3, archive2, archive1, live],  shift 0
    overlay         -> two or more sources on one graph, each with a shift

so "stitch" and "overlay" are not special cases, they compose.

Two rules the rest of the system depends on:

  * Series are resolved by key in every database independently.  An
    archive recorded before a sensor existed simply yields no points for
    it; it never yields the *wrong* sensor's points.
  * Gaps are emitted as None and never interpolated.  Rotation is not
    instantaneous and the server may have been down between archives;
    drawing a straight line across six missing hours would be a lie.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field

from . import db

log = logging.getLogger("hl-traf.reader")

# A point is emitted as a gap when the step to the next one exceeds this
# multiple of the tier's nominal interval.
GAP_FACTOR = 2.5


@dataclass
class Source:
    """An ordered list of databases rendered as one timeline."""

    dbs: list[str]
    shift: float = 0.0
    label: str = ""

    def __post_init__(self) -> None:
        if not self.dbs:
            raise ValueError("source needs at least one database")


@dataclass
class Tier:
    table: str | None      # None = raw samples
    interval: float


TIERS: list[Tier] = [
    Tier(None, 0.0),
    Tier("rollup_1m", 60.0),
    Tier("rollup_1h", 3600.0),
]


def pick_tier(span: float, max_points: int, sample_interval: float) -> Tier:
    """Finest tier whose point count still fits within max_points.

    A tier returns roughly span/interval points, so the budget is met when
    interval >= span/max_points.  Choosing by *span* rather than by the
    number of points already on screen keeps resolution stable while a
    graph scrolls, instead of flipping tiers as points accumulate.
    """
    if max_points <= 0:
        max_points = 1
    needed = span / max_points
    for tier in TIERS:
        effective = tier.interval or sample_interval or 1.0
        if effective >= needed:
            return tier
    return TIERS[-1]


def _fetch(conn: sqlite3.Connection, sid: int, tier: Tier,
           t0: float, t1: float, agg: str) -> list[tuple[float, float | None]]:
    if tier.table is None:
        cur = conn.execute(
            "SELECT ts, value FROM samples "
            "WHERE series_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (sid, t0, t1),
        )
        return [(float(r[0]), r[1]) for r in cur]

    col = {"avg": "vavg", "min": "vmin", "max": "vmax"}.get(agg, "vavg")
    cur = conn.execute(
        f"SELECT bucket, {col} FROM {tier.table} "
        "WHERE series_id = ? AND bucket >= ? AND bucket <= ? ORDER BY bucket",
        (sid, int(t0), int(t1)),
    )
    return [(float(r[0]), r[1]) for r in cur]


def _with_gaps(points: list[tuple[float, float | None]],
               interval: float) -> list[list[float | None]]:
    """Insert an explicit None wherever the series stopped being recorded."""
    if not points:
        return []
    out: list[list[float | None]] = []
    threshold = interval * GAP_FACTOR if interval else 0.0
    prev_ts: float | None = None
    for ts, val in points:
        if (prev_ts is not None and threshold
                and ts - prev_ts > threshold):
            # Mark the hole just after the last real reading.
            out.append([prev_ts + interval, None])
        out.append([ts, val])
        prev_ts = ts
    return out


def query(source: Source, keys: list[str], t0: float, t1: float, *,
          max_points: int = 2000, agg: str = "avg",
          sample_interval: float = 5.0) -> dict[str, list[list[float | None]]]:
    """Read `keys` over [t0, t1] from one source.

    t0/t1 are on the *display* axis.  A shifted source is queried at its
    own real timestamps and the results are moved back onto the display
    axis, so callers never deal with two clocks.
    """
    span = max(t1 - t0, 1e-6)
    tier = pick_tier(span, max_points, sample_interval)
    interval = tier.interval or sample_interval

    # Undo the shift to get real recorded time.
    r0, r1 = t0 - source.shift, t1 - source.shift
    merged: dict[str, list[tuple[float, float | None]]] = {k: [] for k in keys}

    # Query each database separately and concatenate.  SQLite caps ATTACH
    # at 10 databases, and stitching more archives than that is entirely
    # reasonable, so we never rely on ATTACH.
    for path in source.dbs:
        try:
            conn = db.open_samples_ro(path)
        except sqlite3.Error as exc:
            log.warning("cannot read %s: %s", path, exc)
            continue
        try:
            ids = db.series_ids(conn, keys)
            for key, sid in ids.items():
                merged[key] += _fetch(conn, sid, tier, r0, r1, agg)
        finally:
            conn.close()

    out: dict[str, list[list[float | None]]] = {}
    for key, points in merged.items():
        points.sort(key=lambda p: p[0])
        # Overlapping archives would double-plot; selection refuses them,
        # but de-duplicate defensively rather than draw a zigzag.
        deduped: list[tuple[float, float | None]] = []
        for ts, val in points:
            if deduped and ts == deduped[-1][0]:
                continue
            deduped.append((ts, val))
        shifted = [(ts + source.shift, val) for ts, val in deduped]
        out[key] = _with_gaps(shifted, interval)
    return out


# ---------------------------------------------------------------------- #
# Database enumeration for the UI switcher
# ---------------------------------------------------------------------- #

@dataclass
class DbInfo:
    path: str
    name: str
    bytes: int
    start: float | None
    end: float | None
    live: bool = False
    series: int = 0
    error: str = ""


def list_databases(data_dir: str, live_path: str | None = None) -> list[DbInfo]:
    """Enumerate samples databases with their extents, newest first.

    The UI asks the engine for this rather than globbing itself, so the
    extents come from whoever can actually open the files.
    """
    out: list[DbInfo] = []
    try:
        names = os.listdir(data_dir)
    except OSError as exc:
        log.warning("cannot list %s: %s", data_dir, exc)
        return out

    for name in names:
        if not (name.startswith("samples") and name.endswith(".db")):
            continue
        path = os.path.join(data_dir, name)
        info = DbInfo(
            path=path, name=name,
            bytes=os.path.getsize(path) if os.path.exists(path) else 0,
            start=None, end=None,
            live=(live_path is not None and os.path.samefile(path, live_path)
                  if live_path and os.path.exists(live_path) else False),
        )
        try:
            conn = db.open_samples_ro(path)
            try:
                info.start, info.end = db.extent(conn)
                info.series = conn.execute(
                    "SELECT COUNT(*) FROM series").fetchone()[0]
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            info.error = str(exc)
        out.append(info)

    out.sort(key=lambda i: (not i.live, -(i.end or 0)))
    return out
