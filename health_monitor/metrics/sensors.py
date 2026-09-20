"""Known hwmon sensor layout for HL-225 / Gaudi2.

Names and ordering were verified against `hl-smi -q` on hl-1.24.x /
fw 62.6.2: hwmon exposes no *_label files, so the 22 temperature channels
are identified purely by index.  The Gaudi backend cross-checks the
channel count before trusting these labels and falls back to generic
names on any mismatch, rather than mislabelling confidently.

Keys are local to a device (``temp.t1``); the engine prefixes them with
``<monitor>/<gpu_id>/``.
"""

from __future__ import annotations

from .catalog import Kind, Series, Unit

# (index, label, group)
TEMPS: list[tuple[int, str, str]] = [
    (1, "On Chip 0", "temp.onchip"), (2, "On Chip 1", "temp.onchip"),
    (3, "On Chip 2", "temp.onchip"), (4, "On Chip 3", "temp.onchip"),
    (5, "HBM 0", "temp.hbm"), (6, "HBM 1", "temp.hbm"), (7, "HBM 2", "temp.hbm"),
    (8, "HBM 3", "temp.hbm"), (9, "HBM 4", "temp.hbm"), (10, "HBM 5", "temp.hbm"),
    (11, "On Chip TD 0", "temp.td"), (12, "On Chip TD 1", "temp.td"),
    (13, "On Chip TD 2", "temp.td"), (14, "On Chip TD 3", "temp.td"),
    (15, "On Board 2 Top", "temp.board"), (16, "On Board 01 Top", "temp.board"),
    (17, "On Board 01 Bot", "temp.board"), (18, "On Board 23 Top", "temp.board"),
    (19, "On Board 23 Bot", "temp.board"),
    (20, "CPLD", "temp.misc"), (21, "VRM1", "temp.misc"), (22, "VRM2", "temp.misc"),
]

TEMP_GROUP_LABELS = {
    "temp.onchip": "Temps / On Chip", "temp.hbm": "Temps / HBM",
    "temp.td": "Temps / On Chip TD", "temp.board": "Temps / On Board",
    "temp.misc": "Temps / Misc",
}

# Power rails.  Only the 54V rail has a hwmon power channel; hl-smi's 12V
# figure is V x I.  Verified: in19 48.26V x curr1 1.253A = 60.5W vs
# power1_input 61.0W; in21 12.03V x curr2 2.200A = 26.5W vs hl-smi 27W.
RAIL_54V_VOLT, RAIL_54V_CURR = 19, 1
RAIL_12V_VOLT, RAIL_12V_CURR = 21, 2

NAMED_VOLTS = {19: "54V rail", 20: "54V rail (sense 2)", 21: "12V rail", 22: "12V rail (sense 2)"}
NAMED_CURRS = {1: "54V rail", 2: "12V rail"}
SUPPLY_VOLT_CHANNELS = {19, 20, 21, 22}
HOUSEKEEPING_VOLT_CHANNELS = set(range(24, 32))    # fixed 1.2-2.5V rails


def volt_bucket(idx: int) -> str:
    """Channel count is not fixed (this box has in0..in32); anything outside
    the known supply and housekeeping sets is treated as a core rail."""
    if idx in SUPPLY_VOLT_CHANNELS:
        return "volt.supply"
    if idx in HOUSEKEEPING_VOLT_CHANNELS:
        return "volt.housekeeping"
    return "volt.core"


def temp_series(idx: int, label: str, group: str) -> list[Series]:
    base = f"temp.t{idx}"
    return [
        Series(key=base, label=label, group=group, unit=Unit.C, source="hwmon",
               path=f"temp{idx}_input", scale=0.001, peak_key=f"{base}.peak",
               crit_key=f"{base}.crit"),
        Series(key=f"{base}.peak", label=f"{label} (peak)", group="peaks.temp", unit=Unit.C,
               source="hwmon", path=f"temp{idx}_highest", scale=0.001, core=False,
               note="firmware-tracked high-water mark since boot"),
        Series(key=f"{base}.crit", label=f"{label} (crit)", group="limits.temp", unit=Unit.C,
               kind=Kind.STATIC, source="hwmon", path=f"temp{idx}_crit", scale=0.001, core=False),
    ]


def volt_series(idx: int) -> list[Series]:
    group = volt_bucket(idx)
    label = NAMED_VOLTS.get(idx, f"in{idx}")
    base = f"volt.in{idx}"
    note = "" if idx in NAMED_VOLTS else "unidentified rail"
    return [
        Series(key=base, label=label, group=group, unit=Unit.V, source="hwmon",
               path=f"in{idx}_input", scale=0.001, core=idx in SUPPLY_VOLT_CHANNELS,
               peak_key=f"{base}.peak", note=note),
        Series(key=f"{base}.peak", label=f"{label} (peak)", group="peaks.volt", unit=Unit.V,
               source="hwmon", path=f"in{idx}_highest", scale=0.001, core=False),
    ]


def curr_series(idx: int) -> list[Series]:
    label = NAMED_CURRS.get(idx, f"curr{idx}")
    note = "" if idx in NAMED_CURRS else "unidentified"
    base = f"curr.c{idx}"
    return [
        Series(key=base, label=label, group="curr", unit=Unit.A, source="hwmon",
               path=f"curr{idx}_input", scale=0.001, core=idx in NAMED_CURRS,
               peak_key=f"{base}.peak", note=note),
        Series(key=f"{base}.peak", label=f"{label} (peak)", group="peaks.curr", unit=Unit.A,
               source="hwmon", path=f"curr{idx}_highest", scale=0.001, core=False),
    ]
