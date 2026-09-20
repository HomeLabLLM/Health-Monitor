"""Formatting helpers and sparklines."""

from __future__ import annotations

import math

BARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 20, vmax: float | None = None) -> str:
    """Sparkline of the last `width` values.

    vmax=None: scale relative to the series peak (good for bandwidth).
    vmax set:  absolute scale 0..vmax — 0 renders blank and any value > 0
               rounds up (ceil) so tiny samples stay visible.
    """
    if not values:
        return " " * width
    vals = list(values[-width:])
    if len(vals) < width:
        vals = [0.0] * (width - len(vals)) + vals
    scale = vmax if vmax else max(vals)
    if scale <= 0:
        return " " * width
    out = []
    for v in vals:
        if v <= 0:
            out.append(" ")
        else:
            idx = min(8, max(1, math.ceil(v / scale * 8)))
            out.append(BARS[idx])
    return "".join(out)


def spark_text(
    values: list[float],
    width: int,
    style: str,
    vmax: float | None = None,
    cursor_style: str = "reverse",
) -> "Text":
    """Sparkline as styled Text with a terminal-like cursor on the newest
    sample, so the strip visibly ticks even when values are unchanged."""
    from rich.text import Text

    s = sparkline(list(values), width, vmax=vmax)
    txt = Text(no_wrap=True)
    if len(s) > 1:
        txt.append(s[:-1], style=style)
    txt.append(s[-1:], style=cursor_style)
    return txt


def mini_bar(value: float, scale: float, width: int = 6) -> str:
    """Fixed-width utilization bar 0..scale."""
    if scale <= 0:
        return "·" * width
    frac = max(0.0, min(1.0, value / scale))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def fmt_bps(bits_per_sec: float) -> str:
    if bits_per_sec >= 1e12:
        return f"{bits_per_sec / 1e12:6.2f} Tb/s"
    if bits_per_sec >= 1e9:
        return f"{bits_per_sec / 1e9:6.2f} Gb/s"
    if bits_per_sec >= 1e6:
        return f"{bits_per_sec / 1e6:6.1f} Mb/s"
    if bits_per_sec > 0:
        return f"{bits_per_sec / 1e3:6.1f} kb/s"
    return "     0 b/s"


def fmt_bps_short(bits_per_sec: float) -> str:
    if bits_per_sec >= 1e12:
        return f"{bits_per_sec / 1e12:.2f}T"
    if bits_per_sec >= 1e9:
        return f"{bits_per_sec / 1e9:.2f}G"
    if bits_per_sec >= 1e6:
        return f"{bits_per_sec / 1e6:.1f}M"
    if bits_per_sec > 0:
        return f"{bits_per_sec / 1e3:.0f}k"
    return "0"


def fmt_byte_rate(bytes_per_sec: float) -> str:
    """Binary-scaled byte rate: '1.234 MiB/s'; floors at KiB/s."""
    for unit, div in (("GiB/s", 1 << 30), ("MiB/s", 1 << 20)):
        if bytes_per_sec >= div:
            return f"{bytes_per_sec / div:.3f} {unit}"
    return f"{bytes_per_sec / (1 << 10):.3f} KiB/s"


def fmt_byte_rate_short(bytes_per_sec: float) -> str:
    """Compact binary-scaled byte rate: '1.2M' / '340K' / '12B'."""
    if bytes_per_sec >= 1 << 30:
        return f"{bytes_per_sec / (1 << 30):.1f}G"
    if bytes_per_sec >= 1 << 20:
        return f"{bytes_per_sec / (1 << 20):.1f}M"
    if bytes_per_sec >= 1 << 10:
        return f"{bytes_per_sec / (1 << 10):.1f}K"
    return f"{bytes_per_sec:.0f}B"


def heat_style(frac: float) -> str:
    """Color scale for utilization fraction 0..1+."""
    if frac <= 0.001:
        return "grey37"
    if frac < 0.1:
        return "dodger_blue2"
    if frac < 0.3:
        return "cyan3"
    if frac < 0.6:
        return "green3"
    if frac < 0.85:
        return "yellow3"
    return "red3"


# ---------------------------------------------------------------------- #
# change (delta) coloring: increases green, decreases red, 5 bands
# ---------------------------------------------------------------------- #
DELTA_UP = ["dark_green", "green4", "green3", "green1", "bright_green"]
DELTA_DOWN = ["dark_red", "red3", "red1", "bright_red", "bright_red"]
DELTA_BANDS = (10.0, 20.0, 30.0, 40.0, 60.0)  # percent thresholds
DELTA_FLOOR_BPS = 100e6  # ignore residue below 100 Mb/s


def delta_band(prev_bps: float, cur_bps: float, floor_bps: float = DELTA_FLOOR_BPS) -> int:
    """Signed change band: +1..+5 rising, -1..-5 falling, 0 = neutral.

    Traffic appearing or disappearing across the floor maps to the
    strongest band in its direction.
    """
    if prev_bps < floor_bps and cur_bps < floor_bps:
        return 0
    if prev_bps < floor_bps <= cur_bps:
        return len(DELTA_BANDS)
    if cur_bps < floor_bps <= prev_bps:
        return -len(DELTA_BANDS)
    rel = (cur_bps - prev_bps) / prev_bps * 100.0
    if abs(rel) < DELTA_BANDS[0]:
        return 0
    idx = min(len(DELTA_BANDS) - 1, sum(1 for b in DELTA_BANDS if abs(rel) >= b) - 1)
    band = max(1, idx + 1)
    return band if rel > 0 else -band


def delta_style(prev_bps: float, cur_bps: float, floor_bps: float = DELTA_FLOOR_BPS) -> str | None:
    """Text style expressing the relative change prev -> cur.

    None when the change is insignificant.
    """
    band = delta_band(prev_bps, cur_bps, floor_bps)
    if band == 0:
        return None
    palette = DELTA_UP if band > 0 else DELTA_DOWN
    return palette[abs(band) - 1]
