# health-monitor

Multi-vendor GPU health monitoring for a fleet of boxes: Intel Gaudi
(HL-225 / Gaudi2), NVIDIA (NVML) and AMD (amdsmi), with a simulator for
testing. Three roles, one CA:

```
 monitor (every GPU box)        manager (one box)            web (one box)
 ┌──────────────────────┐       ┌─────────────────────┐      ┌──────────────────┐
 │ backends → engine    │ mTLS  │ archive + rollups   │ mTLS │ users, sessions  │ HTTPS
 │ → outbox → uplink ───┼──ws──▶│ per-monitor catalogs│◀─────┤ per-user profiles│◀──── browsers, TUI
 │ ◀── acks, subscribe, │       │ events, nicknames   │      │ proxies queries  │
 │     settings, rename │       │ web REST + live push│──ws─▶│ SSE fan-out      │
 └──────────────────────┘       └─────────────────────┘      └──────────────────┘
```

Monitors initiate the connection (NAT-friendly) and **store-and-forward**:
every sweep lands in a local SQLite outbox and is deleted only when the
manager acknowledges the batch after its own commit. An outage of any
length is replayed on reconnect, rate-limited, in parallel with live
data. There is no local retention: an acked row is gone.

## Quick start

On the **manager** box (devbox1 here):

```sh
git clone <repo> ~/health-monitor && cd ~/health-monitor
make venv
.venvhealth/bin/python -m health_monitor keys init-ca --server-name devbox1 --server-name devbox1.bba --server-name 192.168.2.68
.venvhealth/bin/python -m health_monitor keys new-monitor aibox1     # one per GPU box
.venvhealth/bin/python -m health_monitor keys new-web web
.venvhealth/bin/python -m health_monitor keys new-web-server devbox1 devbox1.bba 192.168.2.68 --out ~/.config/health-monitor/certs-web
make manager
```

On each **GPU box**, with the bundle the manager produced:

```sh
make venv
.venvhealth/bin/python -m health_monitor keys install monitor-aibox1.tar.gz
.venvhealth/bin/python -m health_monitor config set monitor manager '"wss://devbox1.bba:5679/ws"'
make monitor
```

On the **web** box (may be the manager box; it needs its own certs dir):

```sh
tar xzf web-web.tar.gz -C ~/.config/health-monitor/certs-web
.venvhealth/bin/python -m health_monitor config set web certs '"~/.config/health-monitor/certs-web"'
.venvhealth/bin/python -m health_monitor config set web manager '"https://devbox1.bba:5679"'
make web
```

Open `https://<web-box>:5678/`. With no users yet it shows the first-run
page that creates the **admin** account. The CA is private, so browsers
warn until it is imported: `health-monitor keys export-ca > ca.crt`.

The TUI is a client of the web server and needs an API token (burger
menu → *API token*, or `health-monitor users token <name>`):

```sh
health-monitor tui --server https://devbox1.bba:5678 --token hm_… [--insecure]
```

## Layout on disk

