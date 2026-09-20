"""ctypes bindings for libhlml.so (Habana Management Library).

Verified against hl-1.24.x: thread-safe across devices when each thread
uses its own device handle. All counters are cumulative since boot.
"""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass

LIB_PATH = "/usr/lib/habanalabs/libhlml.so"

# From common/uapi/drm/habanalabs_accel.h and include/hl-smi.h
LINK_CNT_MAX = 256          # HABANA_LINK_CNT_MAX_NUM
LINK_STR_LEN = 32           # HABANA_LINK_STR_LEN
RDMA_FILENAME_LEN = LINK_STR_LEN + 2
PCI_ADDR_LEN = 19           # PCI_DOMAIN_LEN(9) + 10
PORTS_ARR_SIZE = 2          # uint64 mask array size
VERSION_MAX_LEN = 64


class HlmlError(RuntimeError):
    pass


PCI_LINK_INFO_LEN = 10


class _PciCap(ctypes.Structure):
    _fields_ = [
        ("link_speed", ctypes.c_char * PCI_LINK_INFO_LEN),
        ("link_width", ctypes.c_char * PCI_LINK_INFO_LEN),
        ("link_max_speed", ctypes.c_char * PCI_LINK_INFO_LEN),
        ("link_max_width", ctypes.c_char * PCI_LINK_INFO_LEN),
    ]


class _PciInfo(ctypes.Structure):
    _fields_ = [
        ("bus", ctypes.c_uint),
        ("bus_id", ctypes.c_char * PCI_ADDR_LEN),
        ("device", ctypes.c_uint),
        ("domain", ctypes.c_uint),
        ("pci_device_id", ctypes.c_uint),
        ("caps", _PciCap),
        ("pci_rev", ctypes.c_uint),
        ("pci_subsys_id", ctypes.c_uint),
    ]


class _NicStats(ctypes.Structure):
    _fields_ = [
        ("port", ctypes.c_uint32),
        ("str_buf", ctypes.c_char_p),
        ("val_buf", ctypes.POINTER(ctypes.c_uint64)),
        ("num", ctypes.POINTER(ctypes.c_uint32)),
    ]


