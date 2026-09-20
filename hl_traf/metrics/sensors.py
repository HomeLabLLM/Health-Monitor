"""Known sensor layout for HL-225 / Gaudi2.

Names and ordering were verified against `hl-smi -q` on this stack
(hl-1.24.x, fw 62.6.2): hwmon exposes no *_label files, so the 22
temperature channels are identified purely by index.  discover()
cross-checks the channel count before trusting these labels and falls
back to generic names on any mismatch, rather than mislabelling
confidently.
"""

from __future__ import annotations

from dataclasses import replace

from .catalog import Kind, Series, Unit


def _own(dev: int, items: list[Series]) -> list[Series]:
    """Stamp the owning device onto a builder's output."""
    return [replace(s, dev=dev) for s in items]

# ---------------------------------------------------------------------- #
# Temperatures: hwmon tempN_input, verified 1:1 against hl-smi -q order.
# (index, label, group, note)
# ---------------------------------------------------------------------- #
TEMPS: list[tuple[int, str, str]] = [
    (1, "On Chip 0", "temp.onchip"),
    (2, "On Chip 1", "temp.onchip"),
    (3, "On Chip 2", "temp.onchip"),
    (4, "On Chip 3", "temp.onchip"),
    (5, "HBM 0", "temp.hbm"),
    (6, "HBM 1", "temp.hbm"),
    (7, "HBM 2", "temp.hbm"),
    (8, "HBM 3", "temp.hbm"),
    (9, "HBM 4", "temp.hbm"),
    (10, "HBM 5", "temp.hbm"),
    (11, "On Chip TD 0", "temp.td"),
    (12, "On Chip TD 1", "temp.td"),
    (13, "On Chip TD 2", "temp.td"),
    (14, "On Chip TD 3", "temp.td"),
    (15, "On Board 2 Top", "temp.board"),
    (16, "On Board 01 Top", "temp.board"),
    (17, "On Board 01 Bot", "temp.board"),
    (18, "On Board 23 Top", "temp.board"),
    (19, "On Board 23 Bot", "temp.board"),
    (20, "CPLD", "temp.misc"),
    (21, "VRM1", "temp.misc"),
    (22, "VRM2", "temp.misc"),
]

TEMP_GROUP_LABELS = {
    "temp.onchip": "Temps / On Chip",
    "temp.hbm": "Temps / HBM",
    "temp.td": "Temps / On Chip TD",
    "temp.board": "Temps / On Board",
    "temp.misc": "Temps / Misc",
}

# ---------------------------------------------------------------------- #
# Power rails.
#
# hl-smi reports two PSU rails.  Only the 54V one has a hwmon power
# channel (power1_input); the 12V figure hl-smi prints is *derived* as
# V x I.  Verified on this box: in19 48.26V x curr1 1.253A = 60.5W vs
# power1_input 61.0W, and in21 12.03V x curr2 2.200A = 26.5W vs hl-smi's
# reported 27W.
#
# The "54V" rail actually runs at ~48.3V; the name is hl-smi's.
# ---------------------------------------------------------------------- #
RAIL_54V_VOLT = 19       # in19_input (in20 is a second sense point)
RAIL_54V_CURR = 1        # curr1_input
RAIL_12V_VOLT = 21       # in21_input (in22 is a second sense point)
RAIL_12V_CURR = 2        # curr2_input

# Supply-rail sense channels get real names; the rest are honest about
# being unidentified.  in0-in18 and in23 all sit at ~817mV and are the
# per-HBM / per-phase core rails; in24-in31 are fixed housekeeping rails.
NAMED_VOLTS: dict[int, str] = {
    19: "54V rail",
    20: "54V rail (sense 2)",
    21: "12V rail",
    22: "12V rail (sense 2)",
}
NAMED_CURRS: dict[int, str] = {
    1: "54V rail",
    2: "12V rail",
}

SUPPLY_VOLT_CHANNELS = {19, 20, 21, 22}
HOUSEKEEPING_VOLT_CHANNELS = set(range(24, 32))   # fixed 1.2-2.5V rails


def volt_bucket(idx: int) -> str:
    """Classify a voltage channel.  Channel count is not fixed -- this box
    has 33 (in0..in32), so anything outside the known supply and
    housekeeping sets is treated as a core rail rather than dropped."""
    if idx in SUPPLY_VOLT_CHANNELS:
        return "volt.supply"
    if idx in HOUSEKEEPING_VOLT_CHANNELS:
        return "volt.housekeeping"
    return "volt.core"


def temp_series(dev: int, idx: int, label: str, group: str) -> list[Series]:
    """Input + firmware peak + critical limit for one temperature channel."""
    base = f"dev{dev}.temp.t{idx}"
    return _own(dev, [
        Series(
            key=base,
            label=label,
            group=group,
            unit=Unit.C,
            kind=Kind.GAUGE,
            source="hwmon",
            path=f"temp{idx}_input",
            scale=0.001,
            core=True,
            peak_key=f"{base}.peak",
            crit_key=f"{base}.crit",
        ),
        Series(
            key=f"{base}.peak",
            label=f"{label} (peak)",
            group="peaks.temp",
            unit=Unit.C,
            kind=Kind.GAUGE,
            source="hwmon",
            path=f"temp{idx}_highest",
            scale=0.001,
            core=False,
            note="firmware-tracked high-water mark since boot",
        ),
        Series(
            key=f"{base}.crit",
            label=f"{label} (crit)",
            group="limits.temp",
            unit=Unit.C,
            kind=Kind.STATIC,
            source="hwmon",
            path=f"temp{idx}_crit",
            scale=0.001,
            core=False,
        ),
    ])


def volt_series(dev: int, idx: int) -> list[Series]:
    """One hwmon voltage channel, plus its peak."""
    group = volt_bucket(idx)
    label = NAMED_VOLTS.get(idx, f"in{idx}")
    base = f"dev{dev}.volt.in{idx}"
    note = "" if idx in NAMED_VOLTS else "unidentified rail"
    return _own(dev, [
        Series(
            key=base, label=label, group=group, unit=Unit.V, kind=Kind.GAUGE,
            source="hwmon", path=f"in{idx}_input", scale=0.001,
            core=idx in SUPPLY_VOLT_CHANNELS, peak_key=f"{base}.peak", note=note,
        ),
        Series(
            key=f"{base}.peak", label=f"{label} (peak)", group="peaks.volt",
            unit=Unit.V, kind=Kind.GAUGE, source="hwmon",
            path=f"in{idx}_highest", scale=0.001, core=False,
        ),
    ])


def curr_series(dev: int, idx: int) -> list[Series]:
    """One hwmon current channel, plus its peak."""
    label = NAMED_CURRS.get(idx, f"curr{idx}")
    note = "" if idx in NAMED_CURRS else "unidentified"
    base = f"dev{dev}.curr.c{idx}"
    return _own(dev, [
        Series(
            key=base, label=label, group="curr", unit=Unit.A, kind=Kind.GAUGE,
            source="hwmon", path=f"curr{idx}_input", scale=0.001,
            core=idx in NAMED_CURRS, peak_key=f"{base}.peak", note=note,
        ),
        Series(
            key=f"{base}.peak", label=f"{label} (peak)", group="peaks.curr",
            unit=Unit.A, kind=Kind.GAUGE, source="hwmon",
            path=f"curr{idx}_highest", scale=0.001, core=False,
        ),
    ])
