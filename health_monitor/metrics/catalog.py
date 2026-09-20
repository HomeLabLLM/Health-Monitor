"""Metric catalog: every value this box can measure, in one place.

The catalog is the contract shared by the poller, the recorder, the JSON API
and both UIs. Nothing else is allowed to invent a series key.

Series keys are dotted and stable across databases -- archives are resolved
by *name*, never by integer id, so a rotated db recorded under an older
catalog still plots against the right axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Unit(str, Enum):
    """Base unit. Series sharing a unit share a Y axis."""

    C = "C"            # degrees Celsius
    W = "W"            # watts
    V = "V"            # volts
    A = "A"            # amps
    PCT = "%"          # percent, 0-100
    BPS = "B/s"        # bytes per second
    MHZ = "MHz"
    GTS = "GT/s"
    COUNT = "count"    # monotonic or instantaneous integer
    TOKPS = "tok/s"
    SEC = "s"
    BYTES = "B"
    MJ = "mJ"


class Kind(str, Enum):
    GAUGE = "gauge"        # instantaneous reading
    COUNTER = "counter"    # monotonic; graphed as a rate
    DERIVED = "derived"    # computed from other series
    STATIC = "static"      # constant for the life of the process


@dataclass(frozen=True)
class Series:
    """One measurable value."""

    key: str
    label: str
    group: str
    unit: Unit
    kind: Kind = Kind.GAUGE
    # How to read it. Interpreted by metrics.sources.
    source: str = ""
    path: str = ""
    scale: float = 1.0
    # Which accelerator this belongs to; -1 for host-level series such as
    # vLLM.  Readers are keyed by (source, dev), so this has to be right
    # even on a single-card box or an eight-card one silently reads the
    # same device eight times.
    dev: int = -1
    # Recorded by default? Exotic rails and NIC counters are opt-in because
    # the firmware mailbox is serialised -- see README 'Polling cost'.
    core: bool = True
    # Companion series holding the firmware-tracked peak / critical limit.
    peak_key: str | None = None
    crit_key: str | None = None
    note: str = ""


@dataclass
class Group:
    """A selectable bundle for the ADD GROUP flow."""

    name: str
    label: str
    keys: list[str] = field(default_factory=list)
    # Members share a unit -> one Y axis, shades of one hue.
    unit: Unit | None = None
    core: bool = True