class _Utilization(ctypes.Structure):
    _fields_ = [("aip", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Memory(ctypes.Structure):
    _fields_ = [
        ("free", ctypes.c_ulonglong),
        ("total", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class PortInfo:
    port: int
    external: bool


class GpuDevice:
    """One Habana AIP (GPU). Owns its hlml handle and reusable buffers."""

    def __init__(self, lib: ctypes.CDLL, handle: ctypes.c_void_p, index: int):
        self._lib = lib
        self._handle = handle
        self.index = index
        self._lock = threading.Lock()

        # Reusable, pre-allocated stats buffers (per-device; guarded by _lock).
        self._str_buf = ctypes.create_string_buffer(LINK_CNT_MAX * RDMA_FILENAME_LEN)
        self._val_buf = (ctypes.c_uint64 * LINK_CNT_MAX)()
        self._num = ctypes.c_uint32()
        self._stats = _NicStats(
            0,
            ctypes.cast(self._str_buf, ctypes.c_char_p),
            self._val_buf,
            ctypes.cast(ctypes.byref(self._num), ctypes.POINTER(ctypes.c_uint32)),
        )

        pci = _PciInfo()
        rc = lib.hlml_device_get_pci_info(handle, ctypes.byref(pci))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_pci_info failed for index {index}: rc={rc}")
        self.pci_addr: str = pci.bus_id.decode()
        self.pci_bus: int = pci.bus
        self.pci_device: int = pci.device
        self.pci_domain: int = pci.domain

        mask = (ctypes.c_uint64 * PORTS_ARR_SIZE)()
        ext_mask = (ctypes.c_uint64 * PORTS_ARR_SIZE)()
        rc = lib.hlml_get_mac_addr_info(handle, mask, ext_mask)
        if rc != 0:
            raise HlmlError(f"hlml_get_mac_addr_info failed for index {index}: rc={rc}")
        self.ports: list[PortInfo] = []
        for arr in range(PORTS_ARR_SIZE):
            m = mask[arr]
            e = ext_mask[arr]
            for bit in range(64):
                if m & (1 << bit):
                    self.ports.append(PortInfo(port=arr * 64 + bit, external=bool(e & (1 << bit))))
        self.ports.sort(key=lambda p: p.port)

    @property
    def num_ports(self) -> int:
        return len(self.ports)

    def internal_ports(self) -> list[int]:
        return [p.port for p in self.ports if not p.external]

    def external_ports(self) -> list[int]:
        return [p.port for p in self.ports if p.external]

    def netdev(self, port: int) -> str | None:
        """Linux netdev for external ports: enp<bus>s0d<port> (decimal bus)."""
        if not any(p.port == port and p.external for p in self.ports):
            return None
        return f"enp{self.pci_bus}s0d{port}"

    def link_up(self, port: int) -> bool:
        up = ctypes.c_bool(False)
        rc = self._lib.hlml_nic_get_link(self._handle, port, ctypes.byref(up))
        if rc != 0:
            raise HlmlError(f"hlml_nic_get_link(dev {self.index} port {port}) rc={rc}")
        return bool(up.value)

    def module_id(self) -> int:
        mid = ctypes.c_uint()
        rc = self._lib.hlml_device_get_module_id(self._handle, ctypes.byref(mid))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_module_id(dev {self.index}) rc={rc}")
        return int(mid.value)

    def minor_number(self) -> int:
        """Device minor: /dev/accel/accel<minor> and accel_controlD<minor>."""
        m = ctypes.c_uint()
        rc = self._lib.hlml_device_get_minor_number(self._handle, ctypes.byref(m))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_minor_number(dev {self.index}) rc={rc}")
        return int(m.value)

    def utilization(self) -> tuple[int, int]:
        """(aip_util_pct, memory_controller_util_pct)."""
        u = _Utilization()
        rc = self._lib.hlml_device_get_utilization_rates(self._handle, ctypes.byref(u))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_utilization_rates(dev {self.index}) rc={rc}")
        return int(u.aip), int(u.memory)

    def memory_info(self) -> tuple[int, int, int]:
        """(total, used, free) in bytes."""
        m = _Memory()
        rc = self._lib.hlml_device_get_memory_info(self._handle, ctypes.byref(m))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_memory_info(dev {self.index}) rc={rc}")
        return int(m.total), int(m.used), int(m.free)

    def power_info(self) -> tuple[int, int]:
        """(used_mw, limit_mw) — instantaneous draw and configured cap."""
        used = ctypes.c_uint32()
        rc = self._lib.hlml_device_get_power_usage(self._handle, ctypes.byref(used))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_power_usage(dev {self.index}) rc={rc}")
        lim = ctypes.c_uint32()
        rc = self._lib.hlml_device_get_power_management_limit(self._handle, ctypes.byref(lim))
        if rc != 0 or lim.value == 0:
            # Fall back to the default limit if the configured one is unset.
            rc = self._lib.hlml_device_get_power_management_default_limit(self._handle, ctypes.byref(lim))
            if rc != 0:
                raise HlmlError(f"hlml_device_get_power_management_limit(dev {self.index}) rc={rc}")
        return int(used.value), int(lim.value)

    def stats(self, port: int) -> dict[str, int]:
        """All cumulative NIC counters for one port (~115 ms FW round trip)."""
        with self._lock:
            self._stats.port = port
            rc = self._lib.hlml_nic_get_statistics(self._handle, ctypes.byref(self._stats))
            if rc != 0:
                raise HlmlError(f"hlml_nic_get_statistics(dev {self.index} port {port}) rc={rc}")
            n = self._num.value
            out: dict[str, int] = {}
            for i in range(n):
                raw = self._str_buf[i * RDMA_FILENAME_LEN:(i + 1) * RDMA_FILENAME_LEN]
                name = raw.split(b"\0", 1)[0].decode(errors="replace")
                if name:
                    out[name] = int(self._val_buf[i])
            return out


class Hlml:
    """Process-wide HLML context."""

    def __init__(self, lib_path: str = LIB_PATH):
        self._lib = ctypes.CDLL(lib_path)
        rc = self._lib.hlml_init()
        if rc != 0:
            raise HlmlError(f"hlml_init failed: rc={rc}")
        self._devices: list[GpuDevice] = []
        count = ctypes.c_uint()
        rc = self._lib.hlml_device_get_count(ctypes.byref(count))
        if rc != 0:
            raise HlmlError(f"hlml_device_get_count failed: rc={rc}")
        for i in range(count.value):
            handle = ctypes.c_void_p()
            rc = self._lib.hlml_device_get_handle_by_index(i, ctypes.byref(handle))
            if rc != 0:
                raise HlmlError(f"hlml_device_get_handle_by_index({i}) failed: rc={rc}")
            self._devices.append(GpuDevice(self._lib, handle, i))

    @property
    def devices(self) -> list[GpuDevice]:
        return list(self._devices)

    def driver_version(self) -> str:
        buf = ctypes.create_string_buffer(VERSION_MAX_LEN)
        rc = self._lib.hlml_get_driver_version(buf, VERSION_MAX_LEN)
        return buf.value.decode() if rc == 0 else "?"

    def nic_driver_version(self) -> str:
        buf = ctypes.create_string_buffer(VERSION_MAX_LEN)
        rc = self._lib.hlml_get_nic_driver_version(buf, VERSION_MAX_LEN)
        return buf.value.decode() if rc == 0 else "?"

    def shutdown(self) -> None:
        self._lib.hlml_shutdown()

    def __enter__(self) -> "Hlml":
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
