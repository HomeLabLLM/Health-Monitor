"""Derived series: rates, ratios, duties and histogram statistics.

All rates use the real elapsed time between samples, so a change of
recording interval -- or a sweep running long -- never corrupts a
derived value.  A counter that goes *backwards* yields None rather than
a negative cliff: vLLM's counters reset when it restarts, and Gaudi NIC
firmware occasionally serves a read offset by an exact multiple of 2**32.

Derived expressions in a catalog use *local* keys (``pct:mem.used:
mem.total``).  ``compute()`` is called once per device with that
device's ref prefix, so the same catalog serves eight identical cards.
"""

from __future__ import annotations

import logging
import math
from collections import deque

from .catalog import Kind, Series

log = logging.getLogger("health-monitor.derive")

NS_PER_S = 1e9
HIST_WINDOW = 60.0     # seconds behind latency percentiles (like rate()[1m])


class Deriver:
    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}      # ref -> (ts, value)
        self._rates: dict[str, float | None] = {}
        self._prev_scrape: dict[str, object] = {}            # tag -> Scrape
        self._ring: dict[str, deque] = {}

    # ------------------------------------------------------------------ #
    def _rate(self, ref: str, ts: float, value: float | None) -> float | None:
        if value is None:
            return None
        prev = self._prev.get(ref)
        self._prev[ref] = (ts, value)
        if prev is None:
            return None
        pts, pval = prev
        dt = ts - pts
        if dt <= 0 or value < pval:
            return None
        return (value - pval) / dt

    # ------------------------------------------------------------------ #
    def compute(self, ts: float, prefix: str, catalog: dict[str, Series],
                values: dict[str, float | None], scrapes: dict[str, object] | None = None
                ) -> dict[str, float | None]:
        """Fill in every DERIVED series for one device.

        `values` is keyed by *ref*; `catalog` by local key; `prefix` is
        ``monitor/gpu_id/``.
        """
        out = dict(values)
        scrapes = scrapes or {}
        for key, s in catalog.items():
            if s.kind is Kind.COUNTER:
                ref = prefix + key
                if ref in out:
                    self._rates[ref] = self._rate(ref, ts, out[ref])
        for key, s in catalog.items():
            if s.kind is not Kind.DERIVED:
                continue
            spec = s.path.split(":")
            try:
                val = self._apply(spec[0], spec[1:], prefix, out, scrapes, s)
            except (KeyError, IndexError, ValueError, ZeroDivisionError) as exc:
                log.debug("derived %s failed: %s", key, exc)
                val = None
            if val is not None and s.scale != 1.0:
                val *= s.scale
            out[prefix + key] = val
        return out

    def _apply(self, op: str, args: list[str], prefix: str, vals: dict, scrapes: dict,
               series: Series) -> float | None:
        g = lambda k: vals.get(prefix + k)   # noqa: E731
        if op == "mul":
            a, b = g(args[0]), g(args[1])
            return None if a is None or b is None else a * b
        if op == "add":
            a, b = g(args[0]), g(args[1])
            return None if a is None or b is None else a + b
        if op == "pct":
            a, b = g(args[0]), g(args[1])
            return None if a is None or not b else 100.0 * a / b
        if series.source == "derived":
            if op == "rate":
                return self._rates.get(prefix + args[0])
            if op == "duty":
                r = self._rates.get(prefix + args[0])
                return None if r is None else min(100.0, r / NS_PER_S * 100.0)
            return None

        # --- vLLM (series.key is vllm.<tag>.<name>) ------------------- #
        tag = series.key.split(".")[1] if series.key.startswith("vllm.") else ""
        cur = scrapes.get(tag)
        if cur is None:
            return None
        prev = self._prev_scrape.get(tag)
        if op == "rate":
            if prev is None:
                return None
            a, b = prev.get(args[0]), cur.get(args[0])
            dt = cur.ts - prev.ts
            return None if a is None or b is None or dt <= 0 or b < a else (b - a) / dt
        if op == "ratio":
            a, b = cur.get(args[0]), cur.get(args[1])
            return None if a is None or not b else 100.0 * a / b
        if op == "ratewin":
            if prev is None:
                return None
            n0, n1 = prev.get(args[0]), cur.get(args[0])
            d0, d1 = prev.get(args[1]), cur.get(args[1])
            if None in (n0, n1, d0, d1) or n1 < n0 or d1 < d0:
                return None
            dd = d1 - d0
            return None if dd <= 0 else 100.0 * (n1 - n0) / dd
        base = self._baseline(tag, cur)
        if op == "havg":
            if base is None:
                return None
            s0, s1 = base.get(f"{args[0]}_sum"), cur.get(f"{args[0]}_sum")
            c0, c1 = base.get(f"{args[0]}_count"), cur.get(f"{args[0]}_count")
            if None in (s0, s1, c0, c1) or s1 < s0 or c1 < c0:
                return None
            dc = c1 - c0
            return None if dc <= 0 else (s1 - s0) / dc
        if op == "hquant":
            return self._hist_quantile(args[0], float(args[1]), cur, base)
        return None

    def _baseline(self, tag: str, cur):
        ring = self._ring.get(tag)
        if not ring:
            return None
        cutoff = cur.ts - HIST_WINDOW
        for scrape in ring:
            if scrape.ts >= cutoff:
                return scrape
        return ring[-1]

    def note_scrapes(self, scrapes: dict[str, object]) -> None:
        """Retain this sweep's scrapes; called *after* compute()."""
        self._prev_scrape.update(scrapes)
        for tag, scrape in scrapes.items():
            ring = self._ring.setdefault(tag, deque())
            ring.append(scrape)
            cutoff = scrape.ts - HIST_WINDOW * 2
            while len(ring) > 2 and ring[0].ts < cutoff:
                ring.popleft()

    @staticmethod
    def _hist_quantile(metric: str, q: float, cur, prev) -> float | None:
        name = f"vllm:{metric}"
        cb = cur.buckets.get(name)
        if not cb:
            return None
        pb = prev.buckets.get(name, {}) if prev is not None else {}
        deltas = [(le, max(0.0, cb[le] - pb.get(le, 0.0))) for le in sorted(cb)]
        total = deltas[-1][1] if deltas else 0.0
        if total <= 0:
            return None
        target = total * q / 100.0
        prev_count = prev_edge = 0.0
        for le, count in deltas:
            if count >= target:
                if math.isinf(le):
                    return prev_edge
                span = count - prev_count
                return le if span <= 0 else prev_edge + (le - prev_edge) * (target - prev_count) / span
            prev_count, prev_edge = count, le
        return prev_edge
