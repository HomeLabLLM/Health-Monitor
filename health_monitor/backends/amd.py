"""AMD backend via amdsmi.

Verified against a Radeon RX 7600 (Navi 33) with amdsmi 26.2 on devbox1.
Consumer Navi exposes less than Instinct: no PCIe bandwidth, no energy
accumulator, no throttle duty counters, and -- the one that matters for
identity -- no serial and a *degenerate* UUID (identical for every card
of the model).  Such a UUID is deliberately not reported, so the identity
registry falls through to model/VBIOS/slot matching instead of treating
two RX 7600s as one card.

amdsmi ships with the driver, not on PyPI in a version-matched form; the
Makefile adds a path shim to the system package rather than pulling the
whole system site-packages into the venv.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from ..metrics.catalog import Kind, Series, Unit
from .base import Backend, BackendUnavailable, DeviceInfo

log = logging.getLogger("health-monitor.amd")

for _p in ("/opt/rocm/share/amd_smi", "/usr/share/amd_smi"):
    if _p not in sys.path:
        sys.path.append(_p)
try:
    import amdsmi
except ImportError:                                  # pragma: no cover
    amdsmi = None

_DEGENERATE_UUID_SUFFIX = "-0000-1000-8000-000000000000"


def _na(v):
    """amdsmi reports 'N/A' as a string in many places."""
    if v is None or (isinstance(v, str) and v.strip().upper() in ("N/A", "")):
        return None
    return v


def _get(fn, *a):
    try:
        return fn(*a)
    except Exception:                                # noqa: BLE001
        return None


def _pci_ids(bdf: str) -> str:
    """vendor:device:subvendor:subdevice from sysfs, '' if unreadable."""
    base = f"/sys/bus/pci/devices/{bdf.lower()}"
    parts = []
    for f in ("vendor", "device", "subsystem_vendor", "subsystem_device"):
        try:
            with open(f"{base}/{f}") as fh:
                parts.append(fh.read().strip().replace("0x", ""))
        except OSError:
            return ""
    return ":".join(parts)


class AmdBackend(Backend):
    vendor = "amd"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg)
        if amdsmi is None:
            raise BackendUnavailable("amdsmi not importable")
        try:
            amdsmi.amdsmi_init()
        except Exception as exc:                     # noqa: BLE001
            raise BackendUnavailable(f"amdsmi init failed: {exc}") from exc
        self._devs: list[DeviceInfo] = []
        for i, h in enumerate(_get(amdsmi.amdsmi_get_processor_handles) or []):
            asic = _get(amdsmi.amdsmi_get_gpu_asic_info, h) or {}
            board = _get(amdsmi.amdsmi_get_gpu_board_info, h) or {}
            vb = _get(amdsmi.amdsmi_get_gpu_vbios_info, h) or {}
            bdf = _get(amdsmi.amdsmi_get_gpu_device_bdf, h) or ""
            uuid = _na(_get(amdsmi.amdsmi_get_gpu_device_uuid, h))
            if uuid and uuid.endswith(_DEGENERATE_UUID_SUFFIX):
                uuid = None                      # not unique per card
            serial = _na(asic.get("asic_serial")) or _na(board.get("product_serial"))
            if serial in ("0x0", "0"):
                serial = None
            # amdsmi's asic_info omits the PCI ids on some builds (it did
            # on devbox1), and an empty model key would let two different
            # AMD models match each other in the registry.  sysfs always
            # has them.
            ids = _pci_ids(str(bdf)) or ":".join(
                str(x).replace("0x", "").lower() if x else ""
                for x in (asic.get("vendor_id"), asic.get("device_id"),
                          asic.get("subvendor_id"), asic.get("subsystem_id")))
            self._devs.append(DeviceInfo(
                vendor="amd", index=i,
                model=_na(asic.get("market_name")) or _na(board.get("product_name")) or "AMD GPU",
                pci_addr=str(bdf).lower(), serial=serial, uuid=uuid,
                vbios=_na(vb.get("version")), ids=ids,
                driver=str(_na((_get(amdsmi.amdsmi_get_gpu_driver_info, h) or {}).get("driver_version")) or ""),
                handle=h))

    def devices(self) -> list[DeviceInfo]:
        return list(self._devs)

    def shutdown(self) -> None:
        try:
            amdsmi.amdsmi_shut_down()
        except Exception:                            # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def _metrics(self, h) -> dict:
        return _get(amdsmi.amdsmi_get_gpu_metrics_info, h) or {}

    def catalog(self, dev: DeviceInfo):
        h = dev.handle
        m = self._metrics(h)
        s = lambda **kw: Series(**{"source": "amdsmi", **kw})  # noqa: E731
        cat: list[Series] = []
        add = cat.append

        def have(*names: str) -> bool:
            return any(_na(m.get(n)) is not None for n in names)

        def have_nonzero(*names: str) -> bool:
            # A sensor that does not exist reads 0 here (temperature_vrmem
            # on the RX 7600), and the energy accumulator on consumer Navi
            # is a permanent 0; neither deserves a flat line on a graph.
            return any((_na(m.get(n)) or 0) not in (0, 0.0) for n in names)

        if have("temperature_edge"):
            add(s(key="temp.edge", label="Edge", group="temp.core", unit=Unit.C, path="temperature_edge"))
        if have("temperature_hotspot"):
            add(s(key="temp.hotspot", label="Hotspot", group="temp.core", unit=Unit.C, path="temperature_hotspot"))
        if have("temperature_mem"):
            add(s(key="temp.mem", label="Memory", group="temp.mem", unit=Unit.C, path="temperature_mem"))
        for k, lbl in (("temperature_vrgfx", "VR gfx"), ("temperature_vrsoc", "VR SoC"),
                       ("temperature_vrmem", "VR mem")):
            if have_nonzero(k):
                add(s(key=f"temp.{k[12:]}", label=lbl, group="temp.misc", unit=Unit.C, path=k))
        if have("current_socket_power", "average_socket_power"):
            add(s(key="power.draw", label="Socket power", group="power", unit=Unit.W,
                  path="current_socket_power|average_socket_power"))
        cap = _get(amdsmi.amdsmi_get_power_cap_info, h) or {}
        if _na(cap.get("power_cap")):
            add(s(key="power.limit", label="Power cap", group="power", unit=Unit.W,
                  kind=Kind.STATIC, path="cap:power_cap", scale=1e-6, core=False))
        if have_nonzero("energy_accumulator"):
            add(s(key="energy", label="Energy consumed", group="energy", unit=Unit.MJ,
                  kind=Kind.COUNTER, path="energy_accumulator", scale=0.001))
            add(s(key="power.avg", label="Average draw (from energy)", group="energy",
                  unit=Unit.W, kind=Kind.DERIVED, source="derived", path="rate:energy",
                  scale=0.001))
        if have("voltage_gfx"):
            add(s(key="volt.gfx", label="GFX voltage", group="volt.supply", unit=Unit.V,
                  path="voltage_gfx", scale=0.001))
        if have("voltage_mem"):
            add(s(key="volt.mem", label="Memory voltage", group="volt.supply", unit=Unit.V,
                  path="voltage_mem", scale=0.001))
        if have("voltage_soc"):
            add(s(key="volt.soc", label="SoC voltage", group="volt.supply", unit=Unit.V,
                  path="voltage_soc", scale=0.001))
        if have("average_gfx_activity"):
            add(s(key="util.gfx", label="GFX activity", group="compute", unit=Unit.PCT, path="average_gfx_activity"))
        if have("average_umc_activity"):
            add(s(key="util.mem", label="Memory controller activity", group="compute",
                  unit=Unit.PCT, path="average_umc_activity"))
        vram = _get(amdsmi.amdsmi_get_gpu_vram_usage, h) or {}
        if _na(vram.get("vram_total")):
            add(s(key="mem.used", label="VRAM used", group="compute", unit=Unit.BYTES,
                  path="vram:vram_used", scale=1024.0**2))
            add(s(key="mem.total", label="VRAM total", group="compute", unit=Unit.BYTES,
                  kind=Kind.STATIC, path="vram:vram_total", scale=1024.0**2, core=False))
            add(s(key="mem.pct", label="VRAM used %", group="compute", unit=Unit.PCT,
                  kind=Kind.DERIVED, source="derived", path="pct:mem.used:mem.total"))
        if have("current_gfxclk"):
            add(s(key="clock.gfx", label="GFX clock", group="clock", unit=Unit.MHZ, path="current_gfxclk"))
        if have("current_uclk"):
            add(s(key="clock.mem", label="Memory clock", group="clock", unit=Unit.MHZ, path="current_uclk"))
        if have("current_fan_speed"):
            add(s(key="fan", label="Fan (RPM)", group="fan", unit=Unit.COUNT, path="current_fan_speed"))
        if have("pcie_link_width"):
            add(s(key="pcie.width", label="PCIe link width", group="pcie", unit=Unit.COUNT, path="pcie_link_width"))
        if have("pcie_link_speed"):
            add(s(key="pcie.gen", label="PCIe link speed", group="pcie", unit=Unit.GTS,
                  path="pcie_link_speed", scale=0.1))
        if have("pcie_bandwidth_inst"):
            add(s(key="pcie.bw", label="PCIe bandwidth", group="pcie", unit=Unit.BPS,
                  path="pcie_bandwidth_inst", scale=1024.0**2))
        if have("throttle_status"):
            add(s(key="throttle.status", label="Throttle status", group="throttle",
                  unit=Unit.COUNT, path="throttle_status"))
        return {x.key: x for x in cat}, {"temp.core": "Temps / Die",
                                          "temp.mem": "Temps / Memory", "fan": "Fan"}

    # ------------------------------------------------------------------ #
    def read(self, dev: DeviceInfo, series: list[Series]) -> dict[str, float | None]:
        h = dev.handle
        m = self._metrics(h)
        vram = None
        cap = None
        out: dict[str, float | None] = {}
        for s in series:
            path = s.path
            val = None
            if path.startswith("vram:"):
                vram = vram if vram is not None else (_get(amdsmi.amdsmi_get_gpu_vram_usage, h) or {})
                val = _na(vram.get(path[5:]))
            elif path.startswith("cap:"):
                cap = cap if cap is not None else (_get(amdsmi.amdsmi_get_power_cap_info, h) or {})
                val = _na(cap.get(path[4:]))
            else:
                for name in path.split("|"):
                    val = _na(m.get(name))
                    if val is not None:
                        break
            if isinstance(val, str):
                # throttle_status arrives as text on some builds
                val = 1.0 if val.upper().startswith("THROTTLED") else \
                      0.0 if val.upper().startswith("UNTHROTTLED") else None
            if isinstance(val, (list, tuple)):
                val = val[0] if val else None
            out[s.key] = None if val is None else float(val) * s.scale
        return out


def create(cfg: dict[str, Any]) -> AmdBackend:
    return AmdBackend(cfg)
