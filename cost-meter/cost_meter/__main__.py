"""cost-meter: real-time cost estimate per vendor and item, sent to Datadog.

Environment:
  PRICES_FILE                 path of prices.json (default /app/prices.json)
  COST_EC2_INSTANCE_TYPE      EC2 type of this host; empty = not on EC2 (AWS lines are not sent)
  COST_EBS_GB                 root volume size in GB (required with COST_EC2_INSTANCE_TYPE)
  COST_REMOTE_EC2_INSTANCE_TYPE, COST_REMOTE_EBS_GB, COST_REMOTE_PUBLIC_IPV4_COUNT
                              complete non-secret VM topology for static EC2/EBS/IPv4 estimates on Fargate
  COST_METER_API_KEY/SECRET   Confluent Cloud API key with MetricsViewer; empty = Confluent lines are not sent
  KAFKA_CLUSTER_ID, FLINK_COMPUTE_POOL_ID   required with the Confluent key
  REDIS_URL                   keeps the CPU-credit total across restarts (optional)
  DD_AGENT_HOST, DD_DOGSTATSD_PORT, STACK
  CONFLUENT_ENVIRONMENT_ID    required with the Confluent key: billed lines are those of this environment
  COST_BILLED_SINCE           first UTC date of billed totals (default 30 days ago: the environment is per stack)
  COST_EMIT_INTERVAL_S (15), COST_CONFLUENT_INTERVAL_S (60), COST_BILLED_INTERVAL_S (300)
  COST_ECS_CLUSTER            optional dedicated stack cluster (ARM Linux Fargate)
  AWS_REGION                 must match prices.json when COST_ECS_CLUSTER is set
  COST_ALB_COUNT, COST_ELASTICACHE_NODES   required deployment counts with COST_ECS_CLUSTER
  Online totals are sampled since process start; ALB LCUs, extra IPv4, transfer and image storage excluded.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping

from botocore.exceptions import BotoCoreError, ClientError

from .adapters import ConfluentCosts, ConfluentMetrics, DogMetrics, MetricsApiError, OnlineMeter, host_cpu_jiffies, host_uptime_hours
from .core import (BilledLine, CostLine, CpuSurplus, HostUsage, PricesError, aws_costs, confluent_costs, datadog_costs, ecku_cost,
                   load_prices, log, setup_logging, static_aws_costs)

REDIS_CPU_KEY = "cost:cpu_surplus_vcpu_hours"
AGGREGATE_VENDORS = ("aws", "confluent", "datadog")


def _require(env: Mapping[str, str], name: str, why: str) -> str:
    v = env.get(name, "")
    if not v:
        raise SystemExit(f"cost-meter: {name} is required {why}")
    return v


def _remote_vm_inputs(env: Mapping[str, str]) -> tuple[str, float, int] | None:
    names = ("COST_REMOTE_EC2_INSTANCE_TYPE", "COST_REMOTE_EBS_GB", "COST_REMOTE_PUBLIC_IPV4_COUNT")
    if not any(env.get(name, "") for name in names):
        return None
    instance_type, ebs_text, ipv4_text = (_require(env, name, "with the remote VM static-cost path") for name in names)
    if re.fullmatch(r"[a-z][a-z0-9]*\.[a-z0-9]+", instance_type) is None:
        raise SystemExit("cost-meter: COST_REMOTE_EC2_INSTANCE_TYPE must be an EC2 instance type such as t4g.xlarge")
    try:
        ebs_gb = float(ebs_text)
        ipv4_count = int(ipv4_text)
    except ValueError as exc:
        raise SystemExit("cost-meter: COST_REMOTE_EBS_GB must be a positive number and COST_REMOTE_PUBLIC_IPV4_COUNT a positive integer") from exc
    if not math.isfinite(ebs_gb) or ebs_gb <= 0 or ipv4_count <= 0 or str(ipv4_count) != ipv4_text:
        raise SystemExit("cost-meter: COST_REMOTE_EBS_GB must be a positive number and COST_REMOTE_PUBLIC_IPV4_COUNT a positive integer")
    return instance_type, ebs_gb, ipv4_count


def _cents_nonnegative(value: float) -> float:
    """Dashboard aggregate values are payable USD, never floating-point residue."""
    return max(0.0, round(value, 2))


def main() -> int:
    setup_logging()
    env = os.environ
    instance_type = env.get("COST_EC2_INSTANCE_TYPE", "")
    remote_vm = _remote_vm_inputs(env)
    if instance_type and remote_vm:
        raise SystemExit("cost-meter: COST_EC2_INSTANCE_TYPE and COST_REMOTE_EC2_INSTANCE_TYPE cannot be combined")
    if instance_type and env.get("ECS_CONTAINER_METADATA_URI_V4"):
        raise SystemExit("cost-meter: host costs require a VM meter, not Fargate /proc; unset COST_EC2_INSTANCE_TYPE")
    try:
        with open(env.get("PRICES_FILE", "/app/prices.json")) as fh:
            raw_prices = json.load(fh)
            prices = load_prices(raw_prices, instance_type or (remote_vm[0] if remote_vm else ""))
    except (OSError, ValueError, PricesError) as exc:
        log.error(f"cost-meter: cannot load prices: {exc}")
        return 2
    stack = _require(env, "STACK", "(tag on every cost metric)")
    metrics = DogMetrics(env.get("DD_AGENT_HOST", "localhost"), int(env.get("DD_DOGSTATSD_PORT", "8125")),
                         [f"project:dd-demo", f"stack:{stack}", "service:cost-meter"])
    emit_s = float(env.get("COST_EMIT_INTERVAL_S", "15"))
    cc_s = float(env.get("COST_CONFLUENT_INTERVAL_S", "60"))
    billed_s = float(env.get("COST_BILLED_INTERVAL_S", "300"))

    online = None
    if env.get("COST_ECS_CLUSTER"):
        import boto3
        region = _require(env, "AWS_REGION", "with COST_ECS_CLUSTER")
        if region != raw_prices["aws"]["region"]:
            raise SystemExit("cost-meter: AWS_REGION does not match prices.json region")
        online = OnlineMeter(boto3.client("ecs", region_name=region), env["COST_ECS_CLUSTER"], raw_prices,
                             int(_require(env, "COST_ALB_COUNT", "with COST_ECS_CLUSTER")),
                             int(_require(env, "COST_ELASTICACHE_NODES", "with COST_ECS_CLUSTER")))

    confluent = costs = None
    since = dt.date.today()
    if env.get("COST_METER_API_KEY"):
        confluent = ConfluentMetrics(env["COST_METER_API_KEY"], _require(env, "COST_METER_API_SECRET", "with COST_METER_API_KEY"),
                                     _require(env, "KAFKA_CLUSTER_ID", "with COST_METER_API_KEY"),
                                     _require(env, "FLINK_COMPUTE_POOL_ID", "with COST_METER_API_KEY"))
        costs = ConfluentCosts(env["COST_METER_API_KEY"], env["COST_METER_API_SECRET"],
                               _require(env, "CONFLUENT_ENVIRONMENT_ID", "with COST_METER_API_KEY"))
        # The environment is per stack, so every billed line of it is this stack's.
        since = dt.date.fromisoformat(env.get("COST_BILLED_SINCE") or (dt.date.today() - dt.timedelta(days=30)).isoformat())
    else:
        log.info("COST_METER_API_KEY not set: Confluent costs not metered")

    redis_client = None
    cpu = None
    ebs_gb = 0.0
    if instance_type:
        ebs_gb = float(_require(env, "COST_EBS_GB", "with COST_EC2_INSTANCE_TYPE"))
        start = 0.0
        if env.get("REDIS_URL"):
            import redis
            redis_client = redis.Redis.from_url(env["REDIS_URL"], decode_responses=True, socket_timeout=2)
            start = float(redis_client.get(REDIS_CPU_KEY) or 0.0)
        cpu = CpuSurplus(os.cpu_count() or 1, prices.baseline_fraction, start)
    else:
        log.info("COST_EC2_INSTANCE_TYPE not set: EC2 host costs not metered")

    cc_lines: list[CostLine] = []
    cc_at = billed_at = 0.0
    billed: dict[str, BilledLine] = {}
    org_billed: dict[str, BilledLine] = {}
    meter_started = time.monotonic()
    log.info("cost-meter started", extra={"fields": {"instance_type": instance_type, "confluent": bool(confluent)}})
    while True:
        now = time.time()
        lines: list[CostLine] = datadog_costs()
        if remote_vm is not None:
            _, remote_ebs_gb, remote_public_ipv4_count = remote_vm
            lines += static_aws_costs(prices, ebs_gb=remote_ebs_gb, public_ipv4_count=remote_public_ipv4_count,
                                      elapsed_hours=(time.monotonic() - meter_started) / 3600.0)
        if online is not None:
            try:
                lines += online.sample(time.monotonic())
                metrics.gauge("dd_demo.cost.meter_error", 0, ["vendor:aws", "source:ecs"])
            except (BotoCoreError, ClientError, MetricsApiError, KeyError, ValueError) as exc:
                log.error(f"ECS cost sampling failed; online costs not emitted: {exc}")
                metrics.gauge("dd_demo.cost.meter_error", 1, ["vendor:aws", "source:ecs"])
        if confluent and now - cc_at >= cc_s:
            try:
                cc_lines = confluent_costs(prices, confluent.usage(dt.datetime.now(dt.timezone.utc)))
                cc_at = now
            except (MetricsApiError, OSError, ValueError) as exc:   # keep the last good lines, say so loudly
                cc_at = now   # retry at the normal interval, not every emit
                log.error(f"Confluent usage query failed, keeping the previous values: {exc}")
                metrics.gauge("dd_demo.cost.meter_error", 1, ["vendor:confluent"])
        lines += cc_lines
        if costs is not None and now - billed_at >= billed_s:
            try:
                billed, org_billed = costs.billed_env_and_org(since, dt.datetime.now(dt.timezone.utc).date())
                billed_at = now
            except (MetricsApiError, OSError, ValueError) as exc:
                billed_at = now
                log.error(f"Confluent Costs API query failed, keeping the previous values: {exc}")
                metrics.gauge("dd_demo.cost.meter_error", 1, ["vendor:confluent", "source:costs_api"])
        if costs is not None:
            utc = dt.datetime.now(dt.timezone.utc)
            lines.append(ecku_cost(prices, billed, utc.hour + utc.minute / 60.0))
        for line_type, b in billed.items():
            tags = ["vendor:confluent", f"line_type:{line_type.lower()}"]
            metrics.gauge("dd_demo.cost.billed_usd_total", b.amount, tags)
            metrics.gauge("dd_demo.cost.billed_list_usd_total", b.original, tags)
        metrics.gauge("dd_demo.cost.aggregate_billed_usd_total", _cents_nonnegative(sum(b.amount for b in billed.values())), [])
        metrics.gauge("dd_demo.cost.aggregate_billed_list_usd_total", _cents_nonnegative(sum(b.original for b in billed.values())), [])
        # Whole Confluent org (every stack and anything else in the org), after and before credits.
        for line_type, b in org_billed.items():
            tags = ["vendor:confluent", f"line_type:{line_type.lower()}"]
            metrics.gauge("dd_demo.cost.org_billed_usd_total", b.amount, tags)
            metrics.gauge("dd_demo.cost.org_billed_list_usd_total", b.original, tags)
        metrics.gauge("dd_demo.cost.aggregate_org_billed_usd_total", _cents_nonnegative(sum(b.amount for b in org_billed.values())), [])
        metrics.gauge("dd_demo.cost.aggregate_org_billed_list_usd_total",
                      _cents_nonnegative(sum(b.original for line_type, b in org_billed.items() if line_type != "PROMO_CREDIT")), [])
        if cpu is not None:
            cpu.sample(now, *host_cpu_jiffies())
            if redis_client is not None:
                try:
                    redis_client.set(REDIS_CPU_KEY, cpu.vcpu_hours)
                except OSError as exc:
                    log.error(f"Redis write of the CPU-credit total failed: {exc}")
            lines += aws_costs(prices, HostUsage(host_uptime_hours(), ebs_gb, cpu.vcpus, cpu.busy_now, cpu.vcpu_hours))
        for c in lines:
            tags = [f"vendor:{c.vendor}", f"item:{c.item}", f"basis:{c.basis}"]
            metrics.gauge("dd_demo.cost.usd_per_hour", c.usd_per_hour, tags)
            metrics.gauge("dd_demo.cost.usd_total", c.usd_total, tags)
        for vendor in (*AGGREGATE_VENDORS, "all"):
            selected = lines if vendor == "all" else [line for line in lines if line.vendor == vendor]
            metrics.gauge("dd_demo.cost.aggregate_usd_per_hour", _cents_nonnegative(sum(line.usd_per_hour for line in selected)),
                          [f"vendor:{vendor}"])
            metrics.gauge("dd_demo.cost.aggregate_usd_total", _cents_nonnegative(sum(line.usd_total for line in selected)),
                          [f"vendor:{vendor}"])
        log.info("emitted", extra={"fields": {
            "usd_per_hour": round(sum(c.usd_per_hour for c in lines), 4),
            "usd_total": round(sum(c.usd_total for c in lines), 4),
            "confluent_billed_usd": round(sum(b.amount for b in billed.values()), 4),
            "confluent_org_billed_usd": round(sum(b.amount for b in org_billed.values()), 4),
            "by_vendor": {v: round(sum(c.usd_total for c in lines if c.vendor == v), 4) for v in ("aws", "confluent", "datadog")}}})
        time.sleep(max(1.0, emit_s - (time.time() - now)))


if __name__ == "__main__":
    sys.exit(main())
