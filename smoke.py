"""Phase-1 smoke test: backends -> identity -> engine sweep -> outbox -> ack.

Run on each box; prints what the local hardware yields and exercises the
store-and-forward path end to end without any network.
"""
import json
import os
import shutil
import sys
import tempfile
import time

from health_monitor import config
from health_monitor.backends import load_backends
from health_monitor.identity import Registry
from health_monitor.engine import Engine, Settings
from health_monitor.store.outbox import Outbox
from health_monitor.metrics.catalog import Kind

fails = []


def chk(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="hm-smoke-")
cfg = {"backends": ["auto"], "sim_devices": 2, "vllm": sys.argv[1:] }
host = os.uname().nodename.split(".")[0]

print("=== backends ===")
backends = load_backends(["auto"], cfg)
chk("at least one backend loaded", bool(backends),
    ", ".join(f"{b.vendor}:{len(b.devices())}" for b in backends))
for b in backends:
    for d in b.devices():
        print(f"     {b.vendor:12s} #{d.index} {d.model!r:40s} pci={d.pci_addr} "
              f"serial={d.serial} uuid={(d.uuid or '')[:20]} ids={d.ids}")

print("\n=== identity registry (two resolves must agree) ===")
reg = Registry(os.path.join(tmp, "gpus.json"), {})
infos = [d for b in backends for d in b.devices()]
m1, ev1 = reg.resolve(infos)
m2, ev2 = reg.resolve(infos)
chk("ids stable across resolves", m1 == m2, str(sorted(m1.values())))
chk("first resolve minted new cards", all(e.kind == "gpu_new" for e in ev1) and ev1,
    f"{len(ev1)} events")
chk("second resolve is quiet", not ev2)
chk("gpus.json written", os.path.exists(os.path.join(tmp, "gpus.json")))
for c in reg.listing():
    print(f"     {c['gpu_id']:10s} name={c['name']:12s} state={c['state']:8s} "
          f"serial={c['serial']} pci={c['pci_addr']}")

print("\n=== simulated bus renumbering ===")
import dataclasses
# handle holds ctypes pointers, so no deepcopy: rebuild without it.
moved = [dataclasses.replace(d, pci_addr=d.pci_addr[:-3] + "1.0", handle=None)
         for d in infos]                          # every card changes address
m3, ev3 = reg.resolve(moved)
chk("cards keep their ids after renumbering", m3 == m1, str(sorted(m3.values())))
chk("no card went pending", all(c["state"] == "active" for c in reg.listing()))

print("\n=== engine ===")
eng = Engine(host, backends, reg, Settings(record_interval=1.0), cfg["vllm"])
cat = eng.full_catalog()
chk("catalog built", len(cat) > 0, f"{len(cat)} series across {len(eng.devices)} devices")
chk("refs are monitor/gpu/key", all(r.count("/") == 2 and r.startswith(host + "/") for r in cat))
groups = eng.groups()
chk("groups per device", len(groups) > 0, f"{len(groups)} groups")
st = eng.statics()
chk("statics read", any(v is not None for v in st.values()),
    f"{sum(1 for v in st.values() if v is not None)}/{len(st)}")

ts1, v1 = eng._sweep()
time.sleep(1.2)
ts2, v2 = eng._sweep()
nonnull = {k: v for k, v in v2.items() if v is not None}
chk("second sweep yields values", len(nonnull) > 0,
    f"{len(nonnull)}/{len(v2)} non-null, sweep {eng._last_sweep_ms:.0f} ms")
derived = [r for r, s in cat.items() if s.kind is Kind.DERIVED and v2.get(r) is not None]
chk("some derived values computed", len(derived) > 0, f"{len(derived)} derived")
print("     sample of values:")
shown = 0
for r in sorted(nonnull):
    s = cat[r]
    if s.kind in (Kind.GAUGE, Kind.DERIVED) and shown < 14:
        print(f"       {r:44s} {nonnull[r]:14.3f} {s.unit.value}")
        shown += 1

print("\n=== outbox: record -> flush -> batch -> ack -> resend ===")
ob = Outbox(os.path.join(tmp, "outbox.db"), cat, max_bytes=50 * 1024**2, min_free=1)
ob.record(ts1, v1)
ob.record(ts2, v2)
ob.flush()
n = ob.backlog()
chk("rows landed", n == len({k for k, v in v1.items() if v is not None}) +
    len({k for k, v in v2.items() if v is not None}), f"{n} rows")
b1 = ob.next_batch(100)
chk("batch tagged", b1 is not None and len(b1[1]) == 100, f"batch {b1[0] if b1 else None}")
chk("rows are [ref, ts, value]", all(len(x) == 3 and isinstance(x[0], str) for x in b1[1]))
ob.reset_inflight()
b2 = ob.next_batch(100)
chk("reset_inflight re-queues the same rows", b2 and b2[1][0][1] == b1[1][0][1] and b2[0] != b1[0])
acked = ob.ack(b2[0])
chk("ack deletes exactly the batch", acked == 100 and ob.backlog() == n - 100,
    f"acked {acked}, backlog {ob.backlog()}")
ob.record_event(host, "test", ts2, None, {"x": 1})
ob.flush()
e = ob.next_events()
chk("events batch", e is not None and e[1][0]["kind"] == "test")
ob.ack(e[0])
chk("event acked", ob.next_events() is None)
ob.close()

print("\n=== config ===")
c = config.load("monitor", path=os.path.join(tmp, "monitor.json"))
chk("monitor config defaults id to hostname", c["id"] == host, c["id"])
chk("size parsing", config.parse_size("20G") == 20 * 1024**3 and
    config.parse_size("10%", of_path=tmp) > 0)

for b in backends:
    b.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
