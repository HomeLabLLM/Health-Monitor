"""Stable GPU identity across reboots and bus renumbering.

PCI bus numbers are not stable: adding one card moved a Gaudi2 from
08:00.0 to 09:00.0 on this very box.  Nor are backend indices.  So each
physical card gets a ``gpu_id`` minted once and kept in ``gpus.json``,
and every sweep re-matches what the backends report against it:

    1. vendor serial            (Gaudi, Quadro/Tesla, Instinct)
    2. vendor UUID if unique    (every NVIDIA card; not consumer AMD)
    3. model ids + VBIOS + same PCI address
    4. model ids + VBIOS, and exactly one known card of that model is
       missing while exactly one unknown one appeared  -> auto-remap,
       recorded as an event
    5. otherwise the card is *pending*: it is monitored under a fresh
       id, flagged in the UI, and an admin resolves it with
       `health-monitor gpus map`.

History stays attached to the gpu_id, so a remap never loses data and a
mistaken one can be corrected.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

from .backends.base import DeviceInfo

log = logging.getLogger("health-monitor.identity")


@dataclass
class Card:
    gpu_id: str
    name: str
    vendor: str
    model: str
    serial: str | None = None
    uuid: str | None = None
    ids: str = ""
    vbios: str | None = None
    pci_addr: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    state: str = "active"        # active | missing | pending
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    kind: str                     # gpu_remap | gpu_new | gpu_missing | gpu_pending
    ts: float
    detail: dict = field(default_factory=dict)


class Registry:
    def __init__(self, path: str, names: dict[str, str] | None = None) -> None:
        self.path = path
        self.names = dict(names or {})       # config overrides: gpu_id -> name
        self.cards: dict[str, Card] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as fh:
                raw = json.load(fh)
            for gid, d in raw.get("gpus", {}).items():
                self.cards[gid] = Card(**{k: d.get(k) for k in Card.__dataclass_fields__
                                          if k in d} | {"gpu_id": gid})
        except (OSError, ValueError, TypeError) as exc:
            log.error("gpus.json unreadable (%s); starting empty", exc)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"gpus": {g: c.as_dict() for g, c in self.cards.items()}},
                      fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    def _mint(self, dev: DeviceInfo) -> str:
        prefix = dev.short_vendor
        n = 1
        while f"{prefix}-{n}" in self.cards:
            n += 1
        return f"{prefix}-{n}"

    def _unique_name(self, wanted: str, own: str) -> str:
        """Names are unique within a monitor; clashes get -2, -3, ..."""
        taken = {c.name for g, c in self.cards.items() if g != own}
        if wanted not in taken:
            return wanted
        n = 2
        while f"{wanted}-{n}" in taken:
            n += 1
        return f"{wanted}-{n}"

    # ------------------------------------------------------------------ #
    def resolve(self, devices: list[DeviceInfo]) -> tuple[dict[int, str], list[Event]]:
        """Map each device (by index in the list) to a gpu_id."""
        now = time.time()
        events: list[Event] = []
        result: dict[int, str] = {}
        unmatched: list[int] = []
        claimed: set[str] = set()

        # Passes 1-3: strong identity, then exact slot.
        for i, dev in enumerate(devices):
            gid = self._match_strong(dev, claimed) or self._match_slot(dev, claimed)
            if gid:
                result[i] = gid
                claimed.add(gid)
            else:
                unmatched.append(i)

        # Pass 4: one-for-one remap of an unidentifiable card that moved.
        for i in list(unmatched):
            dev = devices[i]
            cands = [g for g, c in self.cards.items()
                     if g not in claimed and c.ids == dev.ids and c.vbios == dev.vbios
                     and c.vendor == dev.vendor]
            if len(cands) == 1 and self._one_new_of_model(devices, unmatched, dev):
                gid = cands[0]
                old = self.cards[gid].pci_addr
                self.cards[gid].pci_addr = dev.pci_addr
                self.cards[gid].note = f"auto-remapped from {old}"
                events.append(Event("gpu_remap", now, {"gpu_id": gid, "from": old,
                                                       "to": dev.pci_addr, "auto": True}))
                log.warning("gpu %s moved %s -> %s (auto-remap)", gid, old, dev.pci_addr)
                result[i] = gid
                claimed.add(gid)
                unmatched.remove(i)

        # Pass 5: genuinely new or ambiguous -> fresh id, pending if ambiguous.
        for i in unmatched:
            dev = devices[i]
            gid = self._mint(dev)
            ambiguous = any(c.ids == dev.ids and c.vendor == dev.vendor and c.state == "missing"
                            for g, c in self.cards.items() if g not in claimed)
            card = Card(gpu_id=gid, name=self._unique_name(self.names.get(gid, gid), gid),
                        vendor=dev.vendor, model=dev.model, serial=dev.serial, uuid=dev.uuid,
                        ids=dev.ids, vbios=dev.vbios, pci_addr=dev.pci_addr,
                        first_seen=now, last_seen=now,
                        state="pending" if ambiguous else "active",
                        note="ambiguous: resolve with 'health-monitor gpus map'" if ambiguous else "")
            self.cards[gid] = card
            events.append(Event("gpu_pending" if ambiguous else "gpu_new", now,
                                {"gpu_id": gid, "pci": dev.pci_addr, "model": dev.model}))
            log.info("gpu %s: new %s at %s%s", gid, dev.model, dev.pci_addr,
                     " (PENDING)" if ambiguous else "")
            result[i] = gid
            claimed.add(gid)

        # Bookkeeping: refresh matched cards, mark absent ones missing.
        for i, gid in result.items():
            dev = devices[i]
            c = self.cards[gid]
            c.last_seen = now
            c.pci_addr = dev.pci_addr
            c.serial = c.serial or dev.serial
            c.uuid = c.uuid or dev.uuid
            if c.state == "missing":
                c.state = "active"
            wanted = self.names.get(gid)
            if wanted and wanted != c.name:
                c.name = self._unique_name(wanted, gid)
        for gid, c in self.cards.items():
            if gid not in claimed and c.state == "active":
                c.state = "missing"
                events.append(Event("gpu_missing", now, {"gpu_id": gid, "pci": c.pci_addr}))
                log.warning("gpu %s (%s) no longer present", gid, c.model)
        self.save()
        return result, events

    def _match_strong(self, dev: DeviceInfo, claimed: set[str]) -> str | None:
        for gid, c in self.cards.items():
            if gid in claimed:
                continue
            if dev.serial and c.serial == dev.serial:
                return gid
            if dev.uuid and c.uuid == dev.uuid:
                return gid
        return None

    def _match_slot(self, dev: DeviceInfo, claimed: set[str]) -> str | None:
        for gid, c in self.cards.items():
            if gid in claimed or c.vendor != dev.vendor:
                continue
            if c.pci_addr == dev.pci_addr and c.ids == dev.ids and c.vbios == dev.vbios:
                # Only for cards with no strong identity; a serial mismatch
                # at the same slot is a *different* card.
                if (dev.serial and c.serial and c.serial != dev.serial) or \
                   (dev.uuid and c.uuid and c.uuid != dev.uuid):
                    continue
                return gid
        return None

    def _one_new_of_model(self, devices, unmatched, dev: DeviceInfo) -> bool:
        same = [i for i in unmatched if devices[i].ids == dev.ids
                and devices[i].vendor == dev.vendor]
        return len(same) == 1

    # ------------------------------------------------------------------ #
    # admin operations
    # ------------------------------------------------------------------ #
    def rename(self, gpu_id: str, name: str) -> str:
        c = self.cards[gpu_id]
        c.name = self._unique_name(name, gpu_id)
        self.names[gpu_id] = name
        self.save()
        return c.name

    def remap(self, gpu_id: str, pci_addr: str) -> None:
        """Admin says: the card at pci_addr *is* gpu_id.  Any pending card
        currently at that address is folded into it."""
        c = self.cards[gpu_id]
        for other, oc in list(self.cards.items()):
            if other != gpu_id and oc.pci_addr == pci_addr and oc.state == "pending":
                del self.cards[other]
        c.pci_addr = pci_addr
        c.state = "active"
        c.note = "remapped by admin"
        self.save()

    def forget(self, gpu_id: str) -> None:
        self.cards.pop(gpu_id, None)
        self.save()

    def listing(self) -> list[dict]:
        return [c.as_dict() for c in sorted(self.cards.values(), key=lambda c: c.gpu_id)]
