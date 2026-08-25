"""Fabric topology: wiring cache + traffic-correlation discovery.

No Habana API exposes which internal NIC port connects to which peer, so
wiring is inferred: on a point-to-point link, GPU A port p's tx_Octets
delta equals the peer port's rx_Octets delta (within framing overhead).
Sampled while a collective workload moves traffic, then matched.

Cache file: ~/.config/hl-traf/topology.json (hand-editable).
"""

from __future__ import annotations

import json
import logging
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .hlml import Hlml

log = logging.getLogger("hl-traf")

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "hl-traf")
TOPOLOGY_FILE = os.path.join(CONFIG_DIR, "topology.json")

QUAL_NIC_TESTS_LIB = "/opt/habanalabs/qual/lib/libNICTests.so"
QUAL_MAP_ENTRY_BYTES = 8
QUAL_MAP_PORTS = 24
QUAL_MAP_ROUTE_INTERNAL = 0xFF

LinkKey = tuple[int, int]  # (gpu, port)


@dataclass(frozen=True)
class RoutedLink:
    """External port connected to a peer through a switch/route (not direct copper)."""

    gpu_a: int
    port_a: int
    gpu_b: int
    port_b: int
    route: int


@dataclass(frozen=True)
class Link:
    gpu_a: int
    port_a: int
    gpu_b: int
    port_b: int
    score: float = 1.0

    def touches(self, gpu: int, port: int) -> bool:
        return (gpu, port) in ((self.gpu_a, self.port_a), (self.gpu_b, self.port_b))

    def normalized(self) -> tuple[LinkKey, LinkKey]:
        a, b = (self.gpu_a, self.port_a), (self.gpu_b, self.port_b)
        return (a, b) if a <= b else (b, a)


@dataclass
class Wiring:
    links: list[Link] = field(default_factory=list)
    routed_links: list[RoutedLink] = field(default_factory=list)
    discovered_at: str = ""
    method: str = ""

    def peer(self, gpu: int, port: int) -> LinkKey | None:
        for lk in self.links:
            if lk.gpu_a == gpu and lk.port_a == port:
                return (lk.gpu_b, lk.port_b)
            if lk.gpu_b == gpu and lk.port_b == port:
                return (lk.gpu_a, lk.port_a)
        return None

    def links_between(self, gpu_a: int, gpu_b: int) -> list[Link]:
        out = []
        for lk in self.links:
            if {lk.gpu_a, lk.gpu_b} == {gpu_a, gpu_b}:
                out.append(lk)
        return out

    def routed_peer(self, gpu: int, port: int) -> tuple[int, int, int] | None:
        for lk in self.routed_links:
            if lk.gpu_a == gpu and lk.port_a == port:
                return (lk.gpu_b, lk.port_b, lk.route)
            if lk.gpu_b == gpu and lk.port_b == port:
                return (lk.gpu_a, lk.port_a, lk.route)
        return None

    # ------------------------------------------------------------------ #
    def save(self, path: str = TOPOLOGY_FILE) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "version": 1,
            "discovered_at": self.discovered_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "method": self.method,
            "links": [asdict(lk) for lk in sorted(self.links, key=lambda l: l.normalized())],
            "routed_links": [asdict(lk) for lk in sorted(
                self.routed_links, key=lambda l: (l.gpu_a, l.port_a))],
        }
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        log.info("wrote %s (%d links)", path, len(self.links))

    @classmethod
    def load(cls, path: str = TOPOLOGY_FILE) -> "Wiring | None":
        if not os.path.exists(path):
            return None
        try:
            with open(path) as fh:
                payload = json.load(fh)
            links = [Link(**lk) for lk in payload.get("links", [])]
            routed = [RoutedLink(**lk) for lk in payload.get("routed_links", [])]
            return cls(
                links=links,
                routed_links=routed,
                discovered_at=payload.get("discovered_at", ""),
                method=payload.get("method", ""),
            )
        except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
            log.warning("could not load topology cache %s: %s", path, exc)
            return None


# ---------------------------------------------------------------------- #
# sampling
# ---------------------------------------------------------------------- #
class Sampler:
    """Parallel raw counter sampler used by discovery (and self-tests)."""

    def __init__(self, hlml: Hlml, ports: str = "internal"):
        self.hlml = hlml
        self.ports = ports

    def sweep(self) -> dict[LinkKey, dict[str, float]]:
        """One parallel sweep -> {(gpu,port): {rx, tx, ts}} (cumulative)."""
        out: dict[LinkKey, dict[str, float]] = {}

        def one(dev) -> dict[LinkKey, dict[str, float]]:
            res: dict[LinkKey, dict[str, float]] = {}
            for p in dev.ports:
                if self.ports == "internal" and p.external:
                    continue
                if self.ports == "external" and not p.external:
                    continue
                try:
                    c = dev.stats(p.port)
                except Exception as exc:
                    log.warning("sampler: GPU %d port %d: %s", dev.index, p.port, exc)
                    continue
                res[(dev.index, p.port)] = {
                    "rx": float(c.get("rx_Octets", 0)),
                    "tx": float(c.get("tx_Octets", 0)),
                    "ts": time.time(),
                }
            return res

        with ThreadPoolExecutor(len(self.hlml.devices)) as ex:
            for chunk in ex.map(one, self.hlml.devices):
                out.update(chunk)
        return out


