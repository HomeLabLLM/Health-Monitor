"""Backend registry and loader.

``load_backends(["auto"])`` tries every known backend and keeps the ones
whose driver is present, so a mixed box (oamnode has a Gaudi2 *and* an
NVIDIA card) loads both.  An explicit list loads only those and treats a
failure as an error rather than 'not here'.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .base import Backend, BackendUnavailable, DeviceInfo

log = logging.getLogger("health-monitor.backends")

# Order matters only for gpu_id minting on a first run; keep it stable.
KNOWN: dict[str, str] = {
    "intel_gaudi": ".intel_gaudi",
    "nvidia": ".nvidia",
    "amd": ".amd",
    "sim": ".sim",
    # reserved for later: "intel_xpu" (Arc / Data Center Max via Level Zero)
}


def load_backends(names: list[str], cfg: dict[str, Any] | None = None
                  ) -> list[Backend]:
    cfg = cfg or {}
    explicit = names and names != ["auto"]
    wanted = list(KNOWN) if not explicit else names
    out: list[Backend] = []
    for name in wanted:
        if name not in KNOWN:
            raise ValueError(f"unknown backend {name!r}; known: {', '.join(KNOWN)}")
        if name == "sim" and not explicit and not cfg.get("sim_devices"):
            continue                       # only on request
        try:
            mod = importlib.import_module(KNOWN[name], __name__)
            backend = mod.create(cfg)
        except BackendUnavailable as exc:
            if explicit:
                raise
            log.info("backend %s: not on this box (%s)", name, exc)
            continue
        except Exception as exc:                  # noqa: BLE001
            if explicit:
                raise
            log.warning("backend %s failed to initialise: %s", name, exc)
            continue
        devs = backend.devices()
        if not devs and not explicit:
            log.info("backend %s: no devices", name)
            backend.shutdown()
            continue
        log.info("backend %s: %d device(s)", name, len(devs))
        out.append(backend)
    return out


__all__ = ["Backend", "BackendUnavailable", "DeviceInfo", "load_backends", "KNOWN"]
