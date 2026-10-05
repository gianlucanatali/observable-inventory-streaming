import json
from pathlib import Path

import pytest

from cost_meter import core

RAW = json.loads((Path(__file__).parent.parent / "prices.json").read_text())


def test_online_costs_use_task_allocation_not_container_usage():
    # Two running tasks, total task allocations include the Agent sidecars.
    lines = core.online_costs(RAW, vcpus=2.25, memory_gb=5, elapsed_hours=2,
                              alb_count=1, cache_nodes=1)
    costs = {line.item: line for line in lines}
    assert costs["fargate_vcpu"].usd_per_hour == pytest.approx(2.25 * 0.03238)
    assert costs["fargate_memory"].usd_total == pytest.approx(5 * 0.00356 * 2)
    assert costs["alb"].usd_per_hour == pytest.approx(0.0252)
    assert costs["elasticache"].usd_per_hour == pytest.approx(0.034)
    assert all(line.basis == "sampled_estimate" for line in lines)


class FakeEcs:
    def get_paginator(self, name):
        assert name == "list_tasks"
        return self

    def paginate(self, **kwargs):
        assert kwargs == {"cluster": "demo-hybrid", "launchType": "FARGATE", "desiredStatus": "RUNNING"}
        return [{"taskArns": ["task-a"]}, {"taskArns": ["task-b", "task-c"]}]

    def describe_tasks(self, cluster, tasks):
        return {"tasks": [{"taskArn": arn, "lastStatus": "STOPPED" if arn == "task-c" else "RUNNING",
                           "cpu": "1024", "memory": "2048"} for arn in tasks], "failures": []}


def test_online_meter_paginates_and_preserves_totals_when_tasks_stop():
    from cost_meter import adapters
    meter = adapters.OnlineMeter(FakeEcs(), "demo-hybrid", RAW, 1, 1)
    first = {c.item: c for c in meter.sample(100)}
    assert first["fargate_vcpu"].usd_per_hour == pytest.approx(2 * 0.03238)
    assert first["fargate_vcpu"].usd_total == 0
    second = {c.item: c for c in meter.sample(3700)}
    assert second["fargate_memory"].usd_total == pytest.approx(4 * 0.00356)
    meter.ecs.paginate = lambda **kwargs: [{"taskArns": []}]
    third = {c.item: c for c in meter.sample(7300)}
    assert third["fargate_vcpu"].usd_per_hour == 0
    assert third["fargate_vcpu"].usd_total == second["fargate_vcpu"].usd_total


def test_partial_ecs_response_is_not_silently_zero_cost():
    from cost_meter import adapters
    ecs = FakeEcs()
    ecs.describe_tasks = lambda **kwargs: {"tasks": [], "failures": [{"reason": "MISSING"}]}
    with pytest.raises(adapters.MetricsApiError, match="ECS"):
        adapters.OnlineMeter(ecs, "demo-hybrid", RAW, 1, 1).sample(100)


@pytest.mark.parametrize("mode", ["online", "legacy", "error"])
def test_main_emits_cloud_lines_without_host_meter(monkeypatch, mode):
    import boto3
    from cost_meter import __main__ as app

    env = {"STACK": "hybrid", "COST_ECS_CLUSTER": "demo-hybrid", "AWS_REGION": "eu-west-1",
           "COST_ALB_COUNT": "1", "COST_ELASTICACHE_NODES": "1",
           "PRICES_FILE": str(Path(__file__).parent.parent / "prices.json")}
    if mode == "legacy":
        del env["COST_ECS_CLUSTER"]
    ecs = FakeEcs()
    if mode == "error":
        ecs.describe_tasks = lambda **kwargs: {"tasks": [], "failures": [{"reason": "MISSING"}]}
    monkeypatch.setattr(app.os, "environ", env)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: ecs)
    emitted = []

    class Metrics:
        def __init__(self, *args):
            pass

        def gauge(self, name, value, tags):
            emitted.append((name, value, tags))

    class Done(Exception):
        pass

    def stop(_):
        raise Done

    monkeypatch.setattr(app, "DogMetrics", Metrics)
    monkeypatch.setattr(app.time, "sleep", stop)
    with pytest.raises(Done):
        app.main()
    rates = {next(t for t in tags if t.startswith("item:")): value
             for name, value, tags in emitted if name == "dd_demo.cost.usd_per_hour"}
    if mode == "online":
        assert rates["item:fargate_vcpu"] == pytest.approx(2 * 0.03238)
        assert rates["item:alb"] == pytest.approx(0.0252)
    else:
        assert "item:fargate_vcpu" not in rates
    if mode == "error":
        assert ("dd_demo.cost.meter_error", 1, ["vendor:aws", "source:ecs"]) in emitted
    assert "item:ec2" not in rates


def test_fargate_emits_all_six_hybrid_aws_lines_without_proc_sampling(monkeypatch):
    import boto3
    from cost_meter import __main__ as app

    env = {"STACK": "hybrid", "COST_ECS_CLUSTER": "demo-hybrid", "AWS_REGION": "eu-west-1",
           "COST_ALB_COUNT": "1", "COST_ELASTICACHE_NODES": "1",
           "COST_REMOTE_EC2_INSTANCE_TYPE": "t4g.xlarge", "COST_REMOTE_EBS_GB": "40",
           "COST_REMOTE_PUBLIC_IPV4_COUNT": "1",
           "PRICES_FILE": str(Path(__file__).parent.parent / "prices.json")}
    monkeypatch.setattr(app.os, "environ", env)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: FakeEcs())
    emitted = []

    class Metrics:
        def __init__(self, *args):
            pass

        def gauge(self, name, value, tags):
            emitted.append((name, value, tags))

    class Done(Exception):
        pass

    monkeypatch.setattr(app, "DogMetrics", Metrics)
    monkeypatch.setattr(app.time, "sleep", lambda _: (_ for _ in ()).throw(Done()))
    with pytest.raises(Done):
        app.main()

    rates = {next(t for t in tags if t.startswith("item:")): value
             for name, value, tags in emitted if name == "dd_demo.cost.usd_per_hour"}
    assert set(rates).issuperset({"item:ec2", "item:ebs", "item:public_ipv4", "item:fargate_vcpu",
                                  "item:fargate_memory", "item:alb", "item:elasticache"})
    assert "item:cpu_credits" not in rates


