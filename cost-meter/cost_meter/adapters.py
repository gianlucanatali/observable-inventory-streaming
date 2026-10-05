"""Real I/O: Confluent Metrics API, host /proc, DogStatsD and Redis."""
from __future__ import annotations

import datetime as dt
import logging

import requests
from datadog import DogStatsd

from .core import BilledLine, ConfluentUsage, CostLine, online_costs

log = logging.getLogger("cost_meter")

QUERY_URL = "https://api.telemetry.confluent.cloud/v2/metrics/cloud/query"
KAFKA = "io.confluent.kafka.server/"
FLINK = "io.confluent.flink/compute_pool_utilization/"
HISTORY = dt.timedelta(days=7)    # Metrics API retention; a stack older than this loses its earliest hours
RECENT = dt.timedelta(minutes=10)
RATE_WINDOW_MIN = 5               # bytes/hour from the last 5 one-minute points


class MetricsApiError(RuntimeError):
    pass


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class ConfluentMetrics:
    def __init__(self, key: str, secret: str, cluster_id: str, pool_id: str, session: requests.Session | None = None):
        self._auth = (key, secret)
        self._cluster, self._pool = cluster_id, pool_id
        self._s = session or requests.Session()

    def _query(self, metric: str, resource: str, rid: str, agg: str, granularity: str,
               start: dt.datetime, end: dt.datetime) -> list[float]:
        body = {"aggregations": [{"metric": metric, "agg": agg}],
                "filter": {"field": f"resource.{resource}.id", "op": "EQ", "value": rid},
                "granularity": granularity, "intervals": [f"{_iso(start)}/{_iso(end)}"], "limit": 1000}
        r = self._s.post(QUERY_URL, json=body, auth=self._auth, timeout=15)
        if r.status_code != 200:
            raise MetricsApiError(f"Metrics API {metric} ({resource} {rid}): HTTP {r.status_code}: {r.text[:300]}")
        return [float(p["value"]) for p in r.json().get("data", [])]

    def usage(self, now: dt.datetime) -> ConfluentUsage:
        end = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        hist, recent = end - HISTORY, end - RECENT
        k, f = self._cluster, self._pool
        hq = lambda m, res, rid, agg: self._query(m, res, rid, agg, "PT1H", hist, end)      # noqa: E731
        rq = lambda m, res, rid, agg: self._query(m, res, rid, agg, "PT1M", recent, end)    # noqa: E731
        in_h, out_h = hq(KAFKA + "received_bytes", "kafka", k, "SUM"), hq(KAFKA + "sent_bytes", "kafka", k, "SUM")
        stor_h = hq(KAFKA + "retained_bytes", "kafka", k, "SUM")   # API accepts only SUM: summed over partitions
        cfu_h = hq(FLINK + "cfu_minutes_consumed", "compute_pool", f, "SUM")
        in_r, out_r = rq(KAFKA + "received_bytes", "kafka", k, "SUM"), rq(KAFKA + "sent_bytes", "kafka", k, "SUM")
        stor_r = rq(KAFKA + "retained_bytes", "kafka", k, "SUM")
        cfus_r = rq(FLINK + "current_cfus", "compute_pool", f, "MAX")
        per_hour = 60.0 / RATE_WINDOW_MIN
        return ConfluentUsage(
            bytes_in_total=sum(in_h), bytes_out_total=sum(out_h),
            bytes_in_per_hour=sum(in_r[-RATE_WINDOW_MIN:]) * per_hour,
            bytes_out_per_hour=sum(out_r[-RATE_WINDOW_MIN:]) * per_hour,
            storage_byte_hours=sum(stor_h), storage_bytes_now=stor_r[-1] if stor_r else 0.0,
            cfu_minutes_total=sum(cfu_h), cfus_now=cfus_r[-1] if cfus_r else 0.0,
        )


COSTS_URL = "https://api.confluent.cloud/billing/v1/costs"


