"""Configuration and on-disk layout.

XDG conventions:

    ~/.config/health-monitor/        config.json per role, certs, gpus.json
    ~/.local/share/health-monitor/   sample databases, users.db, archives

Config is small and belongs where sync/backup tooling expects small
files; the sample databases are gigabytes and belong on whatever disk
has room -- `data_dir` in any role's config overrides the default, which
is how devbox1 points its manager at the 7 TB mount.

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` are honoured when set.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

APP = "health-monitor"


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def data_dir_default() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP)


def certs_dir() -> str:
    return os.path.join(config_dir(), "certs")


def path_for(role: str) -> str:
    return os.path.join(config_dir(), f"{role}.json")


# ---------------------------------------------------------------------- #
# defaults per role
# ---------------------------------------------------------------------- #
DEFAULTS: dict[str, dict[str, Any]] = {
    "monitor": {
        "id": None,                       # defaults to the hostname
        "manager": "wss://devbox1.bba:5679/ws",
        "certs": None,                    # defaults to certs_dir()
        "data_dir": None,
        "record_interval": 5.0,
        "record_exotic": False,
        "record_nic_rates": False,
        "record_nic_errors": False,
        "outbox_max": "20G",              # absolute ("20G") or percent ("10%")
        "min_free": "2G",
        "vllm": [],                       # base URLs to scrape
        "backends": ["auto"],             # or an explicit list
        "sim_devices": 0,                 # >0 adds simulated GPUs
        "gpu_names": {},                  # gpu_id -> display name
        "local_listen": "127.0.0.1:5677", # status + live ring for a local TUI
        "catchup_batch": 5000,
        "catchup_rate": 20000,            # rows/second while catching up
        "nic": False,
    },
    "manager": {
        "listen": "0.0.0.0:5679",
        "certs": None,
        "data_dir": None,
        "monitors": [],                   # allowed monitor ids (cert CNs)
        "web_clients": ["web"],           # allowed web-role CNs
        "min_free": "2G",
        "nicknames": {},                  # monitor id -> nickname
        "stale_after": 30.0,              # seconds without a frame -> stale
    },
    "web": {
        "listen": "0.0.0.0:5678",
        "https": True,
        "certs": None,
        "data_dir": None,
        "manager": "https://devbox1.bba:5679",
        "client_name": "web",             # our cert CN
        "session_hours": 72,
        "login_attempts": 8,              # per 10 minutes per address
    },
}


@dataclass
class Config:
    role: str
    values: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    def __getitem__(self, k: str) -> Any:
        return self.values[k]

    def get(self, k: str, default: Any = None) -> Any:
        return self.values.get(k, default)

    @property
    def data_dir(self) -> str:
        d = self.values.get("data_dir") or data_dir_default()
        d = os.path.expanduser(d)
        os.makedirs(os.path.join(d, self.role), exist_ok=True)
        return os.path.join(d, self.role)

    @property
    def certs(self) -> str:
        return os.path.expanduser(self.values.get("certs") or certs_dir())

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.values, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)


def load(role: str, path: str | None = None, create: bool = True) -> Config:
    """Load a role's config, filling in defaults; create it if absent."""
    if role not in DEFAULTS:
        raise ValueError(f"unknown role {role!r}")
    p = path or path_for(role)
    values = copy.deepcopy(DEFAULTS[role])
    existed = os.path.exists(p)
    if existed:
        with open(p) as fh:
            on_disk = json.load(fh)
        if not isinstance(on_disk, dict):
            raise ValueError(f"{p}: expected a JSON object")
        values.update(on_disk)
    cfg = Config(role=role, values=values, path=p)
    if role == "monitor" and not cfg.values.get("id"):
        cfg.values["id"] = os.uname().nodename.split(".")[0]
    if create and not existed:
        cfg.save()
    return cfg


# ---------------------------------------------------------------------- #
# sizes
# ---------------------------------------------------------------------- #
_SIZE_RX = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)(?:i?B)?\s*$", re.I)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


def parse_size(spec: str | int | float, *, of_path: str | None = None) -> int:
    """'20G', '512M', '10%' (of the filesystem holding of_path) -> bytes."""
    if isinstance(spec, (int, float)):
        return int(spec)
    s = str(spec).strip()
    if s.endswith("%"):
        pct = float(s[:-1])
        if not of_path:
            raise ValueError("percentage sizes need a path to measure")
        total = shutil.disk_usage(of_path).total
        return int(total * pct / 100.0)
    m = _SIZE_RX.match(s)
    if not m:
        raise ValueError(f"bad size {spec!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])
