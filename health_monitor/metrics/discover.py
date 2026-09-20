"""Build the metric catalog by probing what this box actually exposes.

Everything measurable is catalogued so the user can graph it; whether a
series is *recorded* is a separate decision (Series.core plus the engine
settings), because the firmware mailbox is serialised and time spent
reading sensors is taken from the NIC sweeps.
"""

from __future__ import annotations

import glob
import logging
import os
import re

from dataclasses import replace

from .catalog import Group, Kind, Series, Unit
from . import sensors

log = logging.getLogger("hl-traf.catalog")


def _own(dev: int, items: list[Series]) -> list[Series]:
    """Stamp the owning device onto a builder's output."""
    return [replace(s, dev=dev) for s in items]

ACCEL_GLOB = "/sys/class/accel/accel*"

GROUP_LABELS: dict[str, str] = {
    **sensors.TEMP_GROUP_LABELS,
    "power": "Power / Rails",
    "energy": "Power / Energy",
    "volt.supply": "Voltages / Supply",
    "volt.core": "Voltages / Core rails",
    "volt.housekeeping": "Voltages / Housekeeping",
    "curr": "Currents",
    "peaks.temp": "Peaks / Temps",
    "peaks.volt": "Peaks / Voltages",
    "peaks.curr": "Peaks / Currents",
    "limits.temp": "Limits / Temps",
    "clock": "Clocks",
    "compute": "Compute",
    "pcie": "PCIe",
    "throttle": "Throttle",
    "health": "Health",
    "nic.rate": "NIC / Rates",
    "nic.err": "NIC / Error counters",
    "vllm.tput": "vLLM / Throughput",
    "vllm.queue": "vLLM / Queue",
    "vllm.cache": "vLLM / Cache",
    "vllm.latency": "vLLM / Latency",
    "vllm.batch": "vLLM / Batch",
}


def hwmon_dir(accel: str) -> str | None:
    hits = glob.glob(os.path.join(accel, "device/hwmon/hwmon*"))
    return hits[0] if hits else None


def _channels(hw: str, pattern: str) -> list[int]:
    """Indices of hwmon channels present, e.g. _channels(hw, 'temp%d_input')."""
    rx = re.compile(pattern.replace("%d", r"(\d+)"))
    out = []
    for p in os.listdir(hw):
        m = rx.fullmatch(p)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _hwmon_series(dev: int, hw: str) -> list[Series]:
    """Temperatures, voltages and currents for one device.

    The 22 temperature labels come from hl-smi's *ordering*, not from
    sysfs -- hwmon ships no *_label files here.  If the channel count
    differs from what we verified, the labels can no longer be trusted,
    so fall back to generic names instead of mislabelling.
    """
    out: list[Series] = []

    temps = _channels(hw, "temp%d_input")
    known = {i: (lbl, grp) for i, lbl, grp in sensors.TEMPS}
    trusted = temps == sorted(known)
    if not trusted:
        log.warning(
            "dev%d: %d temperature channels, expected %d -- using generic "
            "labels (firmware layout may have changed)", dev, len(temps), len(known),
        )
    for idx in temps:
        if trusted:
            label, group = known[idx]
        else:
            label, group = f"temp{idx}", "temp.misc"
        # Not every channel has a _crit (temp21/22 here) -- only advertise
        # what is actually present.
        out += [s for s in sensors.temp_series(dev, idx, label, group)
                if os.path.exists(os.path.join(hw, s.path))]

    for idx in _channels(hw, "in%d_input"):
        out += sensors.volt_series(dev, idx)
    for idx in _channels(hw, "curr%d_input"):
        out += sensors.curr_series(dev, idx)

    if os.path.exists(os.path.join(hw, "power1_input")):
        out.append(Series(
            dev=dev, key=f"dev{dev}.power.54v", label="54V rail draw", group="power",
            unit=Unit.W, kind=Kind.GAUGE, source="hwmon", path="power1_input",
            scale=0.001, core=True, peak_key=f"dev{dev}.power.54v.peak",
        ))
        out.append(Series(
            dev=dev, key=f"dev{dev}.power.54v.peak", label="54V rail draw (peak)",
            group="power", unit=Unit.W, kind=Kind.GAUGE, source="hwmon",
            path="power1_input_highest", scale=0.001, core=False,
        ))

    # hl-smi's 12V figure is V x I -- there is no hwmon power channel for it.
    have = {s.path for s in out}
    if (f"in{sensors.RAIL_12V_VOLT}_input" in have
            and f"curr{sensors.RAIL_12V_CURR}_input" in have):
        out.append(Series(
            dev=dev, key=f"dev{dev}.power.12v", label="12V rail draw", group="power",
            unit=Unit.W, kind=Kind.DERIVED, source="derived",
            path=f"mul:dev{dev}.volt.in{sensors.RAIL_12V_VOLT}"
                 f":dev{dev}.curr.c{sensors.RAIL_12V_CURR}",
            core=True, note="derived V x I; no direct counter exists",
        ))
        out.append(Series(
            dev=dev, key=f"dev{dev}.power.total", label="Total board draw", group="power",
            unit=Unit.W, kind=Kind.DERIVED, source="derived",
            path=f"add:dev{dev}.power.54v:dev{dev}.power.12v", core=True,
        ))
    return out


