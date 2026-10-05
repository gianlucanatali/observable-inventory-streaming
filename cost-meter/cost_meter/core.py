"""Cost math: usage samples x list prices -> cost lines. No I/O here."""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass

log = logging.getLogger("cost_meter")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"timestamp": self.formatTime(record), "level": record.levelname, "logger": record.name,
               "message": record.getMessage()}
        out.update(getattr(record, "fields", {}))
        return json.dumps(out)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


class PricesError(ValueError):
    pass


@dataclass(frozen=True)
class Prices:
    ec2_usd_per_hour: float
    ebs_usd_per_gb_hour: float
    ipv4_usd_per_hour: float
    cpu_credit_usd_per_vcpu_hour: float
    baseline_fraction: float
    ecku_usd_per_hour: float
    write_usd_per_gb: float
    read_usd_per_gb: float
    storage_usd_per_gb_hour: float
    flink_usd_per_cfu_hour: float
    bytes_per_gb: float


def load_prices(raw: dict, instance_type: str) -> Prices:
    """Prices for one EC2 instance type. Raises PricesError naming the missing entry."""
    try:
        aws, cc = raw["aws"], raw["confluent"]
        if instance_type and instance_type not in aws["ec2_usd_per_hour"]:
            raise PricesError(f"prices.json: no EC2 price for instance type '{instance_type}' (aws.ec2_usd_per_hour)")
        if instance_type and instance_type not in aws["burstable_baseline_fraction"]:
            raise PricesError(f"prices.json: no baseline for '{instance_type}' (aws.burstable_baseline_fraction)")
        return Prices(
            ec2_usd_per_hour=float(aws["ec2_usd_per_hour"][instance_type]) if instance_type else 0.0,
            ebs_usd_per_gb_hour=float(aws["ebs_gp3_usd_per_gb_month"]) / float(aws["hours_per_month"]),
            ipv4_usd_per_hour=float(aws["public_ipv4_usd_per_hour"]),
            cpu_credit_usd_per_vcpu_hour=float(aws["cpu_credit_usd_per_vcpu_hour"]),
            baseline_fraction=float(aws["burstable_baseline_fraction"][instance_type]) if instance_type else 1.0,
            ecku_usd_per_hour=float(cc["basic_ecku_usd_per_hour"]),
            write_usd_per_gb=float(cc["kafka_write_usd_per_gb"]),
            read_usd_per_gb=float(cc["kafka_read_usd_per_gb"]),
            storage_usd_per_gb_hour=float(cc["kafka_storage_usd_per_gb_hour"]),
            flink_usd_per_cfu_hour=float(cc["flink_usd_per_cfu_hour"]),
            bytes_per_gb=float(cc["bytes_per_gb"]),
        )
    except KeyError as exc:
        raise PricesError(f"prices.json: missing key {exc}") from exc


@dataclass(frozen=True)
class ConfluentUsage:
    """Since the stack started (totals) and now (rates). Bytes as reported by the Metrics API."""
    bytes_in_total: float      # received_bytes = producer writes
    bytes_out_total: float     # sent_bytes = consumer reads
    bytes_in_per_hour: float   # recent rate
    bytes_out_per_hour: float
    storage_byte_hours: float  # sum over hours of the hourly max retained bytes
    storage_bytes_now: float
    cfu_minutes_total: float
    cfus_now: float


@dataclass(frozen=True)
class HostUsage:
    uptime_hours: float           # EC2 bills from running: host uptime approximates it
    ebs_gb: float
    vcpus: int
    busy_vcpus_now: float         # vCPUs busy on the host right now
    surplus_vcpu_hours: float     # accumulated busy vCPU-hours above the baseline (upper bound for credits)


@dataclass(frozen=True)
class CostLine:
    vendor: str
    item: str
    basis: str            # list_price | upper_bound | trial
    usd_per_hour: float
    usd_total: float


def confluent_costs(p: Prices, u: ConfluentUsage) -> list[CostLine]:
    gb = p.bytes_per_gb
    return [
        CostLine("confluent", "kafka_write", "list_price", u.bytes_in_per_hour / gb * p.write_usd_per_gb,
                 u.bytes_in_total / gb * p.write_usd_per_gb),
        CostLine("confluent", "kafka_read", "list_price", u.bytes_out_per_hour / gb * p.read_usd_per_gb,
                 u.bytes_out_total / gb * p.read_usd_per_gb),
        CostLine("confluent", "kafka_storage", "list_price", u.storage_bytes_now / gb * p.storage_usd_per_gb_hour,
                 u.storage_byte_hours / gb * p.storage_usd_per_gb_hour),
        CostLine("confluent", "flink", "list_price", u.cfus_now * p.flink_usd_per_cfu_hour,
                 u.cfu_minutes_total / 60.0 * p.flink_usd_per_cfu_hour),
    ]


