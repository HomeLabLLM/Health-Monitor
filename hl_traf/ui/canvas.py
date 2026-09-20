"""Braille plotting for the TUI.

Braille gives 2x4 sub-cells per character, so a 100x12 panel carries
200x48 plottable points -- enough for real line graphs rather than
sparklines.

One limitation is inherent to terminals: a character cell can hold only
one colour.  When two series pass through the same cell their dots are
merged but the colour is the later series'.  That is what every terminal
plotter does; the legend underneath is what disambiguates.
"""

from __future__ import annotations

from dataclasses import dataclass

BRAILLE_BASE = 0x2800
# Bit position of each (x, y) sub-cell within a braille character.
DOT_BITS = (
    (0x01, 0x02, 0x04, 0x40),   # left column,  top -> bottom
    (0x08, 0x10, 0x20, 0x80),   # right column, top -> bottom
)
SUB_X, SUB_Y = 2, 4


class Canvas:
    """A braille drawing surface measured in character cells."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.width = self.cols * SUB_X
        self.height = self.rows * SUB_Y
        self._bits = [0] * (self.cols * self.rows)
        self._color: list[str | None] = [None] * (self.cols * self.rows)

    def clear(self) -> None:
        self._bits = [0] * (self.cols * self.rows)
        self._color = [None] * (self.cols * self.rows)

    def set(self, x: int, y: int, color: str | None = None) -> None:
        """Light one sub-cell.  (0, 0) is top-left."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        cell = (y // SUB_Y) * self.cols + (x // SUB_X)
        self._bits[cell] |= DOT_BITS[x % SUB_X][y % SUB_Y]
        if color:
            self._color[cell] = color

    def line(self, x0: int, y0: int, x1: int, y1: int,
             color: str | None = None) -> None:
        """Bresenham, so a steep jump between samples stays connected."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def rows_markup(self) -> list[str]:
        """Render to rich markup, one string per character row."""
        out = []
        for r in range(self.rows):
            parts: list[str] = []
            run_color: str | None = None
            run: list[str] = []
            for c in range(self.cols):
                i = r * self.cols + c
                ch = chr(BRAILLE_BASE + self._bits[i]) if self._bits[i] else " "
                col = self._color[i]
                if col != run_color:
                    if run:
                        parts.append(_wrap(run_color, "".join(run)))
                    run, run_color = [], col
                run.append(ch)
            if run:
                parts.append(_wrap(run_color, "".join(run)))
            out.append("".join(parts))
        return out


def _wrap(color: str | None, text: str) -> str:
    # Escape rich markup that could appear in a braille run (it cannot,
    # but a space run adjacent to user text might).
    text = text.replace("[", r"\[")
    return f"[{color}]{text}[/]" if color else text


@dataclass
class Trace:
    """One line on a plot."""

    label: str
    color: str
    points: list[tuple[float, float | None]]   # (ts, value), None = gap
    unit: str = ""


def plot(traces: list[Trace], cols: int, rows: int,
         t0: float, t1: float,
         vmin: float | None = None, vmax: float | None = None
         ) -> tuple[list[str], float, float]:
    """Draw traces onto a braille canvas.

    Returns (markup rows, y_min, y_max).  Gaps (None values) break the
    line instead of being bridged -- the same rule the web UI follows,
    for the same reason: the data really is missing there.
    """
    canvas = Canvas(cols, rows)
    vals = [v for t in traces for _, v in t.points if v is not None]
    if not vals:
        return canvas.rows_markup(), 0.0, 1.0
    lo = vmin if vmin is not None else min(vals)
    hi = vmax if vmax is not None else max(vals)
    if hi - lo < 1e-9:
        pad = max(abs(hi) * 0.05, 0.5)
        lo, hi = lo - pad, hi + pad
    span_t = max(t1 - t0, 1e-9)
    span_v = hi - lo

    for tr in traces:
        prev: tuple[int, int] | None = None
        for ts, val in tr.points:
            if val is None:
                prev = None            # break the line across the gap
                continue
            x = int((ts - t0) / span_t * (canvas.width - 1))
            y = int((1.0 - (val - lo) / span_v) * (canvas.height - 1))
            x = max(0, min(canvas.width - 1, x))
            y = max(0, min(canvas.height - 1, y))
            if prev is not None:
                canvas.line(prev[0], prev[1], x, y, tr.color)
            else:
                canvas.set(x, y, tr.color)
            prev = (x, y)
    return canvas.rows_markup(), lo, hi