def _hlml_series(dev: int) -> list[Series]:
    """Device-level values read through libhlml.

    Only calls verified to work on this stack are catalogued.  Notably
    absent: per-engine MME/TPC/IC clocks, VRM/CTEMP temperature types and
    PCIe link generation/width all return HLML_ERROR_NOT_SUPPORTED here,
    so clocks come from HLML's SOC channel only and the link geometry
    comes from (free) PCI sysfs instead.
    """
    d = f"dev{dev}"
    return _own(dev, [
        Series(key=f"{d}.util.aip", label="AIP utilization", group="compute",
               unit=Unit.PCT, source="hlml", path="util_aip", core=True),
        Series(key=f"{d}.mem.used", label="HBM used", group="compute",
               unit=Unit.BYTES, source="hlml", path="mem_used", core=True),
        Series(key=f"{d}.mem.total", label="HBM total", group="compute",
               unit=Unit.BYTES, kind=Kind.STATIC, source="hlml",
               path="mem_total", core=False),
        Series(key=f"{d}.mem.pct", label="HBM used %", group="compute",
               unit=Unit.PCT, kind=Kind.DERIVED, source="derived",
               path=f"pct:{d}.mem.used:{d}.mem.total", core=True),

        Series(key=f"{d}.power.draw", label="Power draw (HLML)", group="power",
               unit=Unit.W, source="hlml", path="power_used", scale=0.001,
               core=True),
        Series(key=f"{d}.power.limit", label="Power limit", group="power",
               unit=Unit.W, kind=Kind.STATIC, source="hlml",
               path="power_limit", scale=0.001, core=False),
        Series(key=f"{d}.energy", label="Energy consumed", group="energy",
               unit=Unit.MJ, kind=Kind.COUNTER, source="hlml", path="energy",
               core=True, note="cumulative; rate gives true average watts"),
        # energy is mJ, so its rate is mJ/s == mW; scale to watts.
        Series(key=f"{d}.power.avg", label="Average draw (from energy)",
               group="energy", unit=Unit.W, kind=Kind.DERIVED,
               source="derived", path=f"rate:{d}.energy", scale=0.001,
               core=True),

        Series(key=f"{d}.clock.soc", label="SOC clock", group="clock",
               unit=Unit.MHZ, source="hlml", path="clock_soc", core=True),
        Series(key=f"{d}.clock.soc.max", label="SOC clock max", group="clock",
               unit=Unit.MHZ, kind=Kind.STATIC, source="hlml",
               path="clock_soc_max", core=False),

        # Mean over the interval, not a point sample -- the driver's
        # instantaneous rate is far too bursty to sample once per sweep.
        Series(key=f"{d}.pcie.tx", label="PCIe TX", group="pcie",
               unit=Unit.BPS, source="pcie", path="tx", core=True,
               peak_key=f"{d}.pcie.tx.peak"),
        Series(key=f"{d}.pcie.rx", label="PCIe RX", group="pcie",
               unit=Unit.BPS, source="pcie", path="rx", core=True,
               peak_key=f"{d}.pcie.rx.peak"),
        Series(key=f"{d}.pcie.tx.peak", label="PCIe TX peak", group="pcie",
               unit=Unit.BPS, source="pcie", path="tx_peak", core=True,
               note="highest sub-sample in the interval"),
        Series(key=f"{d}.pcie.rx.peak", label="PCIe RX peak", group="pcie",
               unit=Unit.BPS, source="pcie", path="rx_peak", core=True,
               note="highest sub-sample in the interval"),
        Series(key=f"{d}.pcie.replay", label="PCIe replay counter",
               group="pcie", unit=Unit.COUNT, kind=Kind.COUNTER,
               source="hlml", path="pcie_replay", core=True),

        # Cumulative nanoseconds spent throttled; the rate is a duty cycle.
        Series(key=f"{d}.throttle.power.ns", label="Power-cap time",
               group="throttle", unit=Unit.COUNT, kind=Kind.COUNTER,
               source="hlml", path="viol_power", core=True),
        Series(key=f"{d}.throttle.power.pct", label="Power-capped duty",
               group="throttle", unit=Unit.PCT, kind=Kind.DERIVED,
               source="derived", path=f"duty:{d}.throttle.power.ns", core=True),
        Series(key=f"{d}.throttle.thermal.ns", label="Thermal-throttle time",
               group="throttle", unit=Unit.COUNT, kind=Kind.COUNTER,
               source="hlml", path="viol_thermal", core=True),
        Series(key=f"{d}.throttle.thermal.pct", label="Thermal-throttle duty",
               group="throttle", unit=Unit.PCT, kind=Kind.DERIVED,
               source="derived", path=f"duty:{d}.throttle.thermal.ns",
               core=True),
        Series(key=f"{d}.throttle.reasons", label="Throttle reason bits",
               group="throttle", unit=Unit.COUNT, source="hlml",
               path="throttle_reasons", core=True),
    ])


