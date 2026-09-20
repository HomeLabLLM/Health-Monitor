"""Metric catalog types shared by every backend, the engine, storage, the
API and both UIs.  Nothing else may invent a series key.

A series is identified by a *ref* -- ``<monitor>/<gpu_id>/<key>`` -- where
``key`` is the backend's local name (``temp.hotspot``) and ``gpu_id`` is
the identity registry's stable id (or ``host`` for host-level series such
as vLLM).  Backends only ever see local keys; the engine adds the prefix.
Archives are resolved by ref, never by integer id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Unit(str, Enum):
    """Base unit.  Series sharing a unit share a Y axis."""

    C = "C"
    W = "W"
    V = "V"
    A = "A"
    PCT = "%"
    BPS = "B/s"
    MHZ = "MHz"
    GTS = "GT/s"
    COUNT = "count"
    TOKPS = "tok/s"
    SEC = "s"
    BYTES = "B"
    MJ = "mJ"


class Kind(str, Enum):
    GAUGE = "gauge"
    COUNTER = "counter"     # monotonic; graphed as a rate
    DERIVED = "derived"     # computed from other series of the same device
    STATIC = "static"       # constant for the life of the process


@dataclass(frozen=True)
class Series:
    key: str                # local key, e.g. temp.t1
    label: str
    group: str
    unit: Unit
    kind: Kind = Kind.GAUGE
    source: str = ""        # backend-private reader selector
    path: str = ""          # backend-private path / derived expression
    scale: float = 1.0
    # Recorded by default?  Exotic rails / NIC counters are opt-in because
    # firmware mailboxes are serialised -- see README 'Polling cost'.
    core: bool = True
    peak_key: str | None = None
    crit_key: str | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "group": self.group,
                "unit": self.unit.value, "kind": self.kind.value, "core": self.core,
                "peak": self.peak_key, "crit": self.crit_key, "note": self.note}

    @staticmethod
    def from_dict(d: dict) -> "Series":
        return Series(key=d["key"], label=d.get("label", d["key"]), group=d.get("group", ""),
                      unit=Unit(d.get("unit", "count")), kind=Kind(d.get("kind", "gauge")),
                      core=bool(d.get("core", True)), peak_key=d.get("peak"),
                      crit_key=d.get("crit"), note=d.get("note", ""))


@dataclass
class Group:
    name: str
    label: str
    keys: list[str] = field(default_factory=list)
    unit: Unit | None = None     # set when all members share one
    core: bool = True

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "keys": self.keys,
                "unit": self.unit.value if self.unit else None, "core": self.core}


GROUP_LABELS: dict[str, str] = {
    "temp.core": "Temps / Core", "temp.mem": "Temps / Memory", "temp.misc": "Temps / Misc",
    "power": "Power / Rails", "energy": "Power / Energy",
    "volt.supply": "Voltages / Supply", "volt.core": "Voltages / Core rails",
    "volt.housekeeping": "Voltages / Housekeeping", "curr": "Currents",
    "peaks.temp": "Peaks / Temps", "peaks.volt": "Peaks / Voltages", "peaks.curr": "Peaks / Currents",
    "limits.temp": "Limits / Temps", "clock": "Clocks", "compute": "Compute", "fan": "Fan",
    "pcie": "PCIe", "throttle": "Throttle", "health": "Health",
    "nic.rate": "NIC / Rates", "nic.err": "NIC / Error counters",
}


def build_groups(by_key: dict[str, Series], labels: dict[str, str] | None = None
                 ) -> list[Group]:
    """Bundle series into the ADD GROUP list, one entry per group name."""
    labels = {**GROUP_LABELS, **(labels or {})}
    order: list[str] = []
    members: dict[str, list[str]] = {}
    for key, s in by_key.items():
        members.setdefault(s.group, []).append(key)
        if s.group not in order:
            order.append(s.group)
    out = []
    for name in order:
        keys = members[name]
        units = {by_key[k].unit for k in keys}
        out.append(Group(name=name, label=labels.get(name, name), keys=sorted(keys),
                         unit=units.pop() if len(units) == 1 else None,
                         core=any(by_key[k].core for k in keys)))
    return out


def ref(monitor: str, gpu_id: str, key: str) -> str:
    return f"{monitor}/{gpu_id}/{key}"


def split_ref(r: str) -> tuple[str, str, str]:
    """'mon/gpu/key.with.dots' -> (mon, gpu, key)."""
    parts = r.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"bad series ref {r!r}")
    return parts[0], parts[1], parts[2]
