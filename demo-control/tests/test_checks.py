"""Checks and Background sales cards against the real scenario API server, run in process.

The scenario package is imported from overlay/scenario (pure-Python parts only). Load and verify use fake executors that
write real-shaped files; canary-check uses the real `default_executors` (it reads those files), so the result shape the
panel parses is the one the VM produces.
"""
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

import fakeredis
import pytest

from conftest import PASSWORD, REGISTRY, FakeStatsd, auth
from demo_control.actions import Actions
from demo_control.app import create_app
from demo_control.checks import (LAST_LOAD_KEY, LOAD_DURATION_S, LOAD_RPS, Checks, ScenarioApi,
                                 canary_gate_spec, check_args, parse_routing_value, CheckError)
from demo_control.config import Config, ConfigError
from demo_control.ops import make_operations
from demo_control.registry import load_registry
from demo_control.routing import AlbRouting
from demo_control.sales import SAVED_KEY, BackgroundSales, SalesError
from demo_control.service import Control
from test_ops import RULE, TGS, FakeElb, run

OVERLAY = Path(__file__).parents[2]
sys.path.insert(0, str(OVERLAY / "scenario"))
from scenario import api as scenario_api  # noqa: E402
from scenario.canary import gate_for_routing  # noqa: E402
from scenario.load import Sample, summarize  # noqa: E402
from scenario.presenter_actions import RestockReset  # noqa: E402
from scenario.runs import write_json  # noqa: E402

TOKEN = "a" * 48
LABELS = ["Checks", f"Run load ({LOAD_DURATION_S} s, {LOAD_RPS} rps)", "Verify", "Check canary", "Background sales and reset",
          "Sales off", "Sales on", "Full demo reset"]


def load_summary(b_p95=23.0, b_count=60, a_count=540, weights=(0, 90, 10), a_release="1.1.0"):
    samples = [Sample(a_release, "200", 650.0 if a_release == "1.1.0" else 40.0) for _ in range(a_count)] + [Sample("1.2.0", "200", b_p95) for _ in range(b_count)]
    return {"rps": 5.0, "duration_s": 120.0, "seed": 42, "base_url": "http://alb", "total": summarize(samples)}


