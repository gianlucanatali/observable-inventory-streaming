import json
import pathlib

import pytest

from cost_meter import core
from cost_meter.core import (BilledLine, ConfluentUsage, CpuSurplus, HostUsage, PricesError, aws_costs, confluent_costs,
                             ecku_cost, load_prices)

RAW = json.loads((pathlib.Path(__file__).parent.parent / "prices.json").read_text())


def by_item(lines):
    return {c.item: c for c in lines}


def test_prices_file_loads_for_the_demo_instance_type():
    p = load_prices(RAW, "t4g.xlarge")
    assert p.ec2_usd_per_hour == 0.1472
    assert p.ebs_usd_per_gb_hour == pytest.approx(0.088 / 730)


def test_unknown_instance_type_fails_loudly():
    with pytest.raises(PricesError, match="m7i.large"):
        load_prices(RAW, "m7i.large")


def test_confluent_costs_follow_list_prices():
    p = load_prices(RAW, "t4g.xlarge")
    u = ConfluentUsage(bytes_in_total=2e9, bytes_out_total=4e9,
                       bytes_in_per_hour=1e9, bytes_out_per_hour=0, storage_byte_hours=1e9 * 24,
                       storage_bytes_now=1e9, cfu_minutes_total=120, cfus_now=4)
    c = by_item(confluent_costs(p, u))
    assert "kafka_ecku" not in c   # from the bill, see test_ecku_from_the_bill
    assert c["kafka_write"].usd_total == pytest.approx(0.11)
    assert c["kafka_read"].usd_total == pytest.approx(0.22)
    assert c["kafka_write"].usd_per_hour == pytest.approx(0.055)
    assert c["kafka_storage"].usd_total == pytest.approx(24 * 0.00012055)
    assert c["flink"].usd_total == pytest.approx(2 * 0.21)        # 120 CFU-minutes = 2 CFU-hours
    assert c["flink"].usd_per_hour == pytest.approx(4 * 0.21)


def test_aws_costs_from_uptime():
    p = load_prices(RAW, "t4g.xlarge")
    c = by_item(aws_costs(p, HostUsage(uptime_hours=10, ebs_gb=40, vcpus=4, busy_vcpus_now=2.0, surplus_vcpu_hours=1.5)))
    assert c["ec2"].usd_total == pytest.approx(1.472)
    assert c["ebs"].usd_total == pytest.approx(10 * 40 * 0.088 / 730)
    assert c["public_ipv4"].usd_total == pytest.approx(0.05)
    assert c["cpu_credits"].basis == "upper_bound"
    assert c["cpu_credits"].usd_total == pytest.approx(0.06)
    assert c["cpu_credits"].usd_per_hour == pytest.approx((2.0 - 1.6) * 0.04)


def test_remote_vm_static_costs_do_not_claim_fargate_cpu_credit_sampling():
    p = load_prices(RAW, "t4g.xlarge")
    static_costs = getattr(core, "static_aws_costs", None)
    assert static_costs is not None, "remote VM static-cost path is missing"
    c = by_item(static_costs(p, ebs_gb=40, public_ipv4_count=1, elapsed_hours=2))

    assert set(c) == {"ec2", "ebs", "public_ipv4"}
    assert all(line.basis == "static_estimate" for line in c.values())
    assert c["ec2"].usd_total == pytest.approx(2 * 0.1472)
    assert c["ebs"].usd_total == pytest.approx(2 * 40 * 0.088 / 730)
    assert c["public_ipv4"].usd_total == pytest.approx(2 * 0.005)


def test_cpu_surplus_integrates_only_above_baseline():
    s = CpuSurplus(vcpus=4, baseline_fraction=0.4)
    s.sample(0, total=0, idle=0)
    s.sample(3600, total=1000, idle=200)    # 80 % busy = 3.2 vCPUs, 1.6 above the baseline, for one hour
    assert s.busy_now == pytest.approx(3.2)
    assert s.vcpu_hours == pytest.approx(1.6)
    s.sample(7200, total=2000, idle=700)    # 50 % busy = 2.0 vCPUs: 0.4 above
    assert s.vcpu_hours == pytest.approx(2.0)
    s.sample(10800, total=3000, idle=1600)  # 10 % busy: below the baseline, nothing added
    assert s.vcpu_hours == pytest.approx(2.0)


def test_ecku_from_the_bill():
    p = load_prices(RAW, "t4g.xlarge")
    billed = {"KAFKA_NUM_CKUS": BilledLine(amount=0.0, original=2.5245, quantity_today=8)}   # 17 eCKU-h total, 8 today
    e = ecku_cost(p, billed, hours_today=8.0)
    assert e.basis == "billed"
    assert e.usd_total == pytest.approx(2.5245)
    assert e.usd_per_hour == pytest.approx(0.1485)      # 8 eCKU-hours in 8 hours = 1 eCKU
    assert ecku_cost(p, {}, 3.0).usd_total == 0.0
