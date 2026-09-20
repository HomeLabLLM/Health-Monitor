"""8x8 GPU-pair fabric matrix view."""

from __future__ import annotations

import math
import time

from rich.table import Table
from rich.text import Text

from ..poller import PollerEngine
from ..topology import Wiring
from .format import (
    delta_band,
    fmt_bps_short,
    fmt_byte_rate,
    fmt_byte_rate_short,
    heat_style,
    spark_text,
    sparkline,
)

BARS = " ▁▂▃▄▅▆▇█"


class ChangeTracker:
    """Per-cell baselines + fading "heat" for delta coloring.

    A cell's value baseline only advances when it is older than
    `min_age`, so comparisons reflect movement over ~a second rather
    than per-refresh jitter. A significant change boosts the cell's
    signed heat to its band; the heat then fades linearly to zero over
    `fade` seconds, stepping back down through the color palette, so
    cells relax to neutral instead of flickering on/off.
    """

    def __init__(self, min_age: float = 1.2, fade: float = 2.0):
        self.min_age = min_age
        self.fade = fade
        self.samples: dict[tuple, tuple[float, float]] = {}
        self.heat: dict[tuple, tuple[float, float]] = {}  # key -> (signed level, boost ts)

    def style(self, key: tuple, value: float, now: float, floor: float | None = None) -> str | None:
        from .format import DELTA_DOWN, DELTA_UP

        prev_val, prev_ts = self.samples.get(key, (None, None))
        if prev_val is None:
            self.samples[key] = (value, now)
        elif now - prev_ts >= self.min_age:
            self.samples[key] = (value, now)
            band = delta_band(prev_val, value, floor) if floor is not None else delta_band(prev_val, value)
            if band:
                self.heat[key] = (float(band), now)

        h = self.heat.get(key)
        if h is None:
            return None
        level, boost_ts = h
        mag = abs(level) * max(0.0, 1.0 - (now - boost_ts) / self.fade)
        if mag < 0.5:
            self.heat.pop(key, None)
            return None
        palette = DELTA_UP if level > 0 else DELTA_DOWN
        return palette[min(len(palette), math.ceil(mag)) - 1]


_TRACKER = ChangeTracker()

# PCIe values are bytes/s and noisier at small scale than NIC cells
# (bits/s): use a 1 MB/s floor so keepalive residue stays neutral but
# real DMA-level traffic still colors.
PCIE_FLOOR_BPS = 1e6


def _link_cell(engine: PollerEngine, wiring: Wiring, a: int, b: int, line_rate_bps: float, now: float) -> Text:
    links = wiring.links_between(a, b)
    if not links:
        return Text(" — ", style="grey30")

    total = 0.0
    glyphs = []
    for lk in links:
        if lk.gpu_a == a:
            st = engine.state.get((a, lk.port_a))
        else:
            st = engine.state.get((a, lk.port_b))
        if st is None:
            glyphs.append(("?", "grey30"))
            continue
        link_bps = st.rx_smooth + st.tx_smooth
        total += link_bps
        frac = link_bps / line_rate_bps if line_rate_bps else 0.0
        if link_bps > 0:
            idx = min(8, max(1, int(frac * 8)))  # keep active links visible
            glyphs.append((BARS[idx], heat_style(frac)))
        else:
            glyphs.append(("·", "grey37"))

    txt = Text(justify="center")
    val_style = heat_style(total / (len(links) * line_rate_bps) if line_rate_bps else 0)
    if ds := _TRACKER.style(("pair", a, b), total, now):
        val_style = ds
    txt.append(f"{fmt_bps_short(total)}\n", style=val_style)
    for ch, style in glyphs:
        txt.append(ch, style=style)
    if any(s.stale for s in (engine.state.get((a, lk.port_a if lk.gpu_a == a else lk.port_b)) for lk in links) if s):
        txt.stylize("italic")
    return txt


def _meter(
    prefix: str,
    pct: float | None,
    bar_w: int,
    *,
    label: str | None = None,
) -> Text:
    """nvtop-style meter that degrades with available width.

    wide:   `GPU[████░░░ 20%]`
    medium: `████░ 20%`
    narrow: `20%`

    `label` overrides the trailing text (e.g. `"67W"`) while the bar
    still fills proportional to `pct` (0–100). Defaults to `"{pct}%".
    """
    pct_txt = label if label is not None else (f"{int(round(pct))}%" if pct is not None else "?%")
    style = heat_style(pct / 100.0) if pct is not None else "grey50"

    def bar(inner: int) -> tuple[str, str]:
        if pct is None:
            return "?" * inner, "grey50"
        filled = int(round(pct / 100.0 * inner))
        return "█" * filled + "░" * (inner - filled), style

    txt = Text(no_wrap=True)
    full_overhead = len(prefix) + 1 + 1 + len(pct_txt) + 1  # PFX[ + sp + pct% + ]
    if bar_w >= full_overhead + 3:
        inner = bar_w - full_overhead
        b, bstyle = bar(inner)
        txt.append(f"{prefix}[", style="grey58")
        txt.append(b, style=bstyle)
        txt.append(f" {pct_txt}]", style="grey62")
    elif bar_w >= len(pct_txt) + 4:
        inner = bar_w - len(pct_txt) - 1
        b, bstyle = bar(inner)
        txt.append(b, style=bstyle)
        txt.append(f" {pct_txt}", style="grey62")
    else:
        txt.append(pct_txt, style=style)
    return txt


def _pwr_meter(gs: "GpuHwState", bar_w: int) -> Text:
    """Power meter: bar fills to draw/cap fraction, label shows actual watts."""
    if gs.pwr_used_mw is None:
        return _meter("PWR", None, bar_w, label="?W")
    label = f"{round(gs.pwr_used_mw / 1000)}W"
    return _meter("PWR", gs.pwr_pct, bar_w, label=label)


