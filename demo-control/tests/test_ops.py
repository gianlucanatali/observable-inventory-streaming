"""Release routing and store feed cards: mocked ALB (boto3) and Connect REST, no network."""
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
from pathlib import Path

import boto3
import fakeredis
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from conftest import PASSWORD, REGISTRY, FakeStatsd, auth
from demo_control.actions import Actions
from demo_control.app import create_app
from demo_control.config import Config, ConfigError
from demo_control.ops import make_operations
from demo_control.registry import load_registry
from demo_control.routing import (PRESETS, ROUTING_KEY, STATE_KEY, AlbRouting, RoutingError, forward_actions,
                                  region_from_arn, validate_weights)
from demo_control.service import Control
from demo_control.store_feed import STORES, ConnectFeeds, connector_name

OVERLAY = Path(__file__).parents[2]
RULE = "arn:aws:elasticloadbalancing:eu-west-1:123456789012:listener-rule/app/dd-demo-h/abc/def/ghi"
TGS = {k: f"arn:aws:elasticloadbalancing:eu-west-1:123456789012:targetgroup/dd-demo-h-inventory-{k}/{k}" for k in ("100", "110", "120")}
LABELS = ["Canary 1.1.0 (10%)", "Incident (all to 1.1.0)", "Canary 1.2.0 (10%)", "Canary 1.2.0 (50%)", "Canary 1.2.0 (100%)", "Rollback", "Baseline (all to 1.0.0)"]


class FakeElb:
    def __init__(self, weights=(100, 0, 0)):
        self.weights, self.modified, self.fail_modify, self.fail_describe, self.ignore_modify = list(weights), [], None, None, False
        self.gate = None

    def describe_rules(self, RuleArns):
        assert RuleArns == [RULE]
        if self.fail_describe:
            raise self.fail_describe
        return {"Rules": [{"RuleArn": RULE, "Actions": [{"Type": "forward", "Order": 1, "ForwardConfig": {
            "TargetGroups": [{"TargetGroupArn": TGS[k], "Weight": w} for k, w in zip(("100", "110", "120"), self.weights)]}}]}]}

    def modify_rule(self, RuleArn, Actions):
        assert RuleArn == RULE
        if self.gate:
            self.gate.wait(2)
        if self.fail_modify:
            raise self.fail_modify
        self.modified.append(Actions)
        if not self.ignore_modify:
            self.weights = [g["Weight"] for g in Actions[0]["ForwardConfig"]["TargetGroups"]]
        return {}


class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


class FakeConnect:
    """Connect REST: PUT pause/resume flips the state after `lag` status polls."""

    def __init__(self, lag=1):
        self.state = {connector_name(s): "RUNNING" for s in STORES}
        self.pending, self.calls, self.lag, self.fail, self.down = {}, [], lag, None, False

    def __call__(self, req, timeout):
        path = req.full_url.replace("http://vm:8083", "")
        self.calls.append((req.get_method(), path))
        if self.down:
            raise urllib.error.URLError("connection refused")
        m = re.fullmatch(r"/connectors/([a-z0-9-]+)/(pause|resume|status)", path)
        name, verb = m.group(1), m.group(2)
        if self.fail and verb != "status":
            raise urllib.error.HTTPError(req.full_url, self.fail, "err", {}, io.BytesIO(b'{"message":"Connector not found"}'))
        if verb in ("pause", "resume"):
            assert req.get_method() == "PUT"
            self.pending[name] = ["PAUSED" if verb == "pause" else "RUNNING", self.lag]
            return FakeResp(202, b"")
        if name in self.pending:
            target, left = self.pending[name]
            if left <= 0:
                self.state[name] = target
                del self.pending[name]
            else:
                self.pending[name][1] -= 1
        st = self.state[name]
        return FakeResp(200, json.dumps({"name": name, "connector": {"state": st, "worker_id": "connect:8083"},
                                         "tasks": [{"id": 0, "state": st, "worker_id": "connect:8083"}]}).encode())