def _sysfs_series(dev: int) -> list[Series]:
    """PCI-level attributes the kernel caches -- these cost ~0.05ms, unlike
    the hwmon channels which are ~3.6ms firmware round trips each."""
    d = f"dev{dev}"
    return _own(dev, [
        Series(key=f"{d}.pcie.gen", label="PCIe link speed", group="pcie",
               unit=Unit.GTS, source="sysfs", path="current_link_speed",
               core=True),
        Series(key=f"{d}.pcie.width", label="PCIe link width", group="pcie",
               unit=Unit.COUNT, source="sysfs", path="current_link_width",
               core=True),
        Series(key=f"{d}.health.resets", label="Hard reset count",
               group="health", unit=Unit.COUNT, kind=Kind.COUNTER,
               source="sysfs", path="hard_reset_cnt", core=True),
        Series(key=f"{d}.health.aer_corr", label="AER correctable",
               group="health", unit=Unit.COUNT, kind=Kind.COUNTER,
               source="aer", path="aer_dev_correctable", core=True),
        Series(key=f"{d}.health.aer_fatal", label="AER fatal", group="health",
               unit=Unit.COUNT, kind=Kind.COUNTER, source="aer",
               path="aer_dev_fatal", core=True),
        Series(key=f"{d}.health.aer_nonfatal", label="AER non-fatal",
               group="health", unit=Unit.COUNT, kind=Kind.COUNTER,
               source="aer", path="aer_dev_nonfatal", core=True),
    ])


def build(devices: list[int], nic_ports: dict[int, list[int]] | None = None,
          vllm_endpoints: list[str] | None = None) -> tuple[dict[str, Series],
                                                            list[Group]]:
    """Enumerate every measurable series, plus the ADD GROUP bundles."""
    series: list[Series] = []
    accels = sorted(glob.glob(ACCEL_GLOB))

    for dev in devices:
        accel = next((a for a in accels if a.endswith(f"accel{dev}")), None)
        hw = hwmon_dir(accel) if accel else None
        if hw:
            series += _hwmon_series(dev, hw)
        else:
            log.warning("dev%d: no hwmon node -- temperature/rail data unavailable", dev)
        series += _hlml_series(dev)
        series += _sysfs_series(dev)

    for dev, ports in (nic_ports or {}).items():
        for port in ports:
            p = f"dev{dev}.nic.p{port}"
            series += [
                Series(dev=dev, key=f"{p}.rx", label=f"dev{dev} port {port} RX",
                       group="nic.rate", unit=Unit.BPS, source="nic",
                       path=f"{dev}:{port}:rx", core=False),
                Series(dev=dev, key=f"{p}.tx", label=f"dev{dev} port {port} TX",
                       group="nic.rate", unit=Unit.BPS, source="nic",
                       path=f"{dev}:{port}:tx", core=False),
            ]

    for url in (vllm_endpoints or []):
        series += vllm_series(url)

    by_key = {s.key: s for s in series}
    return by_key, _groups(by_key)