def test_meter_emits_one_cent_clamped_aggregate_per_vendor_and_bill_scope(monkeypatch):
    """Aggregate gauges must survive a rolling ECS overlap without summing task origins."""
    import boto3
    from cost_meter import __main__ as app

    env = {
        "STACK": "hybrid", "COST_ECS_CLUSTER": "demo-hybrid", "AWS_REGION": "eu-west-1",
        "COST_ALB_COUNT": "1", "COST_ELASTICACHE_NODES": "1",
        "COST_REMOTE_EC2_INSTANCE_TYPE": "t4g.xlarge", "COST_REMOTE_EBS_GB": "40",
        "COST_REMOTE_PUBLIC_IPV4_COUNT": "1", "COST_METER_API_KEY": "key",
        "COST_METER_API_SECRET": "secret", "CONFLUENT_ENVIRONMENT_ID": "env-a",
        "KAFKA_CLUSTER_ID": "lkc-a", "FLINK_COMPUTE_POOL_ID": "pool-a",
        "PRICES_FILE": str(Path(__file__).parent.parent / "prices.json"),
    }
    monkeypatch.setattr(app.os, "environ", env)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: FakeEcs())
    monkeypatch.setattr(app, "ConfluentMetrics", lambda *args: type("MetricsApi", (), {"usage": lambda *_: None})())
    monkeypatch.setattr(app, "confluent_costs", lambda *_: [])

    class Costs:
        def __init__(self, *args):
            pass

        def billed_env_and_org(self, *_):
            return (
                {"KAFKA_NUM_CKUS": core.BilledLine(1.234, 2.345, 0)},
                {"KAFKA_NUM_CKUS": core.BilledLine(7e-16, 4.194, 0),
                 "PROMO_CREDIT": core.BilledLine(-4.194, -4.194, 0)},
            )

    monkeypatch.setattr(app, "ConfluentCosts", Costs)
    emitted = []

    class Metrics:
        def __init__(self, *args):
            pass

        def gauge(self, name, value, tags):
            emitted.append((name, value, tags))

    class Done(Exception):
        pass

    monkeypatch.setattr(app, "DogMetrics", Metrics)
    monkeypatch.setattr(app.time, "sleep", lambda _: (_ for _ in ()).throw(Done()))
    with pytest.raises(Done):
        app.main()

    aggregates = [(name, value, tuple(tags)) for name, value, tags in emitted if ".aggregate_" in name]
    assert aggregates.count(("dd_demo.cost.aggregate_billed_usd_total", 1.23, ())) == 1
    assert aggregates.count(("dd_demo.cost.aggregate_billed_list_usd_total", 2.35, ())) == 1
    assert aggregates.count(("dd_demo.cost.aggregate_org_billed_usd_total", 0.0, ())) == 1
    assert aggregates.count(("dd_demo.cost.aggregate_org_billed_list_usd_total", 4.19, ())) == 1
    assert {tags for name, _, tags in aggregates if name == "dd_demo.cost.aggregate_usd_per_hour"} == {
        ("vendor:all",), ("vendor:aws",), ("vendor:confluent",), ("vendor:datadog",)}
    assert {tags for name, _, tags in aggregates if name == "dd_demo.cost.aggregate_usd_total"} == {
        ("vendor:all",), ("vendor:aws",), ("vendor:confluent",), ("vendor:datadog",)}


@pytest.mark.parametrize("remote", [
    {"COST_REMOTE_EC2_INSTANCE_TYPE": "t4g.xlarge"},
    {"COST_REMOTE_EBS_GB": "40", "COST_REMOTE_PUBLIC_IPV4_COUNT": "1"},
    {"COST_REMOTE_EC2_INSTANCE_TYPE": "not-an-instance-type", "COST_REMOTE_EBS_GB": "40",
     "COST_REMOTE_PUBLIC_IPV4_COUNT": "1"},
    {"COST_REMOTE_EC2_INSTANCE_TYPE": "t4g.xlarge", "COST_REMOTE_EBS_GB": "not-a-number",
     "COST_REMOTE_PUBLIC_IPV4_COUNT": "1"},
])
def test_remote_vm_inputs_fail_loudly_when_partial_or_malformed(monkeypatch, remote):
    from cost_meter import __main__ as app

    monkeypatch.setattr(app.os, "environ", {"STACK": "hybrid", "PRICES_FILE": str(Path(__file__).parent.parent / "prices.json"), **remote})
    with pytest.raises(SystemExit, match="COST_REMOTE"):
        app.main()


def test_fargate_must_not_use_proc_as_the_on_prem_host(monkeypatch):
    from cost_meter import __main__ as app
    monkeypatch.setattr(app.os, "environ", {
        "COST_EC2_INSTANCE_TYPE": "t4g.xlarge", "ECS_CONTAINER_METADATA_URI_V4": "http://metadata/task"})
    with pytest.raises(SystemExit, match="host costs"):
        app.main()