# ---------------------------------------------------------------------- #
# correlation matcher
# ---------------------------------------------------------------------- #
@dataclass
class MatchReport:
    wiring: Wiring
    matched: int
    unmatched_ports: list[LinkKey]
    idle_ports: list[LinkKey]          # no measurable tx in any window
    rejected: list[tuple[LinkKey, LinkKey, float]]  # candidates cut by constraints


def match_correlation(
    tx: dict[LinkKey, list[float]],
    rx: dict[LinkKey, list[float]],
    tol: float = 0.10,
    floor_bytes: float = 4096.0,
    min_active_windows: int | None = None,
    links_per_pair: int | None = 3,
) -> MatchReport:
    """Match point-to-point links from per-window tx/rx octet deltas.

    tx[k][w] is the octets sent by port k in window w; rx likewise.
    A candidate pairing (X -> Y) is valid when Y's rx tracks X's tx
    within relative tolerance `tol` in (almost) all active windows.
    """
    keys = sorted(tx)
    windows = len(next(iter(tx.values()), []))
    if min_active_windows is None:
        # Bursts can be sparse (keepalive-style traffic); require only a
        # handful of active windows and lean on the tolerance check.
        min_active_windows = max(2, windows // 30)

    # Build scored candidates.
    candidates: list[tuple[float, float, LinkKey, LinkKey]] = []  # (-score, dev, src, dst)
    idle: list[LinkKey] = []
    for x in keys:
        active = [w for w in range(windows) if tx[x][w] >= floor_bytes]
        if len(active) < min_active_windows:
            idle.append(x)
            continue
        for y in keys:
            if y == x or y[0] == x[0]:
                continue
            ok = 0
            rel_dev = 0.0
            for w in active:
                t = tx[x][w]
                r = rx[y][w]
                # The peer may also receive traffic from no one else on a
                # P2P link, but it can receive *nothing extra*: require
                # rx >= tx*(1-tol) as well as rx <= tx*(1+tol)+slack.
                if t > 0 and abs(r - t) <= tol * t + floor_bytes / 2:
                    ok += 1
                    rel_dev += abs(r - t) / t
            score = ok / len(active)
            if score >= 0.66:
                candidates.append((-score, rel_dev / max(ok, 1), x, y))

    candidates.sort()

    used: set[LinkKey] = set()
    pair_count: dict[tuple[int, int], int] = {}
    links: list[Link] = []
    rejected: list[tuple[LinkKey, LinkKey, float]] = []

    for neg_score, _dev, x, y in candidates:
        if x in used or y in used:
            continue
        pair = (min(x[0], y[0]), max(x[0], y[0]))
        if links_per_pair is not None and pair_count.get(pair, 0) >= links_per_pair:
            rejected.append((x, y, -neg_score))
            continue
        # Consistency: prefer pairings where the reverse direction also
        # correlates (full duplex links usually carry traffic both ways,
        # but one-way is acceptable if nothing better exists).
        used.add(x)
        used.add(y)
        pair_count[pair] = pair_count.get(pair, 0) + 1
        links.append(Link(gpu_a=x[0], port_a=x[1], gpu_b=y[0], port_b=y[1], score=round(-neg_score, 3)))

    matched_ports = {p for lk in links for p in ((lk.gpu_a, lk.port_a), (lk.gpu_b, lk.port_b))}
    unmatched = [k for k in keys if k not in matched_ports and k not in idle]

    wiring = Wiring(
        links=links,
        discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        method="counter-correlation",
    )
    return MatchReport(
        wiring=wiring,
        matched=len(links),
        unmatched_ports=unmatched,
        idle_ports=idle,
        rejected=rejected,
    )


def discover(
    hlml: Hlml,
    windows: int = 8,
    interval: float = 2.0,
    tol: float = 0.10,
    floor_bytes: float = 4096.0,
    links_per_pair: int | None = 3,
    progress=None,
) -> tuple[MatchReport, dict[str, float]]:
    """Run `windows` delta windows of `interval` seconds and match."""
    sampler = Sampler(hlml, ports="internal")
    tx: dict[LinkKey, list[float]] = {}
    rx: dict[LinkKey, list[float]] = {}

    prev = sampler.sweep()
    collected = 0
    for w in range(windows):
        time.sleep(interval)
        cur = sampler.sweep()
        for k in cur:
            if k not in prev:
                continue
            dtx = max(0.0, cur[k]["tx"] - prev[k]["tx"])
            drx = max(0.0, cur[k]["rx"] - prev[k]["rx"])
            tx.setdefault(k, []).append(dtx)
            rx.setdefault(k, []).append(drx)
        prev = cur
        collected += 1
        if progress:
            active = sum(1 for k in tx if sum(tx[k]) > 0)
            progress(collected, windows, active)

    report = match_correlation(
        tx, rx, tol=tol, floor_bytes=floor_bytes, links_per_pair=links_per_pair
    )
    total_bytes = sum(sum(v) for v in tx.values())
    return report, {"total_tx_bytes": total_bytes, "windows": float(windows)}


# ---------------------------------------------------------------------- #
# static wiring from the qual stack (libNICTests.so card-location tables)
# ---------------------------------------------------------------------- #
def _elf_sections(data: bytes) -> list[dict]:
    (e_shoff,) = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, e_shnum, _ = struct.unpack_from("<HHH", data, 0x3A)
    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name, stype, _flags, addr, offset, size, link = struct.unpack_from(
            "<IIQQQQI", data, off
        )
        secs.append(
            {"name": name, "type": stype, "addr": addr, "offset": offset,
             "size": size, "link": link}
        )
    return secs


def load_qual_tables(so_path: str = QUAL_NIC_TESTS_LIB) -> dict[int, list[tuple[int, int, int]]]:
    """Extract g_card_location_N_mapping tables from libNICTests.so.

    Each table: 24 entries x 8 bytes =
        {u8 route, u8 remote_port, u16 pad, i32 remote_card}
    route == 0xFF -> direct internal copper link; otherwise the port is
    external and reaches remote_card through switch/route `route`.
    """
    with open(so_path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"\x7fELF":
        raise ValueError(f"{so_path} is not an ELF file")
    secs = _elf_sections(data)

    symtab = next((s for s in secs if s["type"] == 2), None)  # SHT_SYMTAB
    if symtab is None:
        symtab = next((s for s in secs if s["type"] == 11), None)  # SHT_DYNSYM
    if symtab is None:
        raise ValueError(f"no .symtab/.dynsym in {so_path}")
    strtab = secs[symtab["link"]]

    wanted = {f"g_card_location_{i}_mapping": i for i in range(8)}
    found: dict[int, tuple[int, int]] = {}  # loc -> (va, size)
    nsyms = symtab["size"] // 24
    for i in range(nsyms):
        st_name, _info, _other, _shndx, st_value, st_size = struct.unpack_from(
            "<IBBHQQ", data, symtab["offset"] + i * 24
        )
        if st_name == 0:
            continue
        end = data.find(b"\0", strtab["offset"] + st_name)
        name = data[strtab["offset"] + st_name:end].decode()
        if name in wanted:
            found[wanted[name]] = (st_value, st_size)

    missing = sorted(set(range(8)) - found.keys())
    if missing:
        raise ValueError(f"missing mapping symbols for locations {missing} in {so_path}")

    def va_to_off(va: int) -> int:
        for s in secs:
            if s["type"] == 1 and s["addr"] <= va < s["addr"] + s["size"]:  # PROGBITS
                return s["offset"] + (va - s["addr"])
        raise ValueError(f"no section covers VA {va:#x}")

    tables: dict[int, list[tuple[int, int, int]]] = {}
    for loc, (va, size) in found.items():
        need = QUAL_MAP_PORTS * QUAL_MAP_ENTRY_BYTES
        if size not in (0, need):
            raise ValueError(f"card_location_{loc} mapping has unexpected size {size}")
        off = va_to_off(va)
        entries = []
        for p in range(QUAL_MAP_PORTS):
            route, rport, _pad, rcard = struct.unpack_from("<BBHi", data, off + p * 8)
            entries.append((route, rport, rcard))
        tables[loc] = entries
    return tables


def wiring_from_qual(hlml) -> Wiring:
    """Build wiring for this system from the qual static tables.

    Card locations in the tables correspond to driver module IDs; hl-smi
    device indices are mapped through hlml_device_get_module_id().
    """
    tables = load_qual_tables()
    mods = {dev.index: dev.module_id() for dev in hlml.devices}
    inv = {m: idx for idx, m in mods.items()}

    links: list[Link] = []
    routed: list[RoutedLink] = []
    seen: set = set()
    for loc, entries in tables.items():
        if loc not in inv:
            continue
        for port, (route, rport, rcard) in enumerate(entries):
            if rcard not in inv:
                continue
            key = tuple(sorted(((loc, port), (rcard, rport))))
            if key in seen:
                continue
            seen.add(key)
            ga, gb = inv[loc], inv[rcard]
            if route == QUAL_MAP_ROUTE_INTERNAL:
                links.append(Link(gpu_a=ga, port_a=port, gpu_b=gb, port_b=rport, score=1.0))
            else:
                routed.append(RoutedLink(gpu_a=ga, port_a=port, gpu_b=gb, port_b=rport, route=route))

    return Wiring(
        links=links,
        routed_links=routed,
        discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        method="qual-libNICTests-static",
    )