def vllm_series(url: str) -> list[Series]:
    """Series scraped from a vLLM /metrics endpoint.

    vLLM's own log line is not used: /metrics carries the same fields plus
    the latency histograms, updates at engine-step granularity rather than
    on the 10s logger tick, and is timestamped by us at scrape time so it
    shares a clock with the sensor samples.
    """
    tag = _endpoint_tag(url)
    v = f"vllm.{tag}"
    s = lambda **kw: Series(source="vllm", **kw)  # noqa: E731
    out = [
        s(key=f"{v}.gen_tps", label=f"{tag} generation", group="vllm.tput",
          unit=Unit.TOKPS, kind=Kind.DERIVED, path="rate:generation_tokens_total"),
        s(key=f"{v}.prompt_tps", label=f"{tag} prompt", group="vllm.tput",
          unit=Unit.TOKPS, kind=Kind.DERIVED, path="rate:prompt_tokens_total"),
        s(key=f"{v}.running", label=f"{tag} running reqs", group="vllm.queue",
          unit=Unit.COUNT, path="num_requests_running"),
        s(key=f"{v}.waiting", label=f"{tag} waiting reqs", group="vllm.queue",
          unit=Unit.COUNT, path="num_requests_waiting"),
        s(key=f"{v}.waiting_capacity", label=f"{tag} waiting (capacity)",
          group="vllm.queue", unit=Unit.COUNT,
          path="num_requests_waiting_by_reason|reason=capacity"),
        s(key=f"{v}.waiting_deferred", label=f"{tag} waiting (deferred)",
          group="vllm.queue", unit=Unit.COUNT,
          path="num_requests_waiting_by_reason|reason=deferred"),
        s(key=f"{v}.preemptions", label=f"{tag} preemptions",
          group="vllm.queue", unit=Unit.COUNT, kind=Kind.COUNTER,
          path="num_preemptions_total"),

        s(key=f"{v}.kv_pct", label=f"{tag} KV cache", group="vllm.cache",
          unit=Unit.PCT, path="kv_cache_usage_perc", scale=100.0),
        # Lifetime hit rate barely moves (94.4% over 50M tokens here); the
        # windowed rate is the one that actually tells you something.
        s(key=f"{v}.prefix_hit_win", label=f"{tag} prefix hit (windowed)",
          group="vllm.cache", unit=Unit.PCT, kind=Kind.DERIVED,
          path="ratewin:prefix_cache_hits_total:prefix_cache_queries_total"),
        s(key=f"{v}.prefix_hit_life", label=f"{tag} prefix hit (lifetime)",
          group="vllm.cache", unit=Unit.PCT, kind=Kind.DERIVED,
          path="ratio:prefix_cache_hits_total:prefix_cache_queries_total"),
        s(key=f"{v}.mm_hit_win", label=f"{tag} MM cache hit (windowed)",
          group="vllm.cache", unit=Unit.PCT, kind=Kind.DERIVED,
          path="ratewin:mm_cache_hits_total:mm_cache_queries_total"),

        s(key=f"{v}.iter_tokens", label=f"{tag} tokens/step", group="vllm.batch",
          unit=Unit.COUNT, kind=Kind.DERIVED,
          path="havg:iteration_tokens_total",
          note="1.0 means no batching -- one token per engine step"),
    ]
    # Latency histograms: mean from sum/count, plus percentiles from buckets.
    for metric, label in (
        ("time_to_first_token_seconds", "TTFT"),
        ("inter_token_latency_seconds", "inter-token"),
        ("e2e_request_latency_seconds", "e2e latency"),
        ("request_queue_time_seconds", "queue time"),
        ("request_prefill_time_seconds", "prefill"),
        ("request_decode_time_seconds", "decode"),
        ("request_inference_time_seconds", "inference"),
    ):
        out.append(s(key=f"{v}.{metric}.avg", label=f"{tag} {label} avg",
                     group="vllm.latency", unit=Unit.SEC, kind=Kind.DERIVED,
                     path=f"havg:{metric}"))
        for q in (50, 90, 99):
            out.append(s(key=f"{v}.{metric}.p{q}", label=f"{tag} {label} p{q}",
                         group="vllm.latency", unit=Unit.SEC,
                         kind=Kind.DERIVED, path=f"hquant:{metric}:{q}"))
    return out


def _endpoint_tag(url: str) -> str:
    """Short stable tag for an endpoint, used in series keys."""
    m = re.search(r"//([^/]+)", url)
    host = m.group(1) if m else url
    return re.sub(r"[^A-Za-z0-9]+", "_", host).strip("_")


def _groups(by_key: dict[str, Series]) -> list[Group]:
    """Bundle series into the ADD GROUP list, one entry per group name."""
    order: list[str] = []
    members: dict[str, list[str]] = {}
    for key, s in by_key.items():
        members.setdefault(s.group, []).append(key)
        if s.group not in order:
            order.append(s.group)
    out = []
    for name in order:
        keys = members[name]
        units = {by_key[k].unit for k in keys}
        out.append(Group(
            name=name,
            label=GROUP_LABELS.get(name, name),
            keys=sorted(keys),
            unit=units.pop() if len(units) == 1 else None,
            core=any(by_key[k].core for k in keys),
        ))
    return out
