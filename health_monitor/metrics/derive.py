"""Derived series: rates, ratios, duties and histogram statistics.

All rates use the real elapsed time between samples rather than an
assumed interval, so changing the recording speed mid-run -- or a sweep
running long because something else is hammering the firmware mailbox --
never corrupts a derived value.

Counters that go *backwards* yield None rather than a huge negative
spike.  vLLM's Prometheus counters reset to zero when the server
restarts, and the NIC firmware on this stack occasionally serves a read
offset by an exact multiple of 2**32 (see the counter-read defect guard
in poller.py).  Both would otherwise draw a cliff.
"""

from __future__ import annotations

import logging
import math
from collections import deque

from .catalog import Kind, Series

log = logging.getLogger("hl-traf.derive")

NS_PER_S = 1e9
HIST_WINDOW = 60.0      # seconds of history behind latency percentiles


class Deriver:
    """Computes derived values from the current sweep plus history."""

    def __init__(self, catalog: dict[str, Series]) -> None:
        self.catalog = catalog
        self._prev: dict[str, tuple[float, float]] = {}   # key -> (ts, value)
        self._prev_scrape: dict[str, object] = {}         # endpoint tag -> Scrape
        self._rates: dict[str, float | None] = {}         # rates for this sweep
        # Histograms only move when a request finishes, so differencing
        # them sweep-to-sweep leaves TTFT and e2e latency almost always
        # empty at a 1-5s cadence.  They are differenced against a scrape
        # from HIST_WINDOW ago instead, the way Prometheus' rate()[1m]
        # does, which yields a stable percentile that still tracks change.
        self._ring: dict[str, deque] = {}

    # ------------------------------------------------------------------ #
    def _rate(self, key: str, ts: float, value: float | None) -> float | None:
        """Per-second rate of a monotonic counter."""
        if value is None:
            return None
        prev = self._prev.get(key)
        self._prev[key] = (ts, value)
        if prev is None:
            return None                      # first sample has no rate
        pts, pval = prev
        dt = ts - pts
        if dt <= 0:
            return None
        if value < pval:
            log.debug("counter %s went backwards (%s -> %s); reset assumed",
                      key, pval, value)
            return None
        return (value - pval) / dt

    # ------------------------------------------------------------------ #
    def compute(self, ts: float, values: dict[str, float | None],
                scrapes: dict[str, object] | None = None
                ) -> dict[str, float | None]:
        """Fill in every DERIVED series the catalog knows about."""
        out = dict(values)
        scrapes = scrapes or {}

        # Rate every hardware counter first, so the duty/rate consumers
        # below just look the answer up instead of recomputing it.
        self._rates = {}
        for key, s in self.catalog.items():
            if s.kind is Kind.COUNTER and key in out:
                self._rates[key] = self._rate(key, ts, out[key])

        for key, s in self.catalog.items():
            if s.kind is not Kind.DERIVED:
                continue
            spec = s.path.split(":")
            op = spec[0]
            try:
                val = self._apply(op, spec[1:], ts, out, scrapes, s)
            except (KeyError, IndexError, ValueError, ZeroDivisionError) as exc:
                log.debug("derived %s failed: %s", key, exc)
                val = None
            if val is not None and s.scale != 1.0:
                val *= s.scale
            out[key] = val
        return out

    # ------------------------------------------------------------------ #
    def _apply(self, op: str, args: list[str], ts: float,
               vals: dict[str, float | None], scrapes: dict,
               series: Series) -> float | None:
        if op == "mul":
            a, b = vals.get(args[0]), vals.get(args[1])
            return None if a is None or b is None else a * b
        if op == "add":
            a, b = vals.get(args[0]), vals.get(args[1])
            return None if a is None or b is None else a + b
        if op == "pct":
            a, b = vals.get(args[0]), vals.get(args[1])
            if a is None or b is None or b == 0:
                return None
            return 100.0 * a / b

        if series.source == "derived":
            if op == "rate":
                # Rate of another catalog counter, computed in compute().
                return self._rates.get(args[0])
            if op == "duty":
                # Cumulative nanoseconds throttled -> percent of wall time.
                r = self._rates.get(args[0])
                return None if r is None else min(100.0, r / NS_PER_S * 100.0)
            return None

        # --- vLLM ------------------------------------------------------ #
        tag = series.key.split(".")[1] if series.key.startswith("vllm.") else ""
        cur = scrapes.get(tag)
        if cur is None:
            return None
        prev = self._prev_scrape.get(tag)

        if op == "rate":
            return self._scrape_rate(tag, args[0], cur, prev)
        if op == "ratio":
            a, b = cur.get(args[0]), cur.get(args[1])
            return None if a is None or not b else 100.0 * a / b
        if op == "ratewin":
            return self._window_ratio(args[0], args[1], cur, prev)
        if op == "havg":
            return self._hist_mean(args[0], cur, self._baseline(tag, cur))
        if op == "hquant":
            return self._hist_quantile(args[0], float(args[1]), cur,
                                       self._baseline(tag, cur))
        return None

    def _baseline(self, tag: str, cur):
        """Oldest retained scrape still inside HIST_WINDOW."""
        ring = self._ring.get(tag)
        if not ring:
            return None
        cutoff = cur.ts - HIST_WINDOW
        for scrape in ring:
            if scrape.ts >= cutoff:
                return scrape
        return ring[-1]

    def note_scrapes(self, scrapes: dict[str, object]) -> None:
        """Retain this sweep's scrapes so the next one can difference them.

        Called by the engine *after* compute(), never before -- the
        windowed ratios and histogram quantiles all need the previous
        documents still intact while they run.
        """
        self._prev_scrape.update(scrapes)
        for tag, scrape in scrapes.items():
            ring = self._ring.setdefault(tag, deque())
            ring.append(scrape)
            cutoff = scrape.ts - HIST_WINDOW * 2
            while len(ring) > 2 and ring[0].ts < cutoff:
                ring.popleft()

    # -- vLLM helpers -------------------------------------------------- #
    def _scrape_rate(self, tag: str, metric: str, cur, prev) -> float | None:
        if prev is None:
            return None
        a, b = prev.get(metric), cur.get(metric)
        dt = cur.ts - prev.ts
        if a is None or b is None or dt <= 0 or b < a:
            return None
        return (b - a) / dt

    @staticmethod
    def _window_ratio(num: str, den: str, cur, prev) -> float | None:
        """Hit rate over the last interval only.

        The lifetime ratio is nearly frozen -- prefix cache reads 94.4%
        over 50M queried tokens here and moves by hundredths -- so the
        windowed figure is the one that actually shows behaviour.
        """
        if prev is None:
            return None
        n0, n1 = prev.get(num), cur.get(num)
        d0, d1 = prev.get(den), cur.get(den)
        if None in (n0, n1, d0, d1) or n1 < n0 or d1 < d0:
            return None
        dd = d1 - d0
        return None if dd <= 0 else 100.0 * (n1 - n0) / dd

    @staticmethod
    def _hist_mean(metric: str, cur, prev) -> float | None:
        """Mean over the window from the histogram's _sum/_count pair."""
        if prev is None:
            return None
        s0, s1 = prev.get(f"{metric}_sum"), cur.get(f"{metric}_sum")
        c0, c1 = prev.get(f"{metric}_count"), cur.get(f"{metric}_count")
        if None in (s0, s1, c0, c1) or s1 < s0 or c1 < c0:
            return None
        dc = c1 - c0
        return None if dc <= 0 else (s1 - s0) / dc

    @staticmethod
    def _hist_quantile(metric: str, q: float, cur, prev) -> float | None:
        """Quantile over the window, from per-bucket deltas.

        Prometheus buckets are cumulative since start, so differencing
        them first gives the distribution of what happened *recently*
        rather than a lifetime figure that never moves.
        """
        name = f"vllm:{metric}"
        cb = cur.buckets.get(name)
        if not cb:
            return None
        pb = prev.buckets.get(name, {}) if prev is not None else {}
        edges = sorted(cb)
        deltas: list[tuple[float, float]] = []
        for le in edges:
            d = cb[le] - pb.get(le, 0.0)
            deltas.append((le, max(0.0, d)))
        total = deltas[-1][1] if deltas else 0.0
        if total <= 0:
            return None
        target = total * q / 100.0
        prev_count = 0.0
        prev_edge = 0.0
        for le, count in deltas:
            if count >= target:
                if math.isinf(le):
                    return prev_edge
                span = count - prev_count
                if span <= 0:
                    return le
                # Linear interpolation inside the bucket.
                frac = (target - prev_count) / span
                return prev_edge + (le - prev_edge) * frac
            prev_count, prev_edge = count, le
        return edges[-1] if not math.isinf(edges[-1]) else prev_edge