@pytest.fixture
def ops_parts():
    r = fakeredis.FakeRedis()
    elb, connect, statsd = FakeElb(), FakeConnect(), FakeStatsd()
    alb = AlbRouting(elb, RULE, TGS, r)
    feeds = ConnectFeeds("http://vm:8083", opener=connect, sleep=lambda s: None, settle_seconds=1, poll_seconds=0)
    actions = Actions(lambda p, progress: None, lambda progress: None, event_sink=statsd, stack="hybrid",
                      operations=make_operations(alb, feeds))
    control = Control(load_registry(REGISTRY), {}, r, statsd, "hybrid", actions=actions, alb=alb, feeds=feeds)
    control.rows = lambda: []
    client = create_app(control, PASSWORD).test_client()
    client.actions = actions
    return dict(redis=r, elb=elb, connect=connect, statsd=statsd, control=control, actions=actions, client=client)


def run(client, name, body=None, expect="succeeded"):
    # Step past the 1-second double-click guard so a deliberate repeat of the same button starts a new action.
    client.actions._started_at = -10.0
    started = client.post(f"/control/api/actions/{name}", json=body or {}, headers=auth())
    assert started.status_code == 202, started.get_json()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = client.get("/control/api/actions", headers=auth()).get_json()
        if status["id"] == started.get_json()["id"] and status["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert status["status"] == expect, status
    time.sleep(0.05)  # the Datadog event is sent just after the status flips
    return status


# --- auth and rendering --------------------------------------------------------------------------------------------
def test_new_endpoints_and_actions_require_auth(ops_parts):
    c = ops_parts["client"]
    for path in ("/control/api/routing", "/control/api/store-feeds"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers=auth(pw="wrong")).status_code == 401
    for name in ("canary-10", "rollback", "store-pause"):
        assert c.post(f"/control/api/actions/{name}", json={"store": "S01"}).status_code == 401
    assert ops_parts["elb"].modified == [] and ops_parts["connect"].calls == []


def test_page_renders_routing_and_store_feed_cards(ops_parts):
    html = ops_parts["client"].get("/control/", headers=auth()).get_data(as_text=True)
    for text in ["Release routing", "Store feed", "Pause feed", "Resume feed", "Sell out product", *LABELS]:
        assert text in html, text
    for store in STORES:
        assert f'<option value="{store}"' in html


def test_page_disables_cards_when_not_configured():
    r = fakeredis.FakeRedis()
    actions = Actions(lambda p, g: None, lambda g: None, operations=make_operations(None, None))
    control = Control(load_registry(REGISTRY), {}, r, FakeStatsd(), "dev", actions=actions)
    control.rows = lambda: []
    c = create_app(control, PASSWORD).test_client()
    html = c.get("/control/", headers=auth()).get_data(as_text=True)
    assert 'data-action="canary-10" data-weights="90/0/10"' in html and "disabled>Canary 1.2.0 (10%)" in html
    assert c.get("/control/api/routing", headers=auth()).status_code == 503
    assert "CONNECT_URL" in c.get("/control/api/store-feeds", headers=auth()).get_json()["error"]
    bad = c.post("/control/api/actions/canary-10", json={}, headers=auth())
    assert bad.status_code == 400 and "not configured" in bad.get_json()["error"]


# --- routing -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(PRESETS))
def test_each_routing_button_applies_reads_back_and_publishes(ops_parts, name):
    weights = PRESETS[name][1]
    ops_parts["elb"].weights = [0, 100, 0] if weights != (0, 100, 0) else [100, 0, 0]
    status = run(ops_parts["client"], name)
    assert ops_parts["elb"].modified == [forward_actions(TGS, weights)]
    assert status["result"]["weights"] == dict(zip(("1.0.0", "1.1.0", "1.2.0"), weights))
    assert "(verified)" in status["progress"]
    assert ops_parts["redis"].get(ROUTING_KEY).decode() == "1.0.0={} 1.1.0={} 1.2.0={}".format(*weights)
    events = ops_parts["statsd"].events
    assert [e["title"] for e in events] == [f"demo action: {name} started", f"demo action: {name} succeeded"]
    assert f"weights:{'/'.join(map(str, weights))}" in events[-1]["tags"] and "stack:hybrid" in events[-1]["tags"]
    live = ops_parts["client"].get("/control/api/routing", headers=auth()).get_json()
    assert live["current"] == " ".join(map(str, weights))


def test_rollback_restores_previous_and_toggles_like_the_script(ops_parts):
    c, elb = ops_parts["client"], ops_parts["elb"]
    failed = run(c, "rollback", expect="failed")
    assert "no previous routing recorded" in failed["error"] and elb.modified == []
    run(c, "incident")
    run(c, "canary-10")
    assert json.loads(ops_parts["redis"].get(STATE_KEY)) == {"current": "90 0 10", "previous": "0 100 0"}
    run(c, "rollback")
    assert elb.weights == [0, 100, 0]
    assert c.get("/control/api/routing", headers=auth()).get_json()["previous"] == "90 0 10"
    run(c, "rollback")
    assert elb.weights == [90, 0, 10]
    run(c, "canary-10")  # unchanged routing keeps the remembered previous value (script: only on change)
    assert json.loads(ops_parts["redis"].get(STATE_KEY)) == {"current": "90 0 10", "previous": "0 100 0"}


def test_canary_110_rollback_returns_to_baseline(ops_parts):
    """Canary-first incident: 100/0/0 -> Canary 1.1.0 (10%) -> Rollback lands on 100/0/0 again."""
    c, elb = ops_parts["client"], ops_parts["elb"]
    run(c, "route-baseline")
    status = run(c, "canary-110-10")
    assert elb.weights == [90, 10, 0] and status["result"]["previous"] == "100 0 0"
    assert ops_parts["redis"].get(ROUTING_KEY).decode() == "1.0.0=90 1.1.0=10 1.2.0=0"
    assert "weights:90/10/0" in ops_parts["statsd"].events[-1]["tags"]
    assert ops_parts["statsd"].events[-1]["title"] == "demo action: canary-110-10 succeeded"
    run(c, "rollback")
    assert elb.weights == [100, 0, 0] and elb.modified[-1] == forward_actions(TGS, (100, 0, 0))
    assert json.loads(ops_parts["redis"].get(STATE_KEY)) == {"current": "100 0 0", "previous": "90 10 0"}
    assert ops_parts["redis"].get(ROUTING_KEY).decode() == "1.0.0=100 1.1.0=0 1.2.0=0"


def test_fix_canary_after_rollback_runs_against_1_0_0(ops_parts):
    """After the rollback the fix 1.2.0 is canaried against 1.0.0: 90/0/10 -> 50/0/50 -> 0/0/100; Rollback steps back."""
    c, elb = ops_parts["client"], ops_parts["elb"]
    for step in ("route-baseline", "canary-110-10", "rollback"):
        run(c, step)
    assert elb.weights == [100, 0, 0]
    for step, weights, previous in (("canary-10", [90, 0, 10], "100 0 0"), ("canary-50", [50, 0, 50], "90 0 10"),
                                    ("canary-100", [0, 0, 100], "50 0 50")):
        status = run(c, step)
        assert elb.weights == weights and status["result"]["previous"] == previous, step
        assert f"weights:{'/'.join(map(str, weights))}" in ops_parts["statsd"].events[-1]["tags"], step
    run(c, "rollback")
    assert elb.weights == [50, 0, 50]


def test_canary_110_button_is_first_and_labelled(ops_parts):
    html = ops_parts["client"].get("/control/", headers=auth()).get_data(as_text=True)
    assert 'data-action="canary-110-10" data-weights="90/10/0" data-why="canary: 10% to new release 1.1.0">Canary 1.1.0 (10%)</button>' in html
    assert html.index('data-action="canary-110-10"') < html.index('data-action="incident"')


def test_routing_failures_are_reported(ops_parts):
    c, elb = ops_parts["client"], ops_parts["elb"]
    elb.fail_modify = ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "ModifyRule")
    failed = run(c, "canary-50", expect="failed")
    assert "AWS rejected the weighted forward action" in failed["error"] and "AccessDenied" in failed["error"]
    assert ops_parts["statsd"].events[-1]["title"] == "demo action: canary-50 failed"
    elb.fail_modify, elb.ignore_modify = None, True
    failed = run(c, "canary-50", expect="failed")
    assert "ALB rule reports 100 0 0 after requesting 50 0 50" in failed["error"]
    assert ops_parts["redis"].get(ROUTING_KEY) is None
    elb.fail_describe = ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "DescribeRules")
    r = c.get("/control/api/routing", headers=auth())
    assert r.status_code == 502 and "could not read the weighted forward action" in r.get_json()["error"]