| what | where |
|---|---|
| config per role | `~/.config/health-monitor/{monitor,manager,web}.json` |
| certificates | `~/.config/health-monitor/certs/` (`certs-web/` for a co-hosted web server) |
| GPU identity registry | `~/.config/health-monitor/gpus.json` |
| data (outbox, archive, users, profiles) | `~/.local/share/health-monitor/<role>/` — override with `data_dir` |

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` are honoured. Sample databases are
gigabytes and do not belong in `~/.config`; point `data_dir` at the disk
with room.

## Running as a service

`make install-units` installs a `systemd --user` template; `make
enable-units ROLES="monitor"` (or `"manager web monitor"`) enables it. No
root needed — **except once**:

```sh
sudo loginctl enable-linger $USER
```

Without linger a user's systemd instance is torn down when their last
session ends, so the units stop at logout and only start again at the
next login. This is not theoretical: it is exactly what the fleet did
before linger was enabled.

## Security model

* **Mutual TLS everywhere between processes.** The manager is the CA.
  Each monitor and web server holds a client certificate; the CN is its
  identity and the OU (`monitor` / `web`) its role. One listener, one
  port, role decided by certificate. A monitor certificate cannot call
  the web API; an unregistered CN is refused at hello.
* **Users live on the web server only.** scrypt password hashes with
  per-user salts; server-side sessions (revocable); API tokens stored
  hashed. Login is throttled per address. The manager never sees a user.
* **Roles:** `admin` (users, monitor settings, nicknames, GPU renames)
  and `user` (view, own profiles, own password). Profiles are per user;
  nothing is shared.
* **Passwords are never in config.json.** Reset the admin with
  `health-monitor users passwd admin` on the web box.

## GPU identity

PCI bus numbers are not stable — adding one card moved a Gaudi2 from
`08:00.0` to `09:00.0` — and backend indices are not either. Each
physical card gets a `gpu_id` minted once (`gaudi-1`, `nv-1`, `amd-1`)
and re-matched every sweep:

1. vendor serial (Gaudi, Quadro/Tesla, Instinct)
2. vendor UUID *if genuinely unique* — every NVIDIA card; **not** consumer
   AMD, whose "UUID" is identical for every card of a model
3. model ids + VBIOS + same PCI address
4. one known card of that model missing and one unknown one appeared →
   **auto-remap**, logged as an event
5. otherwise *pending* until an admin resolves it: `health-monitor gpus map
   <gpu_id> <pci>`

Names are unique within a monitor (clashes get `-2`, `-3`); the display
is always `monitor/name`. History follows the `gpu_id`.

## What is measured

Backends catalogue only what the card actually reports; a GeForce with
no serial or an RX 7600 with no PCIe bandwidth simply has fewer series.
Measured on this fleet:

| vendor | card | series | notes |
|---|---|---|---|
| Intel Gaudi | HL-225 | ~230 | 22 hwmon temperatures, both PSU rails (12V derived V×I), energy, throttle duty, PCIe via driver ioctl (sub-sampled: mean + peak) |
| NVIDIA | RTX 8000, RTX 3090 | ~35 | temp, power/limit, energy, util, memory, clocks, fan, PCIe, throttle reasons + duty, ECC |
| AMD | RX 7600 | ~20 | edge/hotspot/mem temps, socket power, voltages, activity, clocks, fan RPM; no energy, no PCIe bandwidth on consumer Navi |
| sim | — | 17 | a slow load cycle plus noise, stable serials, for testing a manager with N monitors |

vLLM is host-level (`<monitor>/host/…`), scraped from `/metrics`, never
from the log. Firmware mailboxes are serialised, so the engine reads only
the union of what is recorded (settings) and what any viewer is currently
displaying — the subscription travels browser → web → manager → monitor.

## Store-and-forward details

* Batches are tagged in the outbox; `ack(batch)` deletes exactly those
  rows. A dropped connection clears the tags and resends; the manager's
  inserts are idempotent on (series, ts).
* Live and backlog interleave; ack is per batch, never "up to time T".
* `outbox_max` (`"20G"` or `"10%"`) bounds the outbox: past it the
  oldest **un-acked** rows are dropped and a `data_dropped` event with
  the span is forwarded, so the gap is explained rather than silent.
* Clock skew is measured at connect and shown; > 5 s is warned.
* Rollups (1 m, 1 h) carry min/max/avg and merge order-independently.
* `health-monitor reset` rotates the manager's archive; archives are
  never deleted automatically.

## Protocol

`health_monitor/proto.py`. Every hello carries `proto`; a mismatch is
refused with a clear message. Bump `PROTO` on incompatible change and
keep `MIN_PROTO` at the oldest the manager still speaks. The three
processes are deployed and updated independently.

## Development

```sh
make check          # compile
make build          # health_monitor/_build.py from git (shown in the UI)
python smoke.py     # backends → identity → engine → outbox on this box
```

The fleet this was built on: oamnode (Gaudi2 + Ubuntu 24.04, Python
3.12), aibox1 (RTX 8000), aibox2 (RTX 3090), devbox1 (RX 7600, manager +
web) — the three Fedora boxes on Python 3.14. Code stays 3.12-compatible.
