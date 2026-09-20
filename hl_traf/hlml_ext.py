"""Additional libhlml calls used by the monitoring engine.

Mixed into GpuDevice rather than rewritten into hlml.py so the existing
NIC-fabric code keeps working untouched.

Each of these was probed on this stack (hl-1.24.x, fw 62.6.2) before
being wired up.  Calls that return HLML_ERROR_NOT_SUPPORTED here are
deliberately absent and served elsewhere:

    per-engine MME/TPC/IC clocks   -> not supported; only SOC works
    VRM / CTEMP temperature types  -> not supported; hwmon has all 22
    PCIe link generation and width -> not supported; PCI sysfs is free
    persistence mode               -> not supported

hlml_device_get_temperature_threshold does work but returns a uniform 90
for all four threshold types, which is useless next to hwmon's real
per-sensor *_crit values (93/125/88/80/90), so it is not used either.
"""

from __future__ import annotations

import ctypes

from .hlml import GpuDevice, HlmlError


def _u32(self: GpuDevice, fn_name: str, *args) -> int:
    val = ctypes.c_uint32()
    fn = getattr(self._lib, fn_name)
    rc = fn(self._handle, *args, ctypes.byref(val))
    if rc != 0:
        raise HlmlError(f"{fn_name}(dev {self.index}) rc={rc}")
    return int(val.value)


def _u64(self: GpuDevice, fn_name: str, *args) -> int:
    val = ctypes.c_uint64()
    fn = getattr(self._lib, fn_name)
    rc = fn(self._handle, *args, ctypes.byref(val))
    if rc != 0:
        raise HlmlError(f"{fn_name}(dev {self.index}) rc={rc}")
    return int(val.value)


class _ViolationTime(ctypes.Structure):
    _fields_ = [
        ("reference_time", ctypes.c_ulonglong),   # throttle start, us
        ("violation_time", ctypes.c_ulonglong),   # throttle duration, ns
    ]


HLML_CLOCK_SOC = 0
HLML_PERF_POLICY_POWER = 0
HLML_PERF_POLICY_THERMAL = 1


def energy(self: GpuDevice) -> int:
    """Cumulative energy in mJ since boot.

    Differentiating this gives a true average power that is immune to the
    aliasing you get from sampling instantaneous draw.
    """
    return _u64(self, "hlml_device_get_total_energy_consumption")


def clock_soc(self: GpuDevice) -> int:
    return _u32(self, "hlml_device_get_clock_info", HLML_CLOCK_SOC)


def clock_soc_max(self: GpuDevice) -> int:
    return _u32(self, "hlml_device_get_max_clock_info", HLML_CLOCK_SOC)


def pcie_replay(self: GpuDevice) -> int:
    return _u32(self, "hlml_device_get_pcie_replay_counter")


def throttle_reasons(self: GpuDevice) -> int:
    """Bitmask; 0 means nothing is currently limiting clocks."""
    return _u64(self, "hlml_device_get_current_clocks_throttle_reasons")


def _violation(self: GpuDevice, policy: int) -> int:
    v = _ViolationTime()
    rc = self._lib.hlml_device_get_violation_status(
        self._handle, policy, ctypes.byref(v))
    if rc != 0:
        raise HlmlError(f"hlml_device_get_violation_status(dev {self.index}, "
                        f"policy {policy}) rc={rc}")
    return int(v.violation_time)


def violation_power(self: GpuDevice) -> int:
    """Cumulative nanoseconds spent clamped by the power cap.

    The rate of this is a duty cycle -- 'what fraction of wall time was
    this card power-limited' -- which is far more diagnostic than the
    instantaneous throttle bitmask.
    """
    return _violation(self, HLML_PERF_POLICY_POWER)


def violation_thermal(self: GpuDevice) -> int:
    return _violation(self, HLML_PERF_POLICY_THERMAL)


def install() -> None:
    """Attach the extra readers to GpuDevice."""
    for fn in (energy, clock_soc, clock_soc_max, pcie_replay,
               throttle_reasons, violation_power, violation_thermal):
        setattr(GpuDevice, fn.__name__, fn)