def test_routing_rejects_unexpected_target_groups(ops_parts):
    alb = AlbRouting(ops_parts["elb"], RULE, {**TGS, "120": "arn:other"}, ops_parts["redis"])
    with pytest.raises(RoutingError, match="unexpected target group weights"):
        alb.live()


def test_redis_publish_failure_is_loud_after_verified_alb_change(ops_parts):
    class BrokenRedis(fakeredis.FakeRedis):
        def set(self, key, *a, **k):
            if key == ROUTING_KEY:
                raise ConnectionError("redis down")
            return super().set(key, *a, **k)
    alb = AlbRouting(ops_parts["elb"], RULE, TGS, BrokenRedis())
    with pytest.raises(RoutingError, match="applied and verified, but Redis demo:routing was not updated"):
        alb.run("incident", lambda m: None)


def test_one_action_at_a_time_across_cards(ops_parts):
    c, elb = ops_parts["client"], ops_parts["elb"]
    elb.gate = threading.Event()
    first = c.post("/control/api/actions/incident", json={}, headers=auth()).get_json()
    second = c.post("/control/api/actions/store-pause", json={"store": "S01"}, headers=auth())
    third = c.post("/control/api/actions/sell-out", json={}, headers=auth())
    assert second.status_code == 202 and second.get_json()["id"] == first["id"]
    assert third.get_json()["id"] == first["id"]
    elb.gate.set()
    run(c, "canary-10")
    assert ops_parts["connect"].calls == []


