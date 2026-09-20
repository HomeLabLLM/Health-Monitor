"""vLLM: scraped from its Prometheus /metrics endpoint.

The log file is not parsed.  /metrics carries the same fields plus the
latency histograms, updates at engine-step granularity rather than on
the 10-second logger tick, and is timestamped by us inside the same sweep
that reads the sensors, so throughput and rail voltage share one clock.

vLLM is host-level, so its series live under ``<monitor>/host/``.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .catalog import Kind, Series, Unit

log = logging.getLogger("health-monitor.vllm")

_SAMPLE = re.compile(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


@dataclass
class Scrape:
    ts: float
    values: dict[str, float] = field(default_factory=dict)
    labelled: dict[str, list[tuple[dict[str, str], float]]] = field(default_factory=dict)
    buckets: dict[str, dict[float, float]] = field(default_factory=dict)

    def get(self, metric: str, labels: dict[str, str] | None = None) -> float | None:
        name = f"vllm:{metric}"
        if not labels:
            return self.values.get(name)
        for got, val in self.labelled.get(name, []):
            if all(got.get(k) == v for k, v in labels.items()):
                return val
        return None


class Scraper:
    def __init__(self, url: str, timeout: float = 4.0) -> None:
        self.url = url.rstrip("/")
        if not self.url.endswith("/metrics"):
            self.url += "/metrics"
        self.timeout = timeout
        self.up = False
        self._logged_down = False

    def scrape(self) -> Scrape | None:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if not self._logged_down:
                log.warning("vllm %s unreachable: %s", self.url, exc)
                self._logged_down = True
            self.up = False
            return None
        if self._logged_down:
            log.info("vllm %s back up", self.url)
            self._logged_down = False
        self.up = True
        return parse(text)


def parse(text: str) -> Scrape:
    s = Scrape(ts=time.time())
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        name, labelstr, raw = m.groups()
        try:
            val = float(raw)
        except ValueError:
            continue
        labels = dict(_LABEL.findall(labelstr)) if labelstr else {}
        if name.endswith("_bucket") and "le" in labels:
            try:
                le = float(labels["le"])
            except ValueError:
                le = float("inf")
            s.buckets.setdefault(name[:-len("_bucket")], {})[le] = val
            continue
        s.values.setdefault(name, val)
        s.labelled.setdefault(name, []).append((labels, val))
    return s


def gauges(series: list[Series], scrape: Scrape | None) -> dict[str, float | None]:
    """Plain gauge/counter series read straight from a scrape."""
    out: dict[str, float | None] = {}
    for s in series:
        if scrape is None:
            out[s.key] = None
            continue
        metric, _, sel = s.path.partition("|")
        labels = dict(p.split("=", 1) for p in sel.split(",") if "=" in p)
        val = scrape.get(metric, labels or None)
        out[s.key] = None if val is None else val * s.scale
    return out


def tag_for(url: str) -> str:
    m = re.search(r"//([^/]+)", url)
    host = m.group(1) if m else url
    return re.sub(r"[^A-Za-z0-9]+", "_", host).strip("_")


def series_for(url: str) -> list[Series]:
    """Catalog for one endpoint.  Local keys are ``vllm.<tag>.<name>``."""
    tag = tag_for(url)
    v = f"vllm.{tag}"
    s = lambda **kw: Series(source="vllm", **kw)  # noqa: E731
    out = [
        s(key=f"{v}.gen_tps", label=f"{tag} generation", group="vllm.tput", unit=Unit.TOKPS,
          kind=Kind.DERIVED, path="rate:generation_tokens_total"),
        s(key=f"{v}.prompt_tps", label=f"{tag} prompt", group="vllm.tput", unit=Unit.TOKPS,
          kind=Kind.DERIVED, path="rate:prompt_tokens_total"),
        s(key=f"{v}.running", label=f"{tag} running reqs", group="vllm.queue", unit=Unit.COUNT,
          path="num_requests_running"),
        s(key=f"{v}.waiting", label=f"{tag} waiting reqs", group="vllm.queue", unit=Unit.COUNT,
          path="num_requests_waiting"),
        s(key=f"{v}.waiting_capacity", label=f"{tag} waiting (capacity)", group="vllm.queue",
          unit=Unit.COUNT, path="num_requests_waiting_by_reason|reason=capacity"),
        s(key=f"{v}.waiting_deferred", label=f"{tag} waiting (deferred)", group="vllm.queue",
          unit=Unit.COUNT, path="num_requests_waiting_by_reason|reason=deferred"),
        s(key=f"{v}.preemptions", label=f"{tag} preemptions", group="vllm.queue", unit=Unit.COUNT,
          kind=Kind.COUNTER, path="num_preemptions_total"),
        s(key=f"{v}.kv_pct", label=f"{tag} KV cache", group="vllm.cache", unit=Unit.PCT,
          path="kv_cache_usage_perc", scale=100.0),
        s(key=f"{v}.prefix_hit_win", label=f"{tag} prefix hit (windowed)", group="vllm.cache",
          unit=Unit.PCT, kind=Kind.DERIVED, path="ratewin:prefix_cache_hits_total:prefix_cache_queries_total"),
        s(key=f"{v}.prefix_hit_life", label=f"{tag} prefix hit (lifetime)", group="vllm.cache",
          unit=Unit.PCT, kind=Kind.DERIVED, path="ratio:prefix_cache_hits_total:prefix_cache_queries_total"),
        s(key=f"{v}.mm_hit_win", label=f"{tag} MM cache hit (windowed)", group="vllm.cache",
          unit=Unit.PCT, kind=Kind.DERIVED, path="ratewin:mm_cache_hits_total:mm_cache_queries_total"),
        s(key=f"{v}.iter_tokens", label=f"{tag} tokens/step", group="vllm.batch", unit=Unit.COUNT,
          kind=Kind.DERIVED, path="havg:iteration_tokens_total",
          note="1.0 means no batching -- one token per engine step"),
    ]
    for metric, label in (("time_to_first_token_seconds", "TTFT"),
                          ("inter_token_latency_seconds", "inter-token"),
                          ("e2e_request_latency_seconds", "e2e latency"),
                          ("request_queue_time_seconds", "queue time"),
                          ("request_prefill_time_seconds", "prefill"),
                          ("request_decode_time_seconds", "decode"),
                          ("request_inference_time_seconds", "inference")):
        out.append(s(key=f"{v}.{metric}.avg", label=f"{tag} {label} avg", group="vllm.latency",
                     unit=Unit.SEC, kind=Kind.DERIVED, path=f"havg:{metric}"))
        for q in (50, 90, 99):
            out.append(s(key=f"{v}.{metric}.p{q}", label=f"{tag} {label} p{q}", group="vllm.latency",
                         unit=Unit.SEC, kind=Kind.DERIVED, path=f"hquant:{metric}:{q}"))
    return out


GROUP_LABELS = {
    "vllm.tput": "vLLM / Throughput", "vllm.queue": "vLLM / Queue",
    "vllm.cache": "vLLM / Cache", "vllm.latency": "vLLM / Latency",
    "vllm.batch": "vLLM / Batch",
}
