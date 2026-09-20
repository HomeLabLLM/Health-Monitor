"""Simulated GPUs.

Plausible sensors driven by a slow load cycle plus noise, so a manager
can be exercised with eight monitors on one box and the UI demoed with
no hardware at all.  Serial numbers are stable per (hostname, index) so
the identity registry behaves exactly as it would for real cards.
"""

from __future__ import annotations

import math
import os
import random
import time
from typing import Any

from ..metrics.catalog import Kind, Series, Unit
from .base import Backend, DeviceInfo


class SimBackend(Backend):
    vendor = "sim"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg)
        n = int(self.cfg.get("sim_devices") or 2)
        host = os.uname().nodename.split(".")[0]
        self._devs = [
            DeviceInfo(vendor="sim", index=i, model="SimGPU-9000",
                       pci_addr=f"0000:{0x50 + i:02x}:00.0",
                       serial=f"SIM-{host}-{i:03d}", uuid=None,
                       vbios="sim-1.0", ids="ffff:0001:ffff:0001",
                       driver="sim", handle={"phase": random.random() * 6.28,
                                             "energy_mj": 0.0, "t": time.time(),
                                             "seed": i})
            for i in range(n)
        ]

    def devices(self) -> list[DeviceInfo]:
        return list(self._devs)

    def catalog(self, dev: DeviceInfo):
        s = lambda **kw: Series(**{"source": "sim", **kw})   # noqa: E731
        cat = [
            s(key="temp.core", label="Core", group="temp.core", unit=Unit.C, path="temp.core"),
            s(key="temp.mem", label="Memory", group="temp.mem", unit=Unit.C, path="temp.mem"),
            s(key="temp.vrm", label="VRM", group="temp.misc", unit=Unit.C, path="temp.vrm"),
            s(key="power.draw", label="Power draw", group="power", unit=Unit.W, path="power.draw"),
            s(key="power.limit", label="Power limit", group="power", unit=Unit.W,
              kind=Kind.STATIC, path="power.limit", core=False),
            s(key="energy", label="Energy consumed", group="energy", unit=Unit.MJ,
              kind=Kind.COUNTER, path="energy"),
            s(key="power.avg", label="Average draw (from energy)", group="energy",
              unit=Unit.W, kind=Kind.DERIVED, source="derived", path="rate:energy",
              scale=0.001),
            s(key="util.core", label="Core utilization", group="compute", unit=Unit.PCT, path="util.core"),
            s(key="util.mem", label="Memory utilization", group="compute", unit=Unit.PCT, path="util.mem"),
            s(key="mem.used", label="Memory used", group="compute", unit=Unit.BYTES, path="mem.used"),
            s(key="mem.total", label="Memory total", group="compute", unit=Unit.BYTES,
              kind=Kind.STATIC, path="mem.total", core=False),
            s(key="mem.pct", label="Memory used %", group="compute", unit=Unit.PCT,
              kind=Kind.DERIVED, source="derived", path="pct:mem.used:mem.total"),
            s(key="clock.core", label="Core clock", group="clock", unit=Unit.MHZ, path="clock.core"),
            s(key="fan", label="Fan", group="fan", unit=Unit.PCT, path="fan"),
            s(key="pcie.tx", label="PCIe TX", group="pcie", unit=Unit.BPS, path="pcie.tx"),
            s(key="pcie.rx", label="PCIe RX", group="pcie", unit=Unit.BPS, path="pcie.rx"),
            s(key="volt.core", label="Core voltage", group="volt.supply", unit=Unit.V,
              path="volt.core", core=False),
        ]
        return {x.key: x for x in cat}, {"temp.core": "Temps / Core",
                                          "temp.mem": "Temps / Memory",
                                          "fan": "Fan"}

    def _state(self, dev: DeviceInfo) -> dict[str, float]:
        h = dev.handle
        now = time.time()
        # A 10-minute load cycle, offset per device, with jitter.
        load = 0.5 + 0.5 * math.sin(now / 600.0 * 2 * math.pi + h["phase"])
        load = min(1.0, max(0.0, load + random.gauss(0, 0.04)))
        dt = now - h["t"]
        h["t"] = now
        power = 60 + 340 * load + random.gauss(0, 3)
        h["energy_mj"] += power * 1000.0 * dt
        return {
            "temp.core": 34 + 42 * load + random.gauss(0, 0.4),
            "temp.mem": 36 + 38 * load + random.gauss(0, 0.4),
            "temp.vrm": 40 + 30 * load + random.gauss(0, 0.6),
            "power.draw": power,
            "power.limit": 400.0,
            "energy": h["energy_mj"],
            "util.core": 100 * load + random.gauss(0, 2),
            "util.mem": 60 * load + random.gauss(0, 2),
            "mem.used": (8 + 56 * load) * 1024**3,
            "mem.total": 80 * 1024**3,
            "clock.core": 900 + 900 * load + random.gauss(0, 15),
            "fan": min(100, 20 + 75 * load),
            "pcie.tx": max(0.0, 2e6 * load + random.gauss(0, 3e5)),
            "pcie.rx": max(0.0, 8e6 * load + random.gauss(0, 1e6)),
            "volt.core": 0.72 + 0.18 * load,
        }

    def read(self, dev: DeviceInfo, series: list[Series]) -> dict[str, float | None]:
        st = self._state(dev)
        return {s.key: st.get(s.path) for s in series}


def create(cfg: dict[str, Any]) -> SimBackend:
    return SimBackend(cfg)