def test_validation_mirrors_the_script():
    assert validate_weights([0, 90, 10]) == (0, 90, 10)
    for bad, text in (([0, 91, 10], "sum to 101"), ([0, 100], "expected 3 weights"), ([-1, 101, 0], "non-negative"),
                      ([True, 99, 0], "non-negative"), ([0, 0, 101], "above 100")):
        with pytest.raises(RoutingError, match=text):
            validate_weights(bad)
    assert region_from_arn(RULE) == "eu-west-1"
    with pytest.raises(RoutingError, match="region"):
        region_from_arn("inventory-rule-arn")


def test_boto3_request_shapes_match_the_elbv2_api():
    client = boto3.client("elbv2", region_name="eu-west-1", aws_access_key_id="test", aws_secret_access_key="test")
    weights = (0, 90, 10)
    rule = {"Rules": [{"RuleArn": RULE, "Actions": [{"Type": "forward", "ForwardConfig": {"TargetGroups": [
        {"TargetGroupArn": TGS[k], "Weight": w} for k, w in zip(("100", "110", "120"), weights)]}}]}]}
    before = {"Rules": [{"RuleArn": RULE, "Actions": [{"Type": "forward", "ForwardConfig": {"TargetGroups": [
        {"TargetGroupArn": TGS[k], "Weight": w} for k, w in zip(("100", "110", "120"), (0, 100, 0))]}}]}]}
    with Stubber(client) as stub:
        stub.add_response("describe_rules", before, {"RuleArns": [RULE]})
        stub.add_response("modify_rule", {"Rules": []}, {"RuleArn": RULE, "Actions": forward_actions(TGS, weights)})
        stub.add_response("describe_rules", rule, {"RuleArns": [RULE]})
        result = AlbRouting(client, RULE, TGS, fakeredis.FakeRedis()).apply(weights, lambda m: None)
        stub.assert_no_pending_responses()
    assert result["previous"] == "0 100 0"


