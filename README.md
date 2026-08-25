# hl-traf

Live NIC fabric traffic monitor for Habana Gaudi (HL-225 / Gaudi2) systems.
Polls all GPUs in parallel via `libhlml.so` (ctypes) and renders per-port
and per-GPU-pair bandwidth in a rich TUI.

![Screenshot](screenshot.png)

## Requirements

- SynapseAI / habanalabs driver stack providing `/usr/lib/habanalabs/libhlml.so`
- Python 3.11+ with `rich` and `typer` (see setup below)

## Setup

```sh
uv venv .venv
uv pip install -p .venv/bin/python rich typer
```

Run via the launcher (works from any directory):

```sh
./hl-traf --help
```

## Commands

| Command | Purpose |
|---|---|
| `hl-traf` / `hl-traf watch` | Live monitor. Default view is the 8×8 GPU-pair **matrix**; `--view table` shows one row per port. |
| `hl-traf discover` | Infer the internal NIC wiring from traffic correlation and cache it. Needs an HCCL collective running while it samples. |
| `hl-traf qual-map` | Decode the static NIC wiring tables shipped with the qual stack (no traffic needed). `--save` writes the cache. |
| `hl-traf ports` | One-shot map of ports, type (internal/external), link state, peers and netdevs. |
| `hl-traf selftest` | Validate the correlation matcher on synthetic counter traces. |

The matrix view carries a status strip under the header: per-GPU AIP
utilization (`GPU[... %]` + history sparkline), HBM usage
(`MEM[... %]` + history), and left-justified `pcie rx` / `pcie tx` rows
(`0.000 KiB/s`), all read every sweep. The PCIe
rates come from the driver's `HL_INFO_PCI_COUNTERS` ioctl on
`/dev/accel/accel_controlD<minor>` (instantaneous bytes/sec), issued as one
parallel wave with fds kept open — ~30 ms for all 8 GPUs; note libhlml's
`hlml_device_get_pcie_throughput` wrapper is both slower (opens the device
per call) and truncates the u64 counters to 32 bits, so hl-traf talks to
the ioctl directly. All rows shrink to fit narrow terminals. Meters and
values use the delta green/red color coding described below.

### Watch keys

`q` quit · `m` matrix view · `t` table view · `p` pause polling

### Useful options

- `--interval/-i` minimum seconds between sweeps (fast path sweeps in ~0.3–0.5 s)
- `--ports all|internal|external`
- `--line-rate` NIC line rate in Gb/s for utilization bars (default 100)
- `--history` sparkline length in sweeps (default 60)
- `--once` render a single primed snapshot and exit (good for logs/scripts)
- `--log-file` mirror the log panel to a file; `-v` for debug logging

## How it works

Each NIC counter read is a ~100 ms firmware query, whether it comes from
`hlml_nic_get_statistics` or from the driver's IB sysfs
(`/sys/class/infiniband/hbl_<n>/ports/<p+1>/hw_counters/<name>` — same
numbers, verified byte-identical). The firmware, however, serves queries
concurrently: all 24 ports of one GPU in parallel finish in ~110 ms, and
all **192 ports of the system (8 GPUs × 24) in one parallel wave of ~0.2 s**.

hl-traf's default backend (`sysfs`) therefore reads every polled port in a
single parallel wave per sweep: rx/tx octets for all ports, with full
rate+error counter sets rotated across GPUs (one GPU per sweep, plus a
whole-system pass every `errors_every` sweeps) so sweeps stay smooth at
~0.3–0.5 s end to end (~2–3 Hz refresh). When the sysfs tree is absent it
falls back to `libhlml.so` calls — serial per GPU, parallel across GPUs —
which costs ~2.7 s per full sweep. The header shows the active backend
(`sysfs`/`hlml`) and measured sweep time.

Rates are deltas of the cumulative `rx_Octets`/`tx_Octets` counters,
smoothed with an EWMA and kept in sparkline ring buffers. A failed read
marks only that port stale — it never kills the sweep.

