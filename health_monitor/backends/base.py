"""The backend interface every vendor implements.

A backend enumerates the devices it can see, describes what each one can
measure (a catalog of local series keys such as ``temp.hotspot``), and
reads a requested subset.  It knows nothing about monitors, storage or
identity -- the engine prefixes keys with ``<monitor>/<gpu_id>/`` and the
identity registry decides which physical card a device *is*.

Keys are local and vendor-shaped on purpose: a Gaudi has 22 temperature
channels and an RX 7600 has three, and pretending otherwise would only
hide data.  Units are shared (``Unit``), which is what lets the UI put a
Gaudi HBM temperature and an NVIDIA memory temperature on one axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..metrics.catalog import Series


@dataclass
class DeviceInfo:
    """A physical accelerator as a backend sees it *this* run.

    Nothing here is assumed stable across reboots except ``serial`` and a
    genuinely unique ``uuid``; the identity registry does the matching.
    """

    vendor: str                  # backend name: intel_gaudi, nvidia, amd, sim
    index: int                   # backend-local index this run
    model: str
    pci_addr: str                # "0000:09:00.0"
    serial: str | None = None
    uuid: str | None = None      # only set when it is truly unique per card
    vbios: str | None = None
    ids: str = ""                # vendor:device:subvendor:subdevice, hex
    driver: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    handle: Any = None           # backend-private

    @property
    def short_vendor(self) -> str:
        return {"intel_gaudi": "gaudi", "nvidia": "nv", "amd": "amd",
                "sim": "sim"}.get(self.vendor, self.vendor)


class Backend:
    """Base class; subclasses set ``vendor`` and implement the four calls."""

    vendor: str = "base"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or {}

    # -- discovery ----------------------------------------------------- #
    def devices(self) -> list[DeviceInfo]:
        raise NotImplementedError

    def catalog(self, dev: DeviceInfo) -> tuple[dict[str, Series], dict[str, str]]:
        """(series by local key, group label overrides)."""
        raise NotImplementedError

    # -- reading ------------------------------------------------------- #
    def read(self, dev: DeviceInfo, series: list[Series]) -> dict[str, float | None]:
        raise NotImplementedError

    def read_static(self, dev: DeviceInfo, series: list[Series]) -> dict[str, float | None]:
        """Constants (limits, sizes).  Default: same path as read()."""
        return self.read(dev, series)

    def shutdown(self) -> None:
        pass


class BackendUnavailable(RuntimeError):
    """Raised by a backend's constructor when its driver/library is absent.

    The loader treats it as 'not on this box' and moves on, so a mixed
    box loads every backend that works and logs the ones that don't.
    """
