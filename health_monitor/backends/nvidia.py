"""NVIDIA backend via NVML (nvidia-ml-py).

Verified against a Quadro RTX 8000 on driver 595.80 under Python 3.14.
Anything NVML reports as unsupported on a given card is dropped from that
card's catalog at discovery, so a GeForce without a serial or a fan
simply has fewer series rather than a column of nulls.

Serial: burned in on Quadro/Tesla, usually absent on GeForce -- the
identity registry then falls back to the GPU UUID, which *is* unique and
stable on every NVIDIA card.
"""

from __future__ import annotations

import logging
from typing import Any

from ..metrics.catalog import Kind, Series, Unit
from .base import Backend, BackendUnavailable, DeviceInfo

log = logging.getLogger("health-monitor.nvidia")

try:
    import pynvml
except ImportError:                                  # pragma: no cover
    pynvml = None

NS_PER_S = 1e9


def _s(h, fn, *a):
    """Call an NVML function, returning None where the card says no."""
    try:
        return fn(h, *a)
    except Exception:                                # noqa: BLE001
        return None


def _str(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


class NvidiaBackend(Backend):
    vendor = "nvidia"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg)
        if pynvml is None:
            raise BackendUnavailable("nvidia-ml-py not installed")
        try:
            pynvml.nvmlInit()
        except Exception as exc:                     # noqa: BLE001
            raise BackendUnavailable(f"NVML init failed: {exc}") from exc
        self.driver = _str(pynvml.nvmlSystemGetDriverVersion())
        self._devs: list[DeviceInfo] = []
        self._supports: dict[int, set[str]] = {}
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            pci = pynvml.nvmlDeviceGetPciInfo(h)
            ids = f"{pci.pciDeviceId & 0xffff:04x}:{pci.pciDeviceId >> 16:04x}:" \
                  f"{pci.pciSubSystemId & 0xffff:04x}:{pci.pciSubSystemId >> 16:04x}"
            serial = _s(h, pynvml.nvmlDeviceGetSerial)
            uuid = _s(h, pynvml.nvmlDeviceGetUUID)
            self._devs.append(DeviceInfo(
                vendor="nvidia", index=i, model=_str(pynvml.nvmlDeviceGetName(h)),
                pci_addr=_str(pci.busId).lower(),
                serial=_str(serial) if serial else None,
                uuid=_str(uuid) if uuid else None,
                vbios=_str(_s(h, pynvml.nvmlDeviceGetVbiosVersion) or ""),
                ids=ids, driver=self.driver, handle=h))

    def devices(self) -> list[DeviceInfo]:
        return list(self._devs)

    def shutdown(self) -> None:
        try:
            pynvml.nvmlShutdown()
        except Exception:                            # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def catalog(self, dev: DeviceInfo):
        h = dev.handle
        s = lambda **kw: Series(**{"source": "nvml", **kw})  # noqa: E731
        probe = {   # local key -> (function, args) used to test support
            "temp.gpu": (pynvml.nvmlDeviceGetTemperature, pynvml.NVML_TEMPERATURE_GPU),
            "power.draw": (pynvml.nvmlDeviceGetPowerUsage,),
            "power.limit": (pynvml.nvmlDeviceGetPowerManagementLimit,),
            "energy": (pynvml.nvmlDeviceGetTotalEnergyConsumption,),
            "util": (pynvml.nvmlDeviceGetUtilizationRates,),
            "mem": (pynvml.nvmlDeviceGetMemoryInfo,),
            "clock.sm": (pynvml.nvmlDeviceGetClockInfo, pynvml.NVML_CLOCK_SM),
            "clock.mem": (pynvml.nvmlDeviceGetClockInfo, pynvml.NVML_CLOCK_MEM),
            "clock.sm.max": (pynvml.nvmlDeviceGetMaxClockInfo, pynvml.NVML_CLOCK_SM),
            "fan": (pynvml.nvmlDeviceGetFanSpeed,),
            "pcie.tx": (pynvml.nvmlDeviceGetPcieThroughput, pynvml.NVML_PCIE_UTIL_TX_BYTES),
            "pcie.rx": (pynvml.nvmlDeviceGetPcieThroughput, pynvml.NVML_PCIE_UTIL_RX_BYTES),
            "pcie.gen": (pynvml.nvmlDeviceGetCurrPcieLinkGeneration,),
            "pcie.width": (pynvml.nvmlDeviceGetCurrPcieLinkWidth,),
            "pcie.replay": (pynvml.nvmlDeviceGetPcieReplayCounter,),
            "throttle": (pynvml.nvmlDeviceGetCurrentClocksThrottleReasons,),
            "viol.power": (pynvml.nvmlDeviceGetViolationStatus, pynvml.NVML_PERF_POLICY_POWER),
            "viol.thermal": (pynvml.nvmlDeviceGetViolationStatus, pynvml.NVML_PERF_POLICY_THERMAL),
            "ecc": (pynvml.nvmlDeviceGetTotalEccErrors, pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED,
                    pynvml.NVML_VOLATILE_ECC),
        }
        ok = {k for k, (fn, *a) in probe.items() if _s(h, fn, *a) is not None}
        # memory temperature exists on data-centre parts only
        if _s(h, pynvml.nvmlDeviceGetTemperature, 1) is not None:
            ok.add("temp.mem")
        self._supports[dev.index] = ok

        cat: list[Series] = []
        add = cat.append
        if "temp.gpu" in ok:
            add(s(key="temp.gpu", label="GPU", group="temp.core", unit=Unit.C, path="temp.gpu"))
        if "temp.mem" in ok:
            add(s(key="temp.mem", label="Memory", group="temp.mem", unit=Unit.C, path="temp.mem"))
        if "power.draw" in ok:
            add(s(key="power.draw", label="Power draw", group="power", unit=Unit.W,
                  path="power.draw", scale=0.001))
        if "power.limit" in ok:
            add(s(key="power.limit", label="Power limit", group="power", unit=Unit.W,
                  kind=Kind.STATIC, path="power.limit", scale=0.001, core=False))
        if "energy" in ok:
            add(s(key="energy", label="Energy consumed", group="energy", unit=Unit.MJ,
                  kind=Kind.COUNTER, path="energy"))
            add(s(key="power.avg", label="Average draw (from energy)", group="energy",
                  unit=Unit.W, kind=Kind.DERIVED, source="derived", path="rate:energy",
                  scale=0.001))
        if "util" in ok:
            add(s(key="util.gpu", label="GPU utilization", group="compute", unit=Unit.PCT, path="util.gpu"))
            add(s(key="util.mem", label="Memory controller util", group="compute",
                  unit=Unit.PCT, path="util.mem"))
        if "mem" in ok:
            add(s(key="mem.used", label="Memory used", group="compute", unit=Unit.BYTES, path="mem.used"))
            add(s(key="mem.total", label="Memory total", group="compute", unit=Unit.BYTES,
                  kind=Kind.STATIC, path="mem.total", core=False))
            add(s(key="mem.pct", label="Memory used %", group="compute", unit=Unit.PCT,
                  kind=Kind.DERIVED, source="derived", path="pct:mem.used:mem.total"))
        for k, lbl in (("clock.sm", "SM clock"), ("clock.mem", "Memory clock")):
            if k in ok:
                add(s(key=k, label=lbl, group="clock", unit=Unit.MHZ, path=k))
        if "clock.sm.max" in ok:
            add(s(key="clock.sm.max", label="SM clock max", group="clock", unit=Unit.MHZ,
                  kind=Kind.STATIC, path="clock.sm.max", core=False))
        if "fan" in ok:
            add(s(key="fan", label="Fan", group="fan", unit=Unit.PCT, path="fan"))
        if "pcie.tx" in ok:
            add(s(key="pcie.tx", label="PCIe TX", group="pcie", unit=Unit.BPS, path="pcie.tx", scale=1024.0))
            add(s(key="pcie.rx", label="PCIe RX", group="pcie", unit=Unit.BPS, path="pcie.rx", scale=1024.0))
        if "pcie.gen" in ok:
            add(s(key="pcie.gen", label="PCIe link gen", group="pcie", unit=Unit.COUNT, path="pcie.gen"))
            add(s(key="pcie.width", label="PCIe link width", group="pcie", unit=Unit.COUNT, path="pcie.width"))
        if "pcie.replay" in ok:
            add(s(key="pcie.replay", label="PCIe replay counter", group="pcie", unit=Unit.COUNT,
                  kind=Kind.COUNTER, path="pcie.replay"))
        if "throttle" in ok:
            add(s(key="throttle.reasons", label="Throttle reason bits", group="throttle",
                  unit=Unit.COUNT, path="throttle"))
        for k, lbl in (("viol.power", "Power-cap"), ("viol.thermal", "Thermal-throttle")):
            if k in ok:
                add(s(key=f"throttle.{k[5:]}.ns", label=f"{lbl} time", group="throttle",
                      unit=Unit.COUNT, kind=Kind.COUNTER, path=k))
                add(s(key=f"throttle.{k[5:]}.pct", label=f"{lbl} duty", group="throttle",
                      unit=Unit.PCT, kind=Kind.DERIVED, source="derived",
                      path=f"duty:throttle.{k[5:]}.ns"))
        if "ecc" in ok:
            add(s(key="health.ecc_uncorrected", label="ECC uncorrected (volatile)",
                  group="health", unit=Unit.COUNT, kind=Kind.COUNTER, path="ecc"))
        return {x.key: x for x in cat}, {"temp.core": "Temps / GPU",
                                          "temp.mem": "Temps / Memory", "fan": "Fan"}

    # ------------------------------------------------------------------ #
    def read(self, dev: DeviceInfo, series: list[Series]) -> dict[str, float | None]:
        h = dev.handle
        out: dict[str, float | None] = {}
        want = {s.path for s in series}
        keys = {s.path: s for s in series}

        def put(path: str, val):
            s = keys.get(path)
            if s is not None:
                out[s.key] = None if val is None else float(val) * s.scale

        if "temp.gpu" in want:
            put("temp.gpu", _s(h, pynvml.nvmlDeviceGetTemperature, pynvml.NVML_TEMPERATURE_GPU))
        if "temp.mem" in want:
            put("temp.mem", _s(h, pynvml.nvmlDeviceGetTemperature, 1))
        if "power.draw" in want:
            put("power.draw", _s(h, pynvml.nvmlDeviceGetPowerUsage))
        if "power.limit" in want:
            put("power.limit", _s(h, pynvml.nvmlDeviceGetPowerManagementLimit))
        if "energy" in want:
            put("energy", _s(h, pynvml.nvmlDeviceGetTotalEnergyConsumption))
        if {"util.gpu", "util.mem"} & want:
            u = _s(h, pynvml.nvmlDeviceGetUtilizationRates)
            put("util.gpu", u.gpu if u else None)
            put("util.mem", u.memory if u else None)
        if {"mem.used", "mem.total"} & want:
            m = _s(h, pynvml.nvmlDeviceGetMemoryInfo)
            put("mem.used", m.used if m else None)
            put("mem.total", m.total if m else None)
        if "clock.sm" in want:
            put("clock.sm", _s(h, pynvml.nvmlDeviceGetClockInfo, pynvml.NVML_CLOCK_SM))
        if "clock.mem" in want:
            put("clock.mem", _s(h, pynvml.nvmlDeviceGetClockInfo, pynvml.NVML_CLOCK_MEM))
        if "clock.sm.max" in want:
            put("clock.sm.max", _s(h, pynvml.nvmlDeviceGetMaxClockInfo, pynvml.NVML_CLOCK_SM))
        if "fan" in want:
            put("fan", _s(h, pynvml.nvmlDeviceGetFanSpeed))
        if "pcie.tx" in want:
            put("pcie.tx", _s(h, pynvml.nvmlDeviceGetPcieThroughput, pynvml.NVML_PCIE_UTIL_TX_BYTES))
        if "pcie.rx" in want:
            put("pcie.rx", _s(h, pynvml.nvmlDeviceGetPcieThroughput, pynvml.NVML_PCIE_UTIL_RX_BYTES))
        if "pcie.gen" in want:
            put("pcie.gen", _s(h, pynvml.nvmlDeviceGetCurrPcieLinkGeneration))
        if "pcie.width" in want:
            put("pcie.width", _s(h, pynvml.nvmlDeviceGetCurrPcieLinkWidth))
        if "pcie.replay" in want:
            put("pcie.replay", _s(h, pynvml.nvmlDeviceGetPcieReplayCounter))
        if "throttle" in want:
            put("throttle", _s(h, pynvml.nvmlDeviceGetCurrentClocksThrottleReasons))
        for k, pol in (("viol.power", pynvml.NVML_PERF_POLICY_POWER),
                       ("viol.thermal", pynvml.NVML_PERF_POLICY_THERMAL)):
            if k in want:
                v = _s(h, pynvml.nvmlDeviceGetViolationStatus, pol)
                put(k, v.violationTime if v else None)
        if "ecc" in want:
            put("ecc", _s(h, pynvml.nvmlDeviceGetTotalEccErrors,
                          pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED, pynvml.NVML_VOLATILE_ECC))
        return out


def create(cfg: dict[str, Any]) -> NvidiaBackend:
    return NvidiaBackend(cfg)