def _pcie_val(value: float, key: tuple, now: float, cell_w: int) -> Text:
    """Left-justified PCIe rate value with delta coloring."""
    style = _TRACKER.style(key, value, now, floor=PCIE_FLOOR_BPS)
    txt = Text(no_wrap=True, justify="left")
    if cell_w >= 11:
        txt.append(fmt_byte_rate(value), style=style)
    elif cell_w >= 5:
        txt.append(fmt_byte_rate_short(value), style=style)
    return txt


def build_matrix(
    engine: PollerEngine,
    wiring: Wiring | None,
    line_rate_bps: float,
    width: int | None = None,
) -> Table:
    n = engine.gpu_count()
    # Responsive meter/spark width: what's left after the label column and
    # borders, divided over the GPU columns.
    if width is None:
        width = 200
    label_w = 7
    bar_w = max(3, min(30, (width - label_w - (n + 1) * 3) // n - 2))  # -2: cell padding
    cell_w = max(3, (width - label_w - (n + 1) * 3) // n - 2)  # unclamped, for text rows

    tab = Table(
        expand=True,
        pad_edge=False,
        header_style="bold cyan",
        border_style="grey30",
        show_lines=True,
    )
    tab.add_column("", justify="right", no_wrap=True, width=label_w)
    for b in range(n):
        tab.add_column(f"GPU {b}", justify="center", ratio=1)

    # --- status strip: util bar, util history, mem bar, mem history ------ #
    now = time.monotonic()
    lab = Text("util", style="grey62")
    tab.add_row(
        lab,
        *[_meter("GPU", engine.gpu_state[g].util, bar_w) for g in range(n)],
    )
    tab.add_row(
        Text("hist", style="grey50"),
        *[spark_text(list(engine.gpu_state[g].util_spark), max(1, bar_w),
                     style="dodger_blue2", vmax=100.0) for g in range(n)],
    )
    tab.add_row(
        Text("mem", style="grey62"),
        *[_meter("MEM", engine.gpu_state[g].mem_pct, bar_w) for g in range(n)],
    )
    tab.add_row(
        Text("hist", style="grey50"),
        *[spark_text(list(engine.gpu_state[g].mem_spark), max(1, bar_w),
                     style="dark_sea_green", vmax=100.0) for g in range(n)],
    )
    tab.add_row(
        Text("pwr", style="grey62"),
        *[_pwr_meter(engine.gpu_state[g], bar_w) for g in range(n)],
    )
    tab.add_row(
        Text("hist", style="grey50"),
        *[spark_text(list(engine.gpu_state[g].pwr_spark), max(1, bar_w),
                     style="orange3", vmax=100.0) for g in range(n)],
    )
    tab.add_row(
        Text("pcie rx", style="grey62"),
        *[_pcie_val(engine.gpu_state[g].pcie_rx_smooth, ("pcie-rx", g), now, cell_w)
          for g in range(n)],
    )
    tab.add_row(
        Text("pcie tx", style="grey62"),
        *[_pcie_val(engine.gpu_state[g].pcie_tx_smooth, ("pcie-tx", g), now, cell_w)
          for g in range(n)],
    )

    # --- per-GPU-pair rows ------------------------------------------------ #
    for a in range(n):
        row: list = [Text(f"GPU {a}", style="bold")]
        for b in range(n):
            if a == b:
                states = engine.ports_for_gpu(a)
                fabric = sum(s.total_bps for s in states if not s.external)
                ext = sum(s.total_bps for s in states if s.external)
                cell = Text(justify="center")
                cell.append("Σ ", style="grey50")
                val_style = "bold " + heat_style(fabric / (21 * line_rate_bps) if line_rate_bps else 0)
                if ds := _TRACKER.style(("diag", a), fabric, now):
                    val_style = "bold " + ds
                cell.append(f"{fmt_bps_short(fabric)}\n", style=val_style)
                cell.append(f"ext {fmt_bps_short(ext)}", style="magenta3" if ext > 0 else "grey37")
                row.append(cell)
            elif wiring is None:
                row.append(Text(" ?", style="grey30"))
            else:
                row.append(_link_cell(engine, wiring, a, b, line_rate_bps, now))
        tab.add_row(*row)

    # Totals strip.
    return tab


def build_totals_line(engine: PollerEngine, line_rate_bps: float) -> Text:
    total_fabric = sum(s.total_bps for s in engine.state.values() if not s.external)
    total_ext = sum(s.total_bps for s in engine.state.values() if s.external)
    n_links = sum(1 for s in engine.state.values() if not s.external)
    cap = n_links * line_rate_bps
    txt = Text()
    txt.append("diagonal Σ = per-GPU total   ", style="grey50")
    txt.append("fabric total ", style="grey62")
    txt.append(fmt_bps_short(total_fabric), style="bold " + heat_style(total_fabric / cap if cap else 0))
    txt.append(f" / {fmt_bps_short(cap)} cap   ", style="grey50")
    txt.append(f"external {fmt_bps_short(total_ext)}", style="magenta3" if total_ext > 0 else "grey50")
    return txt


def build_sparkline_strip(engine: PollerEngine) -> Text:
    """One combined sparkline of whole-fabric traffic."""
    hist_len = max((len(s.spark) for s in engine.state.values()), default=0)
    if hist_len == 0:
        return Text("")
    series = [0.0] * hist_len
    for s in engine.state.values():
        vals = list(s.spark)
        off = hist_len - len(vals)
        for i, v in enumerate(vals):
            series[off + i] += v
    peak = max(series) or 1.0
    txt = Text("fabric history ", style="grey62")
    txt.append_text(spark_text(series, 48, style="dodger_blue2"))
    return txt
