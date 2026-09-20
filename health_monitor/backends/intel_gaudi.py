"""Intel Gaudi backend (Habana HL-225 / Gaudi2) via libhlml + hwmon.

This wraps the readers and catalog that the single-box version of this
tool was built on; the measurements and their costs are documented in
README 'Polling cost'.  Keys lose their old ``devN.`` prefix -- the engine
now prefixes with the registry's gpu_id instead -- but are otherwise the
same, so archives recorded by the old tool can still be read by name.

Identity: ``hlml_device_get_serial`` gives a real serial (AO04008041 on
oamnode), which is the registry's first choice.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import threading
import time
from typing import Any

from ..metrics import sensors
from ..metrics.catalog import Kind, Series, Unit
from .base import Backend, BackendUnavailable, DeviceInfo

log = logging.getLogger("health-monitor.gaudi")

ACCEL_GLOB = "/sys/class/accel/accel*"
VERSION_MAX_LEN = 64


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _strcall(lib, name: str, handle) -> str | None:
    buf = ctypes.create_string_buffer(VERSION_MAX_LEN)
    fn = getattr(lib, name, None)
    if fn is None:
        return None
    rc = fn(handle, buf, VERSION_MAX_LEN)
    return buf.value.decode(errors="replace") if rc == 0 else None


class GaudiBackend(Backend):
    vendor = "intel_gaudi"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg)
        from ..hlml import Hlml, HlmlError, LIB_PATH
        if not os.path.exists(LIB_PATH):
            raise BackendUnavailable(f"{LIB_PATH} not present")
        try:
            self.hlml = Hlml()
        except (HlmlError, OSError) as exc:
            raise BackendUnavailable(f"hlml init failed: {exc}") from exc
        from .. import hlml_ext
        hlml_ext.install()
        self._devs: list[DeviceInfo] = []
        accels = sorted(glob.glob(ACCEL_GLOB))
        for d in self.hlml.devices:
            accel = next((a for a in accels if a.endswith(f"accel{d.index}")), None)
            hw = glob.glob(os.path.join(accel, "device/hwmon/hwmon*"))[0] if accel else None
            dev_dir = f"{accel}/device" if accel else None
            ids = ""
            if dev_dir:
                parts = []
                for f in ("vendor", "device", "subsystem_vendor", "subsystem_device"):
                    try:
                        parts.append(open(f"{dev_dir}/{f}").read().strip().replace("0x", ""))
                    except OSError:
                        parts.append("")
                ids = ":".join(parts)
            lib, h = self.hlml._lib, d._handle
            self._devs.append(DeviceInfo(
                vendor="intel_gaudi", index=d.index,
                model=_strcall(lib, "hlml_device_get_name", h) or "Gaudi",
                pci_addr=d.pci_addr.lower(),
                serial=_strcall(lib, "hlml_device_get_serial", h),
                uuid=_strcall(lib, "hlml_device_get_uuid", h),
                vbios=_strcall(lib, "hlml_get_firmware_spi_version", h),
                ids=ids, driver=self.hlml.driver_version(),
                handle={"dev": d, "hwmon": hw, "dev_dir": dev_dir}))
        self._pcie: _PcieSampler | None = None
        try:
            self._pcie = _PcieSampler(self.hlml)
        except Exception as exc:                     # noqa: BLE001
            log.warning("pcie throughput unavailable: %s", exc)
        self._unsupported: set[str] = set()

    def devices(self) -> list[DeviceInfo]:
        return list(self._devs)

    def shutdown(self) -> None:
        if self._pcie:
            self._pcie.shutdown()
        try:
            self.hlml.shutdown()
        except Exception:                            # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # catalog
    # ------------------------------------------------------------------ #
    def catalog(self, dev: DeviceInfo):
        hw = dev.handle["hwmon"]
        out: list[Series] = []
        if hw:
            out += self._hwmon_series(dev.index, hw)
        else:
            log.warning("gaudi %d: no hwmon node", dev.index)
        out += _hlml_series()
        out += _sysfs_series()
        if self._pcie is not None:
            out += _pcie_series()
        return {s.key: s for s in out}, dict(sensors.TEMP_GROUP_LABELS)

    def _hwmon_series(self, idx: int, hw: str) -> list[Series]:
        out: list[Series] = []
        temps = _channels(hw, "temp%d_input")
        known = {i: (lbl, grp) for i, lbl, grp in sensors.TEMPS}
        trusted = temps == sorted(known)
        if not trusted:
            log.warning("gaudi %d: %d temperature channels, expected %d -- generic labels",
                        idx, len(temps), len(known))
        for i in temps:
            label, group = known[i] if trusted else (f"temp{i}", "temp.misc")
            out += [s for s in sensors.temp_series(i, label, group)
                    if os.path.exists(os.path.join(hw, s.path))]
        for i in _channels(hw, "in%d_input"):
            out += sensors.volt_series(i)
        for i in _channels(hw, "curr%d_input"):
            out += sensors.curr_series(i)
        if os.path.exists(os.path.join(hw, "power1_input")):
            out.append(Series(key="power.54v", label="54V rail draw", group="power", unit=Unit.W,
                              source="hwmon", path="power1_input", scale=0.001,
                              peak_key="power.54v.peak"))
            out.append(Series(key="power.54v.peak", label="54V rail draw (peak)", group="power",
                              unit=Unit.W, source="hwmon", path="power1_input_highest",
                              scale=0.001, core=False))
        have = {s.path for s in out}
        if f"in{sensors.RAIL_12V_VOLT}_input" in have and f"curr{sensors.RAIL_12V_CURR}_input" in have:
            out.append(Series(key="power.12v", label="12V rail draw", group="power", unit=Unit.W,
                              kind=Kind.DERIVED, source="derived",
                              path=f"mul:volt.in{sensors.RAIL_12V_VOLT}:curr.c{sensors.RAIL_12V_CURR}",
                              note="derived V x I; no direct counter exists"))
            out.append(Series(key="power.total", label="Total board draw", group="power", unit=Unit.W,
                              kind=Kind.DERIVED, source="derived", path="add:power.54v:power.12v"))
        return out

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def read(self, dev: DeviceInfo, series: list[Series]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        by_src: dict[str, list[Series]] = {}
        for s in series:
            by_src.setdefault(s.source, []).append(s)
        h = dev.handle
        if "hwmon" in by_src and h["hwmon"]:
            for s in by_src["hwmon"]:
                raw = _read_int(os.path.join(h["hwmon"], s.path))
                out[s.key] = None if raw is None else raw * s.scale
        if "hlml" in by_src:
            out.update(self._read_hlml(h["dev"], by_src["hlml"]))
        if "sysfs" in by_src and h["dev_dir"]:
            for s in by_src["sysfs"]:
                out[s.key] = _read_sysfs(os.path.join(h["dev_dir"], s.path))
        if "aer" in by_src and h["dev_dir"]:
            for s in by_src["aer"]:
                out[s.key] = _read_aer(os.path.join(h["dev_dir"], s.path))
        if "pcie" in by_src and self._pcie is not None:
            got = self._pcie.drain(dev.index)
            for s in by_src["pcie"]:
                out[s.key] = None if got is None else got.get(s.path, 0.0) * s.scale
        return out

    def _read_hlml(self, d, series: list[Series]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        keys = {s.path: s for s in series}
        want = set(keys)

        def put(path: str, val):
            s = keys.get(path)
            if s is not None:
                out[s.key] = None if val is None else float(val) * s.scale

        def guarded(what: str, fn):
            try:
                return fn()
            except Exception as exc:                 # noqa: BLE001
                if what not in self._unsupported:
                    self._unsupported.add(what)
                    log.warning("hlml %s unavailable: %s", what, exc)
                return None

        if "util_aip" in want:
            u = guarded("utilization", d.utilization)
            put("util_aip", u[0] if u else None)
        if {"mem_used", "mem_total"} & want:
            m = guarded("memory_info", d.memory_info)
            put("mem_used", m[1] if m else None)
            put("mem_total", m[0] if m else None)
        if {"power_used", "power_limit"} & want:
            p = guarded("power_info", d.power_info)
            put("power_used", p[0] if p else None)
            put("power_limit", p[1] if p else None)
        for path, name in (("energy", "energy"), ("clock_soc", "clock_soc"),
                           ("clock_soc_max", "clock_soc_max"), ("pcie_replay", "pcie_replay"),
                           ("throttle_reasons", "throttle_reasons"),
                           ("viol_power", "violation_power"), ("viol_thermal", "violation_thermal")):
            if path in want:
                put(path, guarded(name, getattr(d, name)))
        return out


# ---------------------------------------------------------------------- #
def _channels(hw: str, pattern: str) -> list[int]:
    rx = re.compile(pattern.replace("%d", r"(\d+)"))
    return sorted(int(m.group(1)) for p in os.listdir(hw) if (m := rx.fullmatch(p)))


_GTS = re.compile(r"([\d.]+)\s*GT/s")


def _read_sysfs(path: str) -> float | None:
    try:
        with open(path) as fh:
            text = fh.read().strip()
    except OSError:
        return None
    m = _GTS.search(text)
    if m:
        return float(m.group(1))
    try:
        return float(text)
    except ValueError:
        return None


def _read_aer(path: str) -> float | None:
    try:
        with open(path) as fh:
            return float(sum(int(p[1]) for line in fh
                             if len(p := line.split()) == 2 and p[1].isdigit()))
    except OSError:
        return None


def _hlml_series() -> list[Series]:
    s = lambda **kw: Series(source="hlml", **kw)  # noqa: E731
    return [
        s(key="util.aip", label="AIP utilization", group="compute", unit=Unit.PCT, path="util_aip"),
        s(key="mem.used", label="HBM used", group="compute", unit=Unit.BYTES, path="mem_used"),
        s(key="mem.total", label="HBM total", group="compute", unit=Unit.BYTES,
          kind=Kind.STATIC, path="mem_total", core=False),
        Series(key="mem.pct", label="HBM used %", group="compute", unit=Unit.PCT,
               kind=Kind.DERIVED, source="derived", path="pct:mem.used:mem.total"),
        s(key="power.draw", label="Power draw (HLML)", group="power", unit=Unit.W,
          path="power_used", scale=0.001),
        s(key="power.limit", label="Power limit", group="power", unit=Unit.W,
          kind=Kind.STATIC, path="power_limit", scale=0.001, core=False),
        s(key="energy", label="Energy consumed", group="energy", unit=Unit.MJ,
          kind=Kind.COUNTER, path="energy", note="cumulative; rate gives true average watts"),
        Series(key="power.avg", label="Average draw (from energy)", group="energy", unit=Unit.W,
               kind=Kind.DERIVED, source="derived", path="rate:energy", scale=0.001),
        s(key="clock.soc", label="SOC clock", group="clock", unit=Unit.MHZ, path="clock_soc"),
        s(key="clock.soc.max", label="SOC clock max", group="clock", unit=Unit.MHZ,
          kind=Kind.STATIC, path="clock_soc_max", core=False),
        s(key="pcie.replay", label="PCIe replay counter", group="pcie", unit=Unit.COUNT,
          kind=Kind.COUNTER, path="pcie_replay"),
        s(key="throttle.power.ns", label="Power-cap time", group="throttle", unit=Unit.COUNT,
          kind=Kind.COUNTER, path="viol_power"),
        Series(key="throttle.power.pct", label="Power-capped duty", group="throttle", unit=Unit.PCT,
               kind=Kind.DERIVED, source="derived", path="duty:throttle.power.ns"),
        s(key="throttle.thermal.ns", label="Thermal-throttle time", group="throttle",
          unit=Unit.COUNT, kind=Kind.COUNTER, path="viol_thermal"),
        Series(key="throttle.thermal.pct", label="Thermal-throttle duty", group="throttle",
               unit=Unit.PCT, kind=Kind.DERIVED, source="derived", path="duty:throttle.thermal.ns"),
        s(key="throttle.reasons", label="Throttle reason bits", group="throttle",
          unit=Unit.COUNT, path="throttle_reasons"),
    ]


def _sysfs_series() -> list[Series]:
    s = lambda **kw: Series(**kw)  # noqa: E731
    return [
        s(key="pcie.gen", label="PCIe link speed", group="pcie", unit=Unit.GTS,
          source="sysfs", path="current_link_speed"),
        s(key="pcie.width", label="PCIe link width", group="pcie", unit=Unit.COUNT,
          source="sysfs", path="current_link_width"),
        s(key="health.resets", label="Hard reset count", group="health", unit=Unit.COUNT,
          kind=Kind.COUNTER, source="sysfs", path="hard_reset_cnt"),
        s(key="health.aer_corr", label="AER correctable", group="health", unit=Unit.COUNT,
          kind=Kind.COUNTER, source="aer", path="aer_dev_correctable"),
        s(key="health.aer_fatal", label="AER fatal", group="health", unit=Unit.COUNT,
          kind=Kind.COUNTER, source="aer", path="aer_dev_fatal"),
        s(key="health.aer_nonfatal", label="AER non-fatal", group="health", unit=Unit.COUNT,
          kind=Kind.COUNTER, source="aer", path="aer_dev_nonfatal"),
    ]


def _pcie_series() -> list[Series]:
    s = lambda **kw: Series(source="pcie", **kw)  # noqa: E731
    return [
        s(key="pcie.tx", label="PCIe TX", group="pcie", unit=Unit.BPS, path="tx", peak_key="pcie.tx.peak"),
        s(key="pcie.rx", label="PCIe RX", group="pcie", unit=Unit.BPS, path="rx", peak_key="pcie.rx.peak"),
        s(key="pcie.tx.peak", label="PCIe TX peak", group="pcie", unit=Unit.BPS, path="tx_peak",
          note="highest sub-sample in the interval"),
        s(key="pcie.rx.peak", label="PCIe RX peak", group="pcie", unit=Unit.BPS, path="rx_peak",
          note="highest sub-sample in the interval"),
    ]


class _PcieSampler:
    """0.5 s sub-sampling of the driver's bursty instantaneous PCIe rate,
    drained once per sweep as mean + peak.  See README 'PCIe throughput'."""

    INTERVAL = 0.5

    def __init__(self, hlml) -> None:
        from ..poller import PcieReader
        self._reader = PcieReader(hlml)
        self._lock = threading.Lock()
        self._acc: dict[int, list[float]] = {}
        self._last: dict[int, dict[str, float]] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, name="gaudi-pcie", daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            try:
                got = self._reader.sweep()
            except Exception:                        # noqa: BLE001
                continue
            with self._lock:
                for dev, (tx, rx) in got.items():
                    a = self._acc.get(dev)
                    if a is None:
                        self._acc[dev] = [tx, rx, 1, tx, rx]
                    else:
                        a[0] += tx; a[1] += rx; a[2] += 1
                        a[3] = max(a[3], tx); a[4] = max(a[4], rx)

    def drain(self, dev: int) -> dict[str, float] | None:
        with self._lock:
            a = self._acc.pop(dev, None)
        if a is None:
            return self._last.get(dev)
        got = {"tx": a[0] / a[2], "rx": a[1] / a[2], "tx_peak": a[3], "rx_peak": a[4]}
        self._last[dev] = got
        return got

    def shutdown(self) -> None:
        self._stop.set()
        self._t.join(timeout=2.0)
        self._reader.shutdown()


def create(cfg: dict[str, Any]) -> GaudiBackend:
    return GaudiBackend(cfg)