# --- single source of truth with the Makefile and alb-routing.sh -----------------------------------------------------
def test_presets_match_makefile_targets():
    makefile = (OVERLAY / "Makefile").read_text()
    found = {m.group(1): tuple(map(int, m.group(2, 3, 4)))
             for m in re.finditer(r"^([a-z0-9-]+):\n(?:\t[^\n]*\n)*?\t \$\(ROUTE\) (\d+) (\d+) (\d+)$", makefile, re.M)}
    assert {k: v for k, v in found.items() if k in PRESETS} == {k: v[1] for k, v in PRESETS.items()}
    assert set(PRESETS) <= set(found)
    assert re.search(r"^rollback:\n(?:\t[^\n]*\n)*?\t \$\(ROUTE\) --rollback$", makefile, re.M)
    assert "n='inventory-'+'$(STORE)'.lower()" in makefile
    assert all(connector_name(s) == "inventory-" + s.lower() for s in STORES)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_python_port_writes_the_same_actions_and_state_as_alb_routing_sh(tmp_path):
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text("#!/usr/bin/env bash\nset -euo pipefail\nprev=''\nfor a in \"$@\"; do\n"
                                 "  if [ \"$prev\" = --actions ]; then cp \"${a#file://}\" \"$MOCK_ACTIONS\"; fi; prev=$a\ndone\n")
    (bin_dir / "aws").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STACK": "parity", "STATE_DIR": str(state),
           "MOCK_ACTIONS": str(tmp_path / "actions.json"), "ALB_INVENTORY_RULE_ARN": RULE,
           **{f"ALB_INVENTORY_{k}_TARGET_GROUP_ARN": v for k, v in TGS.items()}}
    script = OVERLAY / "compose" / "scripts" / "alb-routing.sh"
    elb, r = FakeElb((100, 0, 0)), fakeredis.FakeRedis()  # the script cannot see the live rule: start aligned
    alb = AlbRouting(elb, RULE, TGS, r)
    for step in ("route-baseline", "canary-110-10", "rollback", "incident", "canary-10", "canary-50", "canary-100",
                 "rollback", "rollback", "incident"):
        args = ["--rollback"] if step == "rollback" else [str(w) for w in PRESETS[step][1]]
        subprocess.run(["bash", str(script), *args], env=env, check=True, capture_output=True)
        alb.run(step, lambda m: None)
        assert json.loads((tmp_path / "actions.json").read_text()) == elb.modified[-1], step
        script_state = dict(line.split("=", 1) for line in (state / "routing-parity").read_text().splitlines())
        assert {"current": script_state["current"], "previous": script_state["previous"] or None} == alb.state(), step


# --- store feed ----------------------------------------------------------------------------------------------------
def test_store_pause_and_resume_read_back_connector_state(ops_parts):
    c, connect = ops_parts["client"], ops_parts["connect"]
    paused = run(c, "store-pause", {"store": "S03"})
    assert ("PUT", "/connectors/inventory-s03/pause") in connect.calls
    assert paused["result"] == {"store": "S03", "connector": "inventory-s03", "state": "PAUSED", "tasks": ["PAUSED"]}
    assert "store:S03" in ops_parts["statsd"].events[-1]["tags"]
    feeds = {f["store"]: f["state"] for f in c.get("/control/api/store-feeds", headers=auth()).get_json()["feeds"]}
    assert feeds == {"S01": "RUNNING", "S02": "RUNNING", "S03": "PAUSED", "S04": "RUNNING", "S05": "RUNNING"}
    resumed = run(c, "store-resume", {"store": "S03"})
    assert ("PUT", "/connectors/inventory-s03/resume") in connect.calls and resumed["result"]["state"] == "RUNNING"


