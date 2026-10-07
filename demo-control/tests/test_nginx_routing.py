"""Local-mode release routing: NginxRouting + the real nginx/routing-watch.sh and render-routing.sh.

The watcher runs with `--once` from the injected sleep, against a temporary routing volume and a stub `nginx`
(records its arguments; `nginx -t` fails on demand). Nothing touches Docker, nginx or the network.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import fakeredis
import pytest

from conftest import PASSWORD, REGISTRY, FakeStatsd, auth
from demo_control.actions import Actions
from demo_control.app import create_app
from demo_control.config import Config, ConfigError
from demo_control.nginx_routing import NginxRouting
from demo_control.ops import make_operations
from demo_control.registry import load_registry
from demo_control.routing import PRESETS, ROUTING_KEY, STATE_KEY, RoutingError
from demo_control.sales import BackgroundSales
from demo_control.service import Control

OVERLAY = Path(__file__).parents[2]
NGINX_DIR = OVERLAY / "nginx"
WATCH, RENDER = NGINX_DIR / "routing-watch.sh", NGINX_DIR / "render-routing.sh"


def render(*weights) -> str:
    return subprocess.run(["sh", str(RENDER), *map(str, weights)], check=True, capture_output=True, text=True).stdout


class Volume:
    """A temporary nginx-routing volume, plus a watcher pass through the real script."""

    def __init__(self, root: Path, weights=(100, 0, 0)):
        self.dir = root / "routing"
        (self.dir / "requests").mkdir(parents=True)
        (self.dir / "routing.conf").write_text(render(*weights))
        self.nginx_log, self.fail_test, self.runs, self.watching = root / "nginx.log", root / "fail-test", 0, True
        self.nginx = root / "nginx"
        self.nginx.write_text('#!/bin/sh\necho "$*" >> "%s"\nif [ "$1" = -t ] && [ -f "%s" ]; then '
                              'echo "nginx: [emerg] host not found in upstream" >&2; exit 1; fi\n'
                              % (self.nginx_log, self.fail_test))
        self.nginx.chmod(0o755)

    def watch_once(self, _seconds=None):
        if not self.watching:
            return
        self.runs += 1
        env = {**os.environ, "ROUTING_DIR": str(self.dir), "RENDER": str(RENDER), "NGINX": str(self.nginx)}
        done = subprocess.run(["sh", str(WATCH), "--once"], env=env, capture_output=True, text=True, timeout=10)
        assert done.returncode == 0, done.stderr

    def conf(self) -> str:
        return (self.dir / "routing.conf").read_text()

    def nginx_calls(self) -> list[str]:
        return self.nginx_log.read_text().splitlines() if self.nginx_log.exists() else []


@pytest.fixture
def vol(tmp_path):
    return Volume(tmp_path)


def backend(vol, redis=None, running=("inventory-api-100", "inventory-api-110", "inventory-api-120"), **kw):
    clock = iter(range(0, 10_000))
    return NginxRouting(str(vol.dir), redis if redis is not None else fakeredis.FakeRedis(), resolve=lambda h: h in running,
                        sleep=vol.watch_once, clock=lambda: next(clock), **kw)


@pytest.mark.parametrize("name", list(PRESETS))
def test_each_preset_renders_like_make_reloads_nginx_and_publishes(vol, name):
    weights = PRESETS[name][1]
    vol.dir.joinpath("routing.conf").write_text(render(*((0, 100, 0) if weights != (0, 100, 0) else (100, 0, 0))))
    r = fakeredis.FakeRedis()
    steps = []
    out = backend(vol, r).run(name, steps.append)
    assert vol.conf() == render(*weights)  # byte-identical to what make canary-* (apply-routing.sh) writes
    assert vol.nginx_calls() == ["-t", "-s reload"]
    assert out["weights"] == dict(zip(("1.0.0", "1.1.0", "1.2.0"), weights))
    assert r.get(ROUTING_KEY).decode() == "1.0.0={} 1.1.0={} 1.2.0={}".format(*weights)
    assert steps[-1] == "nginx weights 1.0.0/1.1.0/1.2.0 = {} {} {}% (verified)".format(*weights)
    assert "Reading the nginx upstream back" in steps


def test_rollback_restores_the_previous_routing(vol):
    r = fakeredis.FakeRedis()
    nx = backend(vol, r)
    nx.run("canary-110-10", lambda m: None)
    assert json.loads(r.get(STATE_KEY)) == {"current": "90 10 0", "previous": "100 0 0"}
    nx.run("rollback", lambda m: None)
    assert vol.conf() == render(100, 0, 0)
    assert nx.view() == {"weights": {"1.0.0": 100, "1.1.0": 0, "1.2.0": 0}, "current": "100 0 0",
                         "previous": "90 10 0", "backend": "nginx", "label": "nginx weights"}


def test_a_stopped_release_is_refused_before_anything_is_written(vol):
    nx = backend(vol, running=("inventory-api-100",))
    with pytest.raises(RoutingError, match="inventory-api-110 but that container is not running.*layer-on L=releases"):
        nx.run("canary-110-10", lambda m: None)
    assert vol.runs == 0 and not (vol.dir / "requests" / "request").exists() and vol.conf() == render(100, 0, 0)


def test_nginx_test_failure_restores_the_previous_file_and_says_why(vol):
    vol.fail_test.write_text("")
    with pytest.raises(RoutingError, match="nginx refused the routing 90 10 0: nginx -t failed, previous routing restored.*host not found"):
        backend(vol).run("canary-110-10", lambda m: None)
    assert vol.conf() == render(100, 0, 0) and vol.nginx_calls() == ["-t"]  # never reloaded


def test_no_watcher_times_out_and_withdraws_the_request(vol):
    vol.watching = False
    nx = backend(vol, timeout_s=3)
    with pytest.raises(RoutingError, match="did not answer.*within 3 s \\(the request was withdrawn, routing unchanged\\).*routing-watch.sh"):
        nx.run("canary-10", lambda m: None)
    assert not (vol.dir / "requests" / "request").exists() and vol.conf() == render(100, 0, 0)


def test_a_stale_answer_for_another_request_is_ignored(vol):
    (vol.dir / "requests" / "result").write_text("someoneelse ok 0 100 0\n")
    backend(vol).run("canary-50", lambda m: None)
    assert vol.conf() == render(50, 0, 50)


def test_unreadable_or_foreign_routing_conf_is_a_clear_error(vol):
    (vol.dir / "routing.conf").write_text("upstream stock_api { server x:1; }\n")
    with pytest.raises(RoutingError, match="cannot read the weights from the routing.conf header"):
        backend(vol).live()
    (vol.dir / "routing.conf").unlink()
    with pytest.raises(RoutingError, match="nginx-routing volume, NGINX_ROUTING_DIR"):
        backend(vol).live()


def test_watcher_rejects_malformed_requests_without_touching_nginx(vol):
    for line, why in [("abc 50 50 1", "sum to 101"), ("abc 1 2", "must be '<id>"), ("ab;c 100 0 0", "invalid request id"),
                      ("abc * 0 0", "not a non-negative integer")]:
        (vol.dir / "requests" / "request").write_text(line + "\n")
        vol.watch_once()
        assert why in (vol.dir / "requests" / "result").read_text(), line
    assert vol.conf() == render(100, 0, 0) and vol.nginx_calls() == []


# --- the panel with the nginx backend: Release routing card, Full demo reset ----------------------------------------------
def panel(vol, sales=None, reset=None):
    r = fakeredis.FakeRedis()
    nx = backend(vol, r)
    actions = Actions(lambda p, g: None, lambda g: None, event_sink=FakeStatsd(), stack="dev",
                      operations=make_operations(nx, None, None, sales, reset))
    control = Control(load_registry(REGISTRY), {}, r, FakeStatsd(), "dev", actions=actions, alb=nx, sales=sales)
    control.rows = lambda: []
    client = create_app(control, PASSWORD).test_client()
    client.actions = actions
    return client, r


def go(client, name):
    client.actions._started_at = -10.0
    started = client.post(f"/control/api/actions/{name}", json={}, headers=auth())
    assert started.status_code == 202, started.get_json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        st = client.get("/control/api/actions", headers=auth()).get_json()
        if st["id"] == started.get_json()["id"] and st["status"] in ("succeeded", "failed"):
            return st
        time.sleep(0.01)
    raise AssertionError("action did not finish")


def test_routing_card_is_enabled_locally_and_buttons_change_nginx(vol):
    c, r = panel(vol)
    html = c.get("/control/", headers=auth()).get_data(as_text=True)
    assert "weighted upstream of the local nginx" in html and "Live nginx weights" in html
    assert 'data-action="canary-110-10" data-weights="90/10/0" data-why="canary: 10% to new release 1.1.0">' in html
    assert "const ROUTING_ON = true" in html
    assert c.get("/control/api/routing", headers=auth()).get_json()["current"] == "100 0 0"
    assert go(c, "incident")["status"] == "succeeded" and vol.conf() == render(0, 100, 0)
    assert c.get("/control/api/routing", headers=auth()).get_json()["weights"] == {"1.0.0": 0, "1.1.0": 100, "1.2.0": 0}


def test_full_reset_locally_is_sales_off_baseline_on_nginx_then_reset(vol):
    vol.dir.joinpath("routing.conf").write_text(render(90, 10, 0))
    calls, rate = [], {"v": 24.0}
    sales = BackgroundSales(lambda: rate["v"], lambda v: (calls.append(("rate", v)), rate.update(v=v), v)[2], 24.0,
                            fakeredis.FakeRedis(), lambda: {})
    c, r = panel(vol, sales=sales, reset=lambda progress: calls.append(("reset", vol.conf())))
    assert 'data-action="full-reset" disabled' not in c.get("/control/", headers=auth()).get_data(as_text=True)
    done = go(c, "full-reset")
    assert done["status"] == "succeeded", done
    assert calls[0] == ("rate", 0.0) and calls[-1] == ("reset", render(100, 0, 0))  # routing before the data reset
    assert r.get(ROUTING_KEY).decode() == "1.0.0=100 1.1.0=0 1.2.0=0"


# --- config and wiring -----------------------------------------------------------------------------------------------
BASE = {"CONTROL_PASSWORD": "x", "REDIS_URL": "redis://r", "KAFKA_BOOTSTRAP": "k:9092", "SR_URL": "http://sr",
        "STACK": "dev", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}


def test_config_nginx_routing_dir():
    assert Config.from_env(BASE).nginx_routing_dir is None
    assert Config.from_env({**BASE, "NGINX_ROUTING_DIR": "/routing"}).nginx_routing_dir == "/routing"
    with pytest.raises(ConfigError, match="must be an absolute path"):
        Config.from_env({**BASE, "NGINX_ROUTING_DIR": "routing"})
    alb = {"ALB_INVENTORY_RULE_ARN": "arn:aws:elasticloadbalancing:eu-west-1:1:listener-rule/app/x/y/z/w",
           **{f"ALB_INVENTORY_{k}_TARGET_GROUP_ARN": "arn:tg" for k in ("100", "110", "120")}}
    with pytest.raises(ConfigError, match="not both"):
        Config.from_env({**BASE, **alb, "NGINX_ROUTING_DIR": "/routing"})


def test_wsgi_and_compose_wire_the_local_backend():
    wsgi = (OVERLAY / "demo-control" / "demo_control" / "wsgi.py").read_text()
    assert "elif cfg.nginx_routing_dir:\n        alb = NginxRouting(cfg.nginx_routing_dir, r)" in wsgi
    dev = (OVERLAY / "compose" / "compose.dev.yaml").read_text()
    assert "NGINX_ROUTING_DIR: /routing" in dev and "- nginx-routing:/routing" in dev
    assert "sh /etc/nginx/routing-watch.sh &" in dev and "chown 10001" in dev
    dockerfile = (OVERLAY / "demo-control" / "Dockerfile").read_text()
    assert "--uid 10001" in dockerfile  # the uid the nginx command makes requests/ writable for
    base = (OVERLAY / "compose" / "compose.yaml").read_text()
    assert "NGINX_ROUTING_DIR" not in base  # the hybrid/cloud panel never gets the local backend