### Counter-read defect guard

The NIC firmware on this stack occasionally serves a counter read that is
offset by an exact multiple of 2**32 units (observed: ±13 × 2**32 bytes ≈
±55.8 GB on rx/tx octets, ~10% of reads, on idle ports, both backends,
sequential and parallel reads alike). A naive delta turns one such read
into phantom traffic of tens of TB/s across the fabric.

hl-traf screens every rate delta against the NIC line rate (`--line-rate`):
a positive delta above what line rate can move in one window, or any delta
with the exact-2**32-multiple signature, is dropped — the port keeps its
previous baseline and reports 0 for that window. Real traffic is always
below the cap and passes untouched. Rejected reads are counted per port
(`torn_reads`) and logged with `-v`.

External ports (8, 22, 23 on this baseboard) also exist as Linux netdevs
named `enp<pci-bus>s0d<port>`; the `ports` command shows that mapping.

## Fabric topology discovery

No Habana API exposes which internal NIC port wires to which peer:
`hlml` only reports port class (internal/external), link state and
counters; `hl-smi topo` is CPU/NUMA affinity; there are no sysfs/debugfs
wiring nodes and internal ports have no netdevs to run LLDP on.

hl-traf gets the wiring from two sources:

### 1. Static qual map (no traffic needed) — `hl-traf qual-map`

`/opt/habanalabs/qual/lib/libNICTests.so` ships hardcoded wiring tables
(`g_card_location_0..7_mapping`, used by the qual `NIC_Base_Test
-test_type pairs` flow). Each table holds 24 entries of `{route,
remote_port, remote_card}`: `route == 0xFF` means a direct internal
copper link; any other value is an external port reaching `remote_card`
through switch route N. hl-traf parses the ELF symbol table, decodes the
tables, and maps card locations to this system's GPUs through
`hlml_device_get_module_id()` (same module IDs as `hl-smi -Q module_id`).

On this HLS-2H box that yields: **84 direct links = 28 GPU pairs × 3
links**, plus 12 switched routes on the external ports (8/22/23). `watch`
and `ports` fall back to this map automatically when no cache file exists.

### 2. Traffic correlation (verifies/overrides) — `hl-traf discover`

On a point-to-point link, port A's `tx_Octets` delta equals the peer port's
`rx_Octets` delta within framing overhead. `discover` samples N windows
(default 60 × 5 s), scores every cross-GPU port pairing, and greedily
assigns matches, constrained to `--links-per-pair` (default 3 — this
baseboard wires 3 links between every GPU pair: 21 internal ports / 7
peers). The result is written to `~/.config/hl-traf/topology.json`:

```json
{
  "version": 1,
  "discovered_at": "2026-08-14T20:31:29+00:00",
  "method": "qual-libNICTests-static",
  "links": [
    {"gpu_a": 0, "port_a": 3, "gpu_b": 5, "port_b": 3, "score": 1.0}
  ],
  "routed_links": [
    {"gpu_a": 0, "port_a": 8, "gpu_b": 1, "port_b": 8, "route": 1}
  ]
}
```

The file is hand-editable; `watch` and `ports` load it on startup and show
peers once present. Links carrying no traffic during sampling cannot be
matched by correlation — but the static qual map covers them.

Example:

```sh
hl-traf qual-map --save          # instant, from the qual stack tables
# and/or, while an HCCL collective is running on all 8 GPUs:
hl-traf discover --windows 20 --interval 5 -y   # verifies with real traffic
hl-traf                         # matrix shows per-pair traffic on the wiring
```

## Layout

```
hl_traf/
  hlml.py       ctypes bindings for libhlml.so (+ module IDs)
  poller.py     async per-GPU polling engine, rates, sparklines
  topology.py   wiring cache, sampler, correlation matcher,
                qual libNICTests.so table decoder (pure-python ELF parse)
  log.py        rich log-panel handler
  ui/           format helpers, table view, matrix view, live app
  cli.py        typer commands: watch (default), discover, qual-map,
                ports, selftest
```