class Vm:
    """The scenario API with controllable load/verify outcomes and the real canary-check."""

    def __init__(self, out: Path):
        self.out, self.load_doc, self.verify_doc, self.gate, self.calls = out, load_summary(), None, None, []
        self.verify_doc = {"ok": True, "mismatches": 0, "sellable_mismatches": 0}
        real = scenario_api.default_executors(str(out))

        def load(p, emit):
            self.calls.append(("load", p))
            emit(json.dumps({"window": 0, "window_s": 10, **self.load_doc["total"]}))
            if self.gate:
                self.gate.wait(3)
            write_json(str(out / "last.json"), self.load_doc)
            emit(json.dumps({"summary": self.load_doc}))
            return 0, self.load_doc

        def verify(p, emit):
            self.calls.append(("verify", p))
            write_json(str(out / "verify.json"), self.verify_doc)
            if not self.verify_doc["ok"]:
                emit("S02/P0007: source 3, redis 4")
            emit(json.dumps(self.verify_doc))
            return (0 if self.verify_doc["ok"] else 1), self.verify_doc

        def canary(p, emit):
            self.calls.append(("canary-check", p))
            return real["canary-check"](p, emit)

        self.runner = scenario_api.Runner({"load": load, "verify": verify, "canary-check": canary})
        self.server = scenario_api.make_server("127.0.0.1", 0, self.runner, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeSales:
    """Rate in the sources + per-store sale revisions; jr 'runs' when `jr` is True and the rate is above 0."""

    def __init__(self):
        self.rate, self.jr, self.revs, self.writes, self.leak = 24.0, True, {"S01": 10, "S02": 20}, [], False

    def read(self):
        return self.rate

    def set(self, v):
        self.writes.append(v)
        self.rate = v
        return v

    def probe(self):
        if (self.jr and self.rate > 0) or self.leak:
            self.revs = {s: r + 1 for s, r in self.revs.items()}
        return dict(self.revs)


class FakeProcurement:
    """purchase_order seen through the two statements RestockReset runs (cancel UPDATE, open count)."""

    def __init__(self, open_orders=2):
        self.open, self.connects, self.down = open_orders, 0, None

    def connect(self):
        if self.down:
            raise self.down
        self.connects += 1
        db = self

        class Cur:
            rowcount, row = 0, None
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                if sql.startswith("UPDATE purchase_order"):
                    self.rowcount, db.open = db.open, 0
                else:
                    self.row = (db.open,)
            def fetchone(self): return self.row

        class Conn:
            def cursor(self): return Cur()
            def transaction(self):
                import contextlib
                return contextlib.nullcontext()
            def close(self): pass
        return Conn()


@pytest.fixture
def parts(tmp_path):
    vm = Vm(tmp_path)
    r = fakeredis.FakeRedis()
    elb, statsd, fake_sales = FakeElb((0, 90, 10)), FakeStatsd(), FakeSales()
    alb = AlbRouting(elb, RULE, TGS, r)
    checks = Checks(ScenarioApi(vm.url, TOKEN, sleep=lambda s: time.sleep(0.005)), r, alb.live)
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    sales = BackgroundSales(lambda: fake_sales.read(), fake_sales.set, 24.0, r, fake_sales.probe, sleep=sleep,
                            clock=lambda: clock["t"])
    resets = []
    rtext = fakeredis.FakeRedis(decode_responses=True)
    proc, layers = FakeProcurement(), {"now": ["core", "restock"]}
    restock = RestockReset(proc.connect, rtext, lambda: layers["now"], sleep=lambda s: None)

    def reset(progress):
        resets.append(("reset", proc.open, sorted(rtext.keys("restock:eta:*"))))  # state seen by the data reset
        progress("Reset 1000 source positions")
    actions = Actions(lambda p, g: None, reset, event_sink=statsd, stack="hybrid",
                      operations=make_operations(alb, None, checks, sales, reset, restock))
    control = Control(load_registry(REGISTRY), {}, r, statsd, "hybrid", actions=actions, alb=alb, checks=checks,
                      sales=sales)
    control.rows = lambda: []
    client = create_app(control, PASSWORD).test_client()
    client.actions = actions
    yield dict(vm=vm, redis=r, elb=elb, statsd=statsd, sales=fake_sales, client=client, resets=resets,
               checks=checks, actions=actions, proc=proc, layers=layers, rtext=rtext)
    vm.close()


def go(parts, name, expect="succeeded"):
    parts["client"].actions._started_at = -10.0
    return run(parts["client"], name, expect=expect)


# --- auth, rendering, not configured ---------------------------------------------------------------------------------
def test_new_endpoints_and_actions_require_panel_auth(parts):
    c = parts["client"]
    for path in ("/control/api/checks", "/control/api/sales"):
        assert c.get(path).status_code == 401
    for name in ("load", "verify", "canary-check", "sales-off", "sales-on", "full-reset"):
        assert c.post(f"/control/api/actions/{name}", json={}).status_code == 401
        assert c.post(f"/control/api/actions/{name}", json={}, headers=auth(pw="nope")).status_code == 401
    assert parts["vm"].calls == [] and parts["sales"].writes == []


def test_page_renders_cards_without_the_token(parts):
    html = parts["client"].get("/control/", headers=auth()).get_data(as_text=True)
    for text in LABELS:
        assert text in html, text
    assert TOKEN not in html and "Bearer" not in html
    assert 'data-action="load"' in html and 'data-action="full-reset"' in html
    assert 'class="chk-btn" data-action="load" disabled' not in html


def test_cards_are_disabled_when_not_configured():
    r = fakeredis.FakeRedis()
    actions = Actions(lambda p, g: None, lambda g: None, operations=make_operations(None, None))
    control = Control(load_registry(REGISTRY), {}, r, FakeStatsd(), "dev", actions=actions)
    control.rows = lambda: []
    c = create_app(control, PASSWORD).test_client()
    html = c.get("/control/", headers=auth()).get_data(as_text=True)
    for action in ("load", "verify", "canary-check", "sales-off", "sales-on", "full-reset"):
        assert re.search(rf'data-action="{action}" disabled>', html), action
    assert c.get("/control/api/checks", headers=auth()).status_code == 503
    assert c.get("/control/api/sales", headers=auth()).status_code == 503
    for name, text in (("load", "SCENARIO_API_URL"), ("canary-check", "SCENARIO_API_URL"),
                       ("sales-on", "STORE_HOSTS"), ("full-reset", "STORE_HOSTS")):
        bad = c.post(f"/control/api/actions/{name}", json={}, headers=auth())
        assert bad.status_code == 400 and text in bad.get_json()["error"], name


# --- the canary loop -------------------------------------------------------------------------------------------------
def test_load_verify_and_canary_at_10_percent(parts):
    loaded = go(parts, "load")
    assert parts["vm"].calls[0] == ("load", {"duration_s": 120.0, "rps": 5.0})
    assert loaded["params"] == {"duration_s": 120, "rps": 5}
    assert loaded["result"]["releases"]["1.2.0"] == {"count": 60, "errors": 0, "p50_ms": 23.0, "p95_ms": 23.0}
    assert loaded["progress"].startswith("Load done: 600 requests (1.1.0 540 req, p95 650 ms, 0 errors; "
                                         "1.2.0 60 req, p95 23 ms, 0 errors)") and "routing 0/90/10" in loaded["progress"]
    assert json.loads(parts["redis"].get(LAST_LOAD_KEY))["weights"] == "0/90/10"
    assert go(parts, "verify")["result"]["ok"] is True
    gated = go(parts, "canary-check")
    assert parts["vm"].calls[-1] == ("canary-check", {"min_samples": 30, "max_error_rate": 0.0, "p95_budget_ms": 200,
                                                      "release_a": "1.1.0", "release_b": "1.2.0"})
    res = gated["result"]
    assert res["ok"] is True and res["check_args"] == "--release-a 1.1.0 --release-b 1.2.0 --min-samples 30 --verify-file /out/verify.json"
    assert [g["name"] for g in res["gates"]] == ["min_samples_a", "min_samples_b", "error_rate_a", "error_rate_b", "p95_b", "correctness"]
    assert "PASS p95_b: release 1.2.0: p95 23 ms, budget 200 ms" in res["lines"]
    assert res["lines"][-1] == "CANARY GATES PASSED"
    assert gated["progress"].startswith("CANARY GATES PASSED at 10%")
    events = parts["statsd"].events
    assert [e["title"] for e in events[-2:]] == ["demo action: canary-check started", "demo action: canary-check succeeded"]
    tags = events[-1]["tags"]
    assert {"weights:0/90/10", "release_a:1.1.0", "release_b:1.2.0", "min_samples:30", "stack:hybrid"} <= set(tags)
    assert "load" in [e["title"].split(": ")[1].split()[0] for e in events]
    assert all(TOKEN not in json.dumps(e) for e in events)


@pytest.mark.parametrize("weights,params,args", [
    ((50, 0, 50), {"release_a": "1.0.0", "release_b": "1.2.0"}, "--release-a 1.0.0 --release-b 1.2.0 --verify-file /out/verify.json"),
    ((0, 50, 50), {"release_a": "1.1.0", "release_b": "1.2.0"}, "--release-a 1.1.0 --release-b 1.2.0 --verify-file /out/verify.json"),
    ((0, 0, 100), {"release_b": "1.2.0"}, "--release-b 1.2.0 --verify-file /out/verify.json"),
])
def test_canary_gate_follows_live_routing(parts, weights, params, args):
    parts["elb"].weights = list(weights)
    a_release = "1.0.0" if weights[0] else "1.1.0"
    parts["vm"].load_doc = load_summary(a_count=300 if weights[0] or weights[1] else 0,
                                        b_count=600 if weights[2] == 100 else 300, a_release=a_release)
    go(parts, "load")
    go(parts, "verify")
    gated = go(parts, "canary-check")
    sent = parts["vm"].calls[-1][1]
    assert {k: sent[k] for k in ("release_a", "release_b")} == {"release_a": params.get("release_a"), "release_b": "1.2.0"}
    assert sent["min_samples"] == 100 and gated["result"]["check_args"] == args
    view = parts["client"].get("/control/api/checks", headers=auth()).get_json()
    assert view["canary"]["check_args"] == args and view["last"]["canary-check"]["ok"] is True


@pytest.mark.parametrize("weights", [(100, 0, 0), (20, 40, 40)])
def test_canary_check_refuses_non_canary_routing(parts, weights):
    parts["elb"].weights = list(weights)
    r = parts["client"].post("/control/api/actions/canary-check", json={}, headers=auth())
    assert r.status_code == 400 and "needs a canary split: a newer release next to an older one" in r.get_json()["error"]
    assert parts["vm"].calls == []


def canary_110_summary(slow_p95=412.0, a_count=540, b_count=60):
    samples = [Sample("1.0.0", "200", 40.0) for _ in range(a_count)] + [Sample("1.1.0", "200", slow_p95) for _ in range(b_count)]
    return {"rps": 5.0, "duration_s": 120.0, "seed": 42, "base_url": "http://alb", "total": summarize(samples)}


def test_canary_110_at_10_percent_fails_on_p95_and_names_the_rollback(parts):
    """Canary-first incident: live 90/10/0 gates 1.1.0 (canary) against 1.0.0 (baseline) and FAILS on p95."""
    parts["elb"].weights = [90, 10, 0]
    parts["vm"].load_doc = canary_110_summary()
    assert go(parts, "load")["progress"].startswith("Load done: 600 requests (1.0.0 540 req, p95 40 ms, 0 errors; "
                                                    "1.1.0 60 req, p95 412 ms, 0 errors)")
    go(parts, "verify")
    view = parts["client"].get("/control/api/checks", headers=auth()).get_json()["canary"]
    assert (view["release_a"], view["release_b"], view["label"], view["weights"]) == ("1.0.0", "1.1.0", "10%", "90/10/0")
    failed = go(parts, "canary-check", expect="failed")
    assert parts["vm"].calls[-1] == ("canary-check", {"min_samples": 30, "max_error_rate": 0.0, "p95_budget_ms": 200,
                                                      "release_a": "1.0.0", "release_b": "1.1.0"})
    assert failed["error"] == "Action failed: CANARY GATES FAILED: p95 1.1.0 412 ms > budget 200 ms; roll back"
    res = failed["result"]
    assert res["check_args"] == "--release-a 1.0.0 --release-b 1.1.0 --min-samples 30 --verify-file /out/verify.json"
    assert (res["release_a"], res["release_b"]) == ("1.0.0", "1.1.0")
    assert "PASS min_samples_b: release 1.1.0: 60 samples, need >= 30" in res["lines"]
    assert "FAIL p95_b: release 1.1.0: p95 412 ms, budget 200 ms" in res["lines"]
    assert res["lines"][-1] == "CANARY GATES FAILED: p95 1.1.0 412 ms > budget 200 ms; roll back"
    assert [g["name"] for g in res["gates"] if not g["passed"]] == ["p95_b"]
    event = parts["statsd"].events[-1]
    assert event["title"] == "demo action: canary-check failed"
    assert {"weights:90/10/0", "release_a:1.0.0", "release_b:1.1.0", "min_samples:30"} <= set(event["tags"])


def test_fix_canary_90_0_10_gates_1_2_0_against_1_0_0(parts):
    """The fix after the rollback: Canary 1.2.0 (10%) = 90/0/10, gated 1.2.0 vs 1.0.0 with min samples 30."""
    parts["elb"].weights = [90, 0, 10]
    samples = [Sample("1.0.0", "200", 40.0) for _ in range(540)] + [Sample("1.2.0", "200", 23.0) for _ in range(60)]
    parts["vm"].load_doc = {**load_summary(), "total": summarize(samples)}
    go(parts, "load")
    go(parts, "verify")
    gated = go(parts, "canary-check")
    assert parts["vm"].calls[-1][1] == {"min_samples": 30, "max_error_rate": 0.0, "p95_budget_ms": 200,
                                        "release_a": "1.0.0", "release_b": "1.2.0"}
    assert gated["result"]["check_args"] == "--release-a 1.0.0 --release-b 1.2.0 --min-samples 30 --verify-file /out/verify.json"
    assert gated["progress"].startswith("CANARY GATES PASSED at 10%")
    assert {"weights:90/0/10", "release_a:1.0.0", "release_b:1.2.0"} <= set(parts["statsd"].events[-1]["tags"])


def test_canary_110_at_10_percent_passes_when_fast(parts):
    parts["elb"].weights = [90, 10, 0]
    parts["vm"].load_doc = canary_110_summary(slow_p95=45.0)
    go(parts, "load")
    go(parts, "verify")
    assert go(parts, "canary-check")["progress"].startswith("CANARY GATES PASSED at 10%")


def test_incident_routing_gates_1_1_0_alone(parts):
    parts["elb"].weights = [0, 100, 0]
    parts["vm"].load_doc = {**load_summary(), "total": summarize([Sample("1.1.0", "200", 650.0) for _ in range(600)])}
    go(parts, "load")
    go(parts, "verify")
    failed = go(parts, "canary-check", expect="failed")
    assert parts["vm"].calls[-1][1]["release_b"] == "1.1.0" and parts["vm"].calls[-1][1]["release_a"] is None
    assert failed["error"] == "Action failed: CANARY GATES FAILED: p95 1.1.0 650 ms > budget 200 ms; roll back"


@pytest.mark.parametrize("weights", [(90, 10, 0), (0, 90, 10), (0, 50, 50), (0, 0, 100), (0, 100, 0), (50, 50, 0),
                                     (10, 90, 0), (10, 0, 90), (90, 0, 10), (50, 0, 50), (100, 0, 0), (20, 40, 40)])
def test_panel_gate_rule_matches_scenario_routing_rule(weights):
    """`make canary-check` (scenario --routing) and the panel's Check canary pick the same releases."""
    try:
        expected = gate_for_routing(weights)
    except ValueError:
        with pytest.raises(CheckError):
            canary_gate_spec(weights)
        return
    assert canary_gate_spec(weights) == expected


def test_failed_gate_is_a_failed_action_with_the_gate_lines(parts):
    parts["vm"].load_doc = load_summary(b_p95=250.0)
    go(parts, "load")
    go(parts, "verify")
    failed = go(parts, "canary-check", expect="failed")
    assert failed["error"] == "Action failed: CANARY GATES FAILED: p95 1.2.0 250 ms > budget 200 ms; roll back"
    assert failed["result"]["ok"] is False and "FAIL p95_b: release 1.2.0: p95 250 ms, budget 200 ms" in failed["result"]["lines"]
    assert parts["statsd"].events[-1]["title"] == "demo action: canary-check failed"


def test_verify_mismatch_fails_and_canary_fails_correctness(parts):
    parts["vm"].verify_doc = {"ok": False, "mismatches": 1, "sellable_mismatches": 0}
    go(parts, "load")
    failed = go(parts, "verify", expect="failed")
    assert "Verify FAILED: 1 position mismatch(es), 0 sellable mismatch(es)" in failed["error"]
    assert failed["result"]["lines"][0] == "S02/P0007: source 3, redis 4"
    assert "CANARY GATES FAILED: verify 1 mismatch(es); roll back" in go(parts, "canary-check", expect="failed")["error"]


def test_verify_failing_with_background_sales_on_says_to_full_reset(parts):
    parts["vm"].verify_doc = {"ok": False, "mismatches": 0, "sellable_mismatches": 21}
    failed = go(parts, "verify", expect="failed")
    assert failed["error"] == ("Action failed: Verify FAILED: 0 position mismatch(es), 21 sellable mismatch(es). "
                               "Background sales are on (24/min per store) and kept changing the stock while Verify "
                               "compared it: press Full demo reset, then Verify again")
    parts["sales"].rate = 0.0
    assert "Background sales" not in go(parts, "verify", expect="failed")["error"]


def test_canary_refuses_when_routing_changed_since_the_last_load(parts):
    go(parts, "load")
    go(parts, "verify")
    parts["elb"].weights = [0, 0, 100]
    failed = go(parts, "canary-check", expect="failed")
    assert "routing changed since the panel's last load (load ran at 0/90/10, live is 0/0/100)" in failed["error"]
    assert [c[0] for c in parts["vm"].calls] == ["load", "verify"]


def test_canary_without_any_load_says_run_load_first(parts):
    failed = go(parts, "canary-check", expect="failed")
    assert "last.json does not exist: run load first" in failed["error"]


def test_one_run_at_a_time_on_the_vm(parts):
    vm = parts["vm"]
    vm.gate = threading.Event()
    vm.runner.start("load", {"duration_s": 120.0, "rps": 5.0})  # e.g. started by another panel tab
    failed = go(parts, "verify", expect="failed")
    assert "answered HTTP 409" in failed["error"] and "one run at a time" in failed["error"]
    vm.gate.set()


def test_api_auth_and_network_failures_are_loud(parts, tmp_path):
    bad = Checks(ScenarioApi(parts["vm"].url, "b" * 48), parts["redis"], lambda: (0, 90, 10))
    with pytest.raises(CheckError) as exc:
        bad.verify(lambda m: None)
    assert "HTTP 401" in str(exc.value) and "token mismatch" in str(exc.value) and "b" * 48 not in str(exc.value)
    down = Checks(ScenarioApi("http://127.0.0.1:9", TOKEN, timeout=0.5), parts["redis"], lambda: (0, 90, 10))
    with pytest.raises(CheckError, match="unreachable"):
        down.load(lambda m: None)
    with pytest.raises(CheckError, match="unreachable"):
        down.view()


def test_checks_view_lists_the_last_run_per_kind(parts):
    go(parts, "load")
    go(parts, "verify")
    view = parts["client"].get("/control/api/checks", headers=auth()).get_json()
    assert view["active"] is None and set(view["last"]) == {"load", "verify"}
    assert view["canary"]["label"] == "10%" and view["last_load"]["weights"] == "0/90/10"


def test_token_is_never_logged(parts, caplog):
    with caplog.at_level(logging.DEBUG):
        go(parts, "load")
        go(parts, "verify", expect="succeeded")
    assert TOKEN not in caplog.text and TOKEN not in json.dumps([getattr(r, "fields", {}) for r in caplog.records])


# --- single source of truth with the Makefile and the workshop -------------------------------------------------------
def test_load_and_gate_parameters_mirror_make_and_docs():
    makefile = (OVERLAY / "Makefile").read_text()
    assert f"LOAD_DURATION ?= {LOAD_DURATION_S}\n" in makefile and f"LOAD_RPS ?= {LOAD_RPS}\n" in makefile
    # Default CHECK_ARGS is empty: make reads the live routing and scenario applies the same rule as the panel.
    assert "CHECK_ARGS ?=\n" in makefile
    assert '$(SCEN) canary-check /out/last.json --routing "$$live" --verify-file /out/verify.json' in makefile
    assert check_args(canary_gate_spec((0, 50, 50))) == "--release-a 1.1.0 --release-b 1.2.0 --verify-file /out/verify.json"
    workshop = (OVERLAY / "workshop" / "README.md").read_text()
    assert f'canary-check CHECK_ARGS="{check_args(canary_gate_spec((90, 10, 0)))}"' in workshop  # Lab 3.1 bad canary
    assert f'canary-check CHECK_ARGS="{check_args(canary_gate_spec((90, 0, 10)))}"' in workshop  # Lab 4.1 fix at 10%
    assert f'canary-check CHECK_ARGS="{check_args(canary_gate_spec((0, 0, 100)))}"' in workshop
    assert "load --output /out/last.json $(LOAD_ARGS)" in makefile and "verify --json-out /out/verify.json" in makefile
    assert "canary-check /out/last.json $(CHECK_ARGS)" in makefile


def test_routing_value_parser():
    assert parse_routing_value("1.0.0=0 1.1.0=90 1.2.0=10") == (0, 90, 10)
    for bad in (None, "", "garbage", "1.0.0=a 1.1.0=0 1.2.0=0"):
        with pytest.raises(CheckError):
            parse_routing_value(bad)


# --- background sales and full reset ---------------------------------------------------------------------------------
def test_sales_off_then_on_restores_the_rate(parts):
    s, r = parts["sales"], parts["redis"]
    off = go(parts, "sales-off")
    assert s.rate == 0 and s.writes == [0.0] and float(r.get(SAVED_KEY)) == 24.0
    assert off["progress"] == "Background sales are off (rate 0; 24/min remembered for Sales on)"
    assert parts["client"].get("/control/api/sales", headers=auth()).get_json() == {"rate": 0, "saved": 24.0, "default": 24.0}
    go(parts, "sales-off")  # already off: the remembered rate stays
    assert float(r.get(SAVED_KEY)) == 24.0 and s.writes == [0.0]
    on = go(parts, "sales-on")
    assert s.rate == 24.0 and r.get(SAVED_KEY) is None and on["result"]["sale_seen_in"] == ["S01", "S02"]
    assert on["progress"].startswith("Background sales are on: 24/min per store")


def test_sales_on_without_jr_containers_fails_with_the_make_hint(parts):
    parts["sales"].jr = False
    failed = go(parts, "sales-on", expect="failed")
    assert "no background sale was recorded in any store for 10 s" in failed["error"]
    assert "make sales-on starts them" in failed["error"]


def test_sales_off_fails_when_sales_continue(parts):
    parts["sales"].leak = True
    failed = go(parts, "sales-off", expect="failed")
    assert "rate is 0 but store(s) S01, S02 still recorded sales" in failed["error"]


def test_sales_on_uses_default_without_a_remembered_rate(parts):
    parts["sales"].rate = 0.0
    go(parts, "sales-on")
    assert parts["sales"].writes == [24.0]


def test_full_reset_runs_sales_off_baseline_and_reset_in_order(parts):
    parts["rtext"].set("restock:eta:P0042", "1")
    done = go(parts, "full-reset")
    # orders are cancelled before the data reset (nothing delivered onto the baseline), eta keys cleared after it
    assert parts["sales"].rate == 0 and parts["elb"].weights == [100, 0, 0]
    assert parts["resets"] == [("reset", 0, ["restock:eta:P0042"])]
    assert parts["proc"].open == 0 and parts["rtext"].keys("restock:eta:*") == []
    assert done["result"]["routing"]["weights"] == {"1.0.0": 100, "1.1.0": 0, "1.2.0": 0}
    assert done["result"]["restock"] == {"cancelled_orders": 2, "eta_keys_cleared": 1}
    assert done["progress"].startswith("Full demo reset done")
    assert "2 open purchase order(s) cancelled, 1 restock:eta key(s) cleared" in done["progress"]
    assert "weights:100/0/0" in parts["statsd"].events[-1]["tags"]


def test_full_reset_skips_procurement_when_the_restock_layer_is_off(parts):
    parts["layers"]["now"] = ["core"]
    parts["rtext"].set("restock:eta:P0042", "1")
    done = go(parts, "full-reset")
    assert parts["proc"].connects == 0 and parts["rtext"].keys("restock:eta:*") == ["restock:eta:P0042"]
    assert done["result"]["restock"] == {"skipped": "the restock layer is off (running layers: core)"}
    assert "purchase orders not touched: the restock layer is off" in done["progress"]


def test_full_reset_fails_loudly_when_the_procurement_db_is_unreachable(parts):
    parts["proc"].down = OSError("connection refused")
    failed = go(parts, "full-reset", expect="failed")
    assert "cannot connect to the procurement database" in failed["error"] and parts["resets"] == []


def test_full_reset_stops_at_the_first_failure(parts):
    parts["sales"].leak = True
    go(parts, "full-reset", expect="failed")
    assert parts["elb"].modified == [] and parts["resets"] == [] and parts["proc"].open == 2


def test_reset_demo_data_refuses_while_background_sales_are_on(parts):
    c = parts["client"]
    c.actions._started_at = -10.0
    bad = c.post("/control/api/actions/reset", json={}, headers=auth())
    assert bad.status_code == 400 and parts["resets"] == []
    assert bad.get_json()["error"] == ("Background sales are on (24/min per store): press Sales off first, "
                                       "or use Full demo reset")
    go(parts, "sales-off")
    done = go(parts, "reset")
    assert len(parts["resets"]) == 1 and done["progress"] == "Source and Redis verification completed"


def test_reset_demo_data_refuses_when_the_sales_rate_is_unreadable(parts):
    def unreadable():
        raise SalesError("sales_per_min_per_store is not readable from the store sources: error S02 down")
    parts["sales"].read = unreadable
    c = parts["client"]
    c.actions._started_at = -10.0
    bad = c.post("/control/api/actions/reset", json={}, headers=auth())
    assert bad.status_code == 400 and parts["resets"] == []
    assert bad.get_json()["error"].startswith("Reset refused: cannot read the background sales rate")
    assert "S02 down" in bad.get_json()["error"]


def test_full_reset_needs_alb_routing():
    r = fakeredis.FakeRedis()
    sales = BackgroundSales(lambda: 24.0, lambda v: v, 24.0, r, lambda: {})
    actions = Actions(lambda p, g: None, lambda g: None, operations=make_operations(None, None, None, sales, lambda g: None))
    control = Control(load_registry(REGISTRY), {}, r, FakeStatsd(), "dev", actions=actions, sales=sales)
    control.rows = lambda: []
    c = create_app(control, PASSWORD).test_client()
    bad = c.post("/control/api/actions/full-reset", json={}, headers=auth())
    assert bad.status_code == 400 and "make reset" in bad.get_json()["error"]
    assert 'data-action="full-reset" disabled>' in c.get("/control/", headers=auth()).get_data(as_text=True)


def test_sales_saved_value_must_be_a_number():
    r = fakeredis.FakeRedis()
    r.set(SAVED_KEY, "lots")
    with pytest.raises(SalesError, match="is not a number"):
        BackgroundSales(lambda: 0.0, lambda v: v, 24.0, r, lambda: {}).on(lambda m: None)


# --- config ----------------------------------------------------------------------------------------------------------
BASE = {"CONTROL_PASSWORD": "x", "REDIS_URL": "redis://r", "KAFKA_BOOTSTRAP": "k:9092", "SR_URL": "http://sr",
        "STACK": "dev", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}


def test_config_scenario_api_is_optional_but_complete():
    assert Config.from_env(BASE).scenario_api_url is None
    cfg = Config.from_env({**BASE, "SCENARIO_API_URL": "http://10.0.0.5:8090", "SCENARIO_API_TOKEN": TOKEN})
    assert cfg.scenario_api_url == "http://10.0.0.5:8090" and TOKEN not in repr(cfg)
    with pytest.raises(ConfigError, match="must be set together"):
        Config.from_env({**BASE, "SCENARIO_API_URL": "http://10.0.0.5:8090"})
    with pytest.raises(ConfigError, match="must be set together"):
        Config.from_env({**BASE, "SCENARIO_API_TOKEN": TOKEN})
    with pytest.raises(ConfigError, match="at least 32"):
        Config.from_env({**BASE, "SCENARIO_API_URL": "http://vm:8090", "SCENARIO_API_TOKEN": "short"})
    with pytest.raises(ConfigError, match="http://"):
        Config.from_env({**BASE, "SCENARIO_API_URL": "vm:8090", "SCENARIO_API_TOKEN": TOKEN})


def test_actions_card_copy_matches_the_reset_guard(parts):
    html = parts["client"].get("/control/", headers=auth()).get_data(as_text=True)
    assert "Reset demo data refuses to start while background sales are on" in html
    assert "Reset does not stop background sales" not in html
