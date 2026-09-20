"""Wire protocol between monitor, manager and web server.

One TLS listener on the manager; the client's certificate says who it
is (CN) and what it is (OU = ``monitor`` or ``web``).  Monitors speak
JSON over a persistent WebSocket; web servers use HTTPS for queries and
the same WebSocket for live push.

Every ``hello`` carries ``proto``.  The three processes are deployed and
updated independently, so a mismatch is refused with a clear message
rather than misparsed.  Bump PROTO on any incompatible change and keep
``MIN_PROTO`` at the oldest version the manager still understands.
"""

from __future__ import annotations

import json
from typing import Any

PROTO = 1
MIN_PROTO = 1

# monitor -> manager
HELLO = "hello"            # {proto, role, id, version, gpus, catalog, groups, settings, clock}
SAMPLES = "samples"        # {batch, rows: [[ref, ts, value], ...], live: bool}
EVENTS = "events"          # {rows: [{kind, ts, end?, detail}]}
GPUS = "gpus"              # {gpus: [...]}  on identity change
STATUS = "status"          # {engine: {...}, outbox: {...}}
PONG = "pong"              # {t}

# manager -> monitor
HELLO_OK = "hello_ok"      # {proto, server_time, skew, settings?, subscribe?}
REFUSE = "refuse"          # {reason}
ACK = "ack"                # {batch}
SUBSCRIBE = "subscribe"    # {refs: [...]}      union of what web viewers want
SETTINGS = "settings"      # {record_interval, ...}
RENAME = "rename"          # {gpu_id, name}
REMAP = "remap"            # {gpu_id, pci}
PING = "ping"              # {t}

# manager <-> web (WebSocket)
WATCH = "watch"            # web -> manager {refs: [...]} for live push
SAMPLE = "sample"          # manager -> web {monitor, ts, values}
MONITOR_STATE = "monitor_state"   # manager -> web {monitor, state, ...}


def encode(t: str, **body: Any) -> str:
    body["t"] = t
    return json.dumps(body, allow_nan=False, separators=(",", ":"))


def decode(text: str) -> dict:
    msg = json.loads(text)
    if not isinstance(msg, dict) or "t" not in msg:
        raise ValueError("frame is not a typed object")
    return msg


class ProtocolError(RuntimeError):
    pass


def check_proto(msg: dict) -> int:
    p = msg.get("proto")
    if not isinstance(p, int):
        raise ProtocolError("hello without a proto version")
    if p < MIN_PROTO:
        raise ProtocolError(f"peer proto {p} is older than the minimum {MIN_PROTO}; upgrade it")
    if p > PROTO:
        raise ProtocolError(f"peer proto {p} is newer than this side's {PROTO}; upgrade this side")
    return p