@dataclass(frozen=True)
class BilledLine:
    amount: float          # after discounts and credits
    original: float        # list price
    quantity_today: float  # billed units on today's (UTC) line, e.g. eCKU-hours


def ecku_cost(p: Prices, billed: dict[str, BilledLine], hours_today: float) -> CostLine:
    """eCKU from the bill, not from a metric: io.confluent.kafka.server/elastic_cku_count read 3 to 11 while the Costs
    API billed 1 eCKU-hour per hour (rehearsal stack, 2026-10-04), so the metric is not the billing unit. Total = billed
    KAFKA_NUM_CKUS at list price; rate = today's billed eCKU-hours per elapsed hour of the UTC day x price."""
    line = billed.get("KAFKA_NUM_CKUS", BilledLine(0.0, 0.0, 0.0))
    rate = line.quantity_today / max(hours_today, 1.0) * p.ecku_usd_per_hour
    return CostLine("confluent", "kafka_ecku", "billed", rate, line.original)


def aws_costs(p: Prices, h: HostUsage) -> list[CostLine]:
    ebs_rate = h.ebs_gb * p.ebs_usd_per_gb_hour
    surplus_now = max(0.0, h.busy_vcpus_now - p.baseline_fraction * h.vcpus)
    return [
        CostLine("aws", "ec2", "list_price", p.ec2_usd_per_hour, h.uptime_hours * p.ec2_usd_per_hour),
        CostLine("aws", "ebs", "list_price", ebs_rate, h.uptime_hours * ebs_rate),
        CostLine("aws", "public_ipv4", "list_price", p.ipv4_usd_per_hour, h.uptime_hours * p.ipv4_usd_per_hour),
        CostLine("aws", "cpu_credits", "upper_bound", surplus_now * p.cpu_credit_usd_per_vcpu_hour,
                 h.surplus_vcpu_hours * p.cpu_credit_usd_per_vcpu_hour),
    ]


def static_aws_costs(p: Prices, *, ebs_gb: float, public_ipv4_count: int, elapsed_hours: float) -> list[CostLine]:
    """Remote VM list-price estimates from declared topology, never host /proc sampling."""
    ebs_rate = ebs_gb * p.ebs_usd_per_gb_hour
    ipv4_rate = public_ipv4_count * p.ipv4_usd_per_hour
    return [
        CostLine("aws", "ec2", "static_estimate", p.ec2_usd_per_hour, elapsed_hours * p.ec2_usd_per_hour),
        CostLine("aws", "ebs", "static_estimate", ebs_rate, elapsed_hours * ebs_rate),
        CostLine("aws", "public_ipv4", "static_estimate", ipv4_rate, elapsed_hours * ipv4_rate),
    ]


def datadog_costs() -> list[CostLine]:
    return [CostLine("datadog", "trial", "trial", 0.0, 0.0)]


def online_costs(raw: dict, *, vcpus: float, memory_gb: float, elapsed_hours: float,
                 alb_count: int, cache_nodes: int) -> list[CostLine]:
    """One observed interval, not a bill: ARM Linux task allocations include sidecars.

    ALB base fee only (LCUs excluded); Redis cache.t4g.small nodes. The caller
    accumulates interval totals, so scaling down does not erase previous usage.
    """
    aws = raw["aws"]
    rates = {
        "fargate_vcpu": vcpus * aws["fargate_arm_vcpu_usd_per_hour"],
        "fargate_memory": memory_gb * aws["fargate_arm_gb_usd_per_hour"],
        "alb": alb_count * aws["alb_usd_per_hour"],
        "elasticache": cache_nodes * aws["elasticache_t4g_small_usd_per_hour"],
    }
    return [CostLine("aws", item, "sampled_estimate", rate, rate * elapsed_hours)
            for item, rate in rates.items()]


class CpuSurplus:
    """Integrates busy vCPUs above the baseline over time from /proc/stat samples (total, idle jiffies)."""

    def __init__(self, vcpus: int, baseline_fraction: float, start_vcpu_hours: float = 0.0):
        self.vcpus, self.baseline = vcpus, baseline_fraction
        self.vcpu_hours = start_vcpu_hours
        self.busy_now = 0.0
        self._last: tuple[float, int, int] | None = None   # (time s, total, idle)

    def sample(self, now_s: float, total: int, idle: int) -> None:
        if self._last is not None:
            t0, tot0, idle0 = self._last
            dt, dtot = now_s - t0, total - tot0
            if dt > 0 and dtot > 0:
                self.busy_now = (1.0 - (idle - idle0) / dtot) * self.vcpus
                surplus = max(0.0, self.busy_now - self.baseline * self.vcpus)
                self.vcpu_hours += surplus * dt / 3600.0
        self._last = (now_s, total, idle)