@pytest.mark.parametrize("body", [{}, {"store": "S06"}, {"store": "s01"}, {"store": ["S01"]}])
def test_store_feed_rejects_invalid_store(ops_parts, body):
    r = ops_parts["client"].post("/control/api/actions/store-pause", json=body, headers=auth())
    assert r.status_code == 400 and "S01, S02, S03, S04, S05" in r.get_json()["error"]
    assert ops_parts["connect"].calls == []


def test_store_feed_failures_are_reported(ops_parts):
    c, connect = ops_parts["client"], ops_parts["connect"]
    connect.fail = 404
    failed = run(c, "store-pause", {"store": "S02"}, expect="failed")
    assert "answered HTTP 404" in failed["error"] and "Connector not found" in failed["error"]
    connect.fail, connect.lag = None, 10**9
    ops_parts["control"].feeds._settle = 0.05
    failed = run(c, "store-pause", {"store": "S02"}, expect="failed")
    assert "still reports connector RUNNING" in failed["error"] and "expected PAUSED" in failed["error"]
    assert failed["progress"] == "Waiting for inventory-s02 to report PAUSED"
    connect.down = True
    failed = run(c, "store-resume", {"store": "S02"}, expect="failed")
    assert "unreachable" in failed["error"]
    feeds = c.get("/control/api/store-feeds", headers=auth()).get_json()["feeds"]
    assert all(f["state"] == "ERROR" and "unreachable" in f["error"] for f in feeds)


def test_store_feed_reports_failed_task(ops_parts):
    connect = ops_parts["connect"]
    connect.state["inventory-s05"] = "FAILED"
    with pytest.raises(Exception, match="FAILED"):
        ops_parts["control"].feeds.run("store-resume", "S05", lambda m: None)


# --- config --------------------------------------------------------------------------------------------------------
BASE = {"CONTROL_PASSWORD": "x", "REDIS_URL": "redis://r", "KAFKA_BOOTSTRAP": "k:9092", "SR_URL": "http://sr",
        "STACK": "dev", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}


def test_config_routing_and_connect_are_optional_but_complete():
    cfg = Config.from_env(BASE)
    assert cfg.alb_rule_arn is None and cfg.connect_url is None
    full = {**BASE, "ALB_INVENTORY_RULE_ARN": RULE, "CONNECT_URL": "http://10.0.0.5:8083",
            **{f"ALB_INVENTORY_{k}_TARGET_GROUP_ARN": v for k, v in TGS.items()}}
    cfg = Config.from_env(full)
    assert dict(cfg.alb_target_groups) == TGS and cfg.connect_url == "http://10.0.0.5:8083"
    with pytest.raises(ConfigError, match="ALB_INVENTORY_120_TARGET_GROUP_ARN is required"):
        Config.from_env({k: v for k, v in full.items() if k != "ALB_INVENTORY_120_TARGET_GROUP_ARN"})
    with pytest.raises(ConfigError, match="CONNECT_URL"):
        Config.from_env({**full, "CONNECT_URL": "10.0.0.5:8083"})
    with pytest.raises(ConfigError, match="listener-rule ARN"):
        Config.from_env({**full, "ALB_INVENTORY_RULE_ARN": "inventory-rule-arn"})


def test_presenter_operations_get_a_decoding_redis_client():
    # Without decode_responses the active namespace is bytes and keys become stock:b'n1':... (never converges).
    from pathlib import Path
    wsgi = (Path(__file__).parents[1] / "demo_control/wsgi.py").read_text()
    assert "decode_responses=True" in wsgi
    assert "make_presenter_operations(store_conn, cfg.stores, r_text)" in wsgi
    # Full demo reset's restock part scans restock:eta:* with the same decoding client and reaches make_operations.
    assert "RestockReset(procurement_conn if cfg.procurement_host else None, r_text," in wsgi
    assert "make_operations(alb, feeds, checks, sales, reset_data, restock)" in wsgi