class ConfluentCosts:
    """The bill (Costs API): daily line items, updated during the day (seen about 1 h behind on 2026-10-04).
    Lines of this stack's environment are summed by line type, since `since` (UTC date)."""

    def __init__(self, key: str, secret: str, environment_id: str, session: requests.Session | None = None):
        self._auth, self._env = (key, secret), environment_id
        self._s = session or requests.Session()

    def billed(self, since: dt.date, today: dt.date) -> dict[str, BilledLine]:
        """line_type -> totals for the environment, since..today inclusive, and the quantity on today's line."""
        return self.billed_env_and_org(since, today)[0]

    def billed_env_and_org(self, since: dt.date, today: dt.date) -> tuple[dict[str, BilledLine], dict[str, BilledLine]]:
        """(this environment, whole org) line_type -> totals. The org view keeps every line, promo credit included:
        the key has BillingAdmin, which is organisation-wide."""
        params = {"start_date": since.isoformat(), "end_date": (today + dt.timedelta(days=1)).isoformat(), "page_size": 1000}
        out: dict[str, BilledLine] = {}
        org: dict[str, BilledLine] = {}
        url: str | None = COSTS_URL
        while url:
            r = self._s.get(url, params=params if url == COSTS_URL else None, auth=self._auth, timeout=20)
            if r.status_code != 200:
                raise MetricsApiError(f"Costs API: HTTP {r.status_code}: {r.text[:300]}")
            body = r.json()
            for line in body.get("data", []):
                env = ((line.get("resource") or {}).get("environment") or {}).get("id")
                today_qty = float(line.get("quantity", 0)) if line.get("start_date") == today.isoformat() else 0.0
                _add(org, line, today_qty)
                if env == self._env:   # other environments and org-level lines (promo credit) are not this stack's
                    _add(out, line, today_qty)
            url = (body.get("metadata") or {}).get("next")
        return out, org

    def org_gross_billed(self, since: dt.date, today: dt.date) -> float:
        """Organisation-wide gross usage billed at list price, before organisation promo credits.

        `amount` is the credit-adjusted payable balance and can legitimately net to a
        floating-point residual during a trial. It is unsuitable for a live view of
        all running stacks' usage, which must not display a near-zero cost while the
        resource lines have accrued charges.
        """
        _, org = self.billed_env_and_org(since, today)
        return sum(line.original for line_type, line in org.items() if line_type != "PROMO_CREDIT")


def _add(acc: dict[str, BilledLine], line: dict, today_qty: float) -> None:
    prev = acc.get(line["line_type"], BilledLine(0.0, 0.0, 0.0))
    acc[line["line_type"]] = BilledLine(prev.amount + float(line.get("amount", 0)),
                                        prev.original + float(line.get("original_amount", 0)),
                                        prev.quantity_today + today_qty)


def host_uptime_hours(path: str = "/proc/uptime") -> float:
    with open(path) as fh:
        return float(fh.read().split()[0]) / 3600.0


def host_cpu_jiffies(path: str = "/proc/stat") -> tuple[int, int]:
    """(total, idle incl. iowait) from the aggregate cpu line. /proc/stat is host-wide inside a container."""
    with open(path) as fh:
        fields = [int(x) for x in fh.readline().split()[1:]]
    return sum(fields[:8]), fields[3] + fields[4]


class DogMetrics:
    def __init__(self, host: str, port: int, constant_tags: list[str]):
        self._d = DogStatsd(host=host, port=port, constant_tags=constant_tags)

    def gauge(self, name: str, value: float, tags: list[str]) -> None:
        self._d.gauge(name, value, tags=tags)


class OnlineMeter:
    """Sample a dedicated stack cluster; totals cover this process lifetime only.

    Uses task-level allocation, not utilization or summed container limits.
    Polling misses short tasks and image-pull billing; this is not the AWS bill.
    ALB/node counts are explicit deployment inputs, not inferred from task count.
    """

    def __init__(self, ecs, cluster: str, raw: dict, alb_count: int, cache_nodes: int):
        if alb_count < 0 or cache_nodes < 0:
            raise ValueError("cost-meter: ALB and ElastiCache counts must be nonnegative")
        self.ecs, self.cluster, self.raw = ecs, cluster, raw
        self.alb_count, self.cache_nodes = alb_count, cache_nodes
        self._last = None
        self._totals: dict[str, float] = {}

    def sample(self, now: float) -> list[CostLine]:
        arns = []
        for page in self.ecs.get_paginator("list_tasks").paginate(
                cluster=self.cluster, launchType="FARGATE", desiredStatus="RUNNING"):
            arns.extend(page["taskArns"])
        arns = list(dict.fromkeys(arns))
        vcpus = memory_gb = 0.0
        for offset in range(0, len(arns), 100):
            batch = arns[offset:offset + 100]
            response = self.ecs.describe_tasks(cluster=self.cluster, tasks=batch)
            if response.get("failures") or len(response["tasks"]) != len(batch):
                raise MetricsApiError("ECS DescribeTasks returned an incomplete task inventory")
            for task in response["tasks"]:
                if task["lastStatus"] == "RUNNING":
                    vcpus += float(task["cpu"]) / 1024
                    memory_gb += float(task["memory"]) / 1024
        hours = 0.0 if self._last is None else max(0.0, now - self._last) / 3600
        lines = online_costs(self.raw, vcpus=vcpus, memory_gb=memory_gb, elapsed_hours=hours,
                             alb_count=self.alb_count, cache_nodes=self.cache_nodes)
        self._last = now
        out = []
        for line in lines:
            self._totals[line.item] = self._totals.get(line.item, 0.0) + line.usd_total
            out.append(CostLine(line.vendor, line.item, line.basis, line.usd_per_hour, self._totals[line.item]))
        return out
