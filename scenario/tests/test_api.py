"""scenario API: auth, validation, one run at a time, and output identical to the make targets."""
import contextlib
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scenario import api, cli, runs

TOKEN = "t" * 40


@contextlib.contextmanager
def serving(executors):
    runner = api.Runner(executors)
    srv = api.make_server("127.0.0.1", 0, runner, TOKEN)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", runner
    finally:
        srv.shutdown()
        srv.server_close()


def call(base, method, path, body=None, token=TOKEN, raw=None, ctype="application/json", auth=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if auth is not None:
        req.add_header("Authorization", auth)
    elif token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_done(base, run_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, run = call(base, "GET", f"/runs/{run_id}")
        assert status == 200
        if run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} still running")


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, params, emit):
        self.calls.append(params)
        emit("line")
        return 0, {"ok": True}


# --- auth ----------------------------------------------------------------------------------------------------------
def test_auth_is_required_everywhere_but_healthz():
    rec = Recorder()
    with serving({"load": rec, "verify": rec, "canary-check": rec}) as (base, _):
        assert call(base, "GET", "/healthz", token=None) == (200, {"status": "ok"})
        for auth in (None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN[:-1]}", f"bearer {TOKEN}", "Bearer"):
            kw = {"auth": auth} if auth else {"token": None}
            assert call(base, "GET", "/runs", **kw)[0] == 401, auth
            assert call(base, "POST", "/runs", {"kind": "verify"}, **kw)[0] == 401, auth
            assert call(base, "GET", "/runs/0123456789abcdef", **kw)[0] == 401, auth
        assert rec.calls == []
        assert call(base, "GET", "/runs")[0] == 200


def test_token_must_be_long(monkeypatch):
    with pytest.raises(api.ConfigError, match="at least 32"):
        api.make_server("127.0.0.1", 0, api.Runner({}), "short")
    with pytest.raises(api.ConfigError, match="SCENARIO_API_TOKEN"):
        api.check_token(None)


def test_token_is_never_logged(caplog):
    rec = Recorder()
    with caplog.at_level("INFO", logger="scenario.api"):
        with serving({"load": rec, "verify": rec, "canary-check": rec}) as (base, _):
            run = call(base, "POST", "/runs", {"kind": "verify"})[1]
            wait_done(base, run["id"])
            call(base, "GET", "/runs", token="wrong-token-value")
    text = caplog.text + json.dumps([getattr(r, "fields", {}) for r in caplog.records])
    assert TOKEN not in text and "wrong-token-value" not in text and "scenario_run" in text


# --- validation ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body,text", [
    ([], "JSON object"),
    ({"kind": "seed"}, "kind must be one of load, verify, canary-check"),
    ({"kind": "reset"}, "kind must be one of"),
    ({"kind": "load", "extra": 1}, "unknown field"),
    ({"kind": "load", "params": []}, "params must be a JSON object"),
    ({"kind": "load", "params": {"duration_s": 601}}, "between 10.0 and 600.0"),
    ({"kind": "load", "params": {"duration_s": 5}}, "between 10.0 and 600.0"),
    ({"kind": "load", "params": {"rps": 51}}, "between 0.5 and 50.0"),
    ({"kind": "load", "params": {"rps": "5"}}, "must be a number"),
    ({"kind": "load", "params": {"rps": True}}, "must be a number"),
    ({"kind": "load", "params": {"output": "/etc/passwd"}}, "unknown load parameter(s): output"),
    ({"kind": "load", "params": {"seed": 1}}, "unknown load parameter(s): seed"),
    ({"kind": "verify", "params": {"settle_s": 121}}, "between 0.0 and 120.0"),
    ({"kind": "verify", "params": {"json_out": "/tmp/x"}}, "unknown verify parameter"),
    ({"kind": "canary-check", "params": {}}, "release_b is required"),
    ({"kind": "canary-check", "params": {"release_b": "9.9.9"}}, "release_b is required"),
    ({"kind": "canary-check", "params": {"release_b": "1.2.0", "release_a": "1.2.0"}}, "other than release_b"),
    ({"kind": "canary-check", "params": {"release_b": "1.2.0", "min_samples": 30.5}}, "must be an integer"),
    ({"kind": "canary-check", "params": {"release_b": "1.2.0", "min_samples": 0}}, "between 1 and 10000"),
    ({"kind": "canary-check", "params": {"release_b": "1.2.0", "verify_file": "/x"}}, "unknown canary-check"),
])
def test_validation_rejects(body, text):
    with pytest.raises(api.ValidationError) as exc:
        api.validate(body)
    assert text in str(exc.value)


def test_validation_defaults_mirror_the_makefile():
    assert api.validate({"kind": "load"}) == ("load", {"duration_s": 120.0, "rps": 5.0})
    assert api.validate({"kind": "verify", "params": {}}) == ("verify", {"settle_s": 30.0})
    assert api.validate({"kind": "canary-check", "params": {"release_a": "1.1.0", "release_b": "1.2.0",
                                                            "min_samples": 30}}) == (
        "canary-check", {"min_samples": 30, "max_error_rate": 0.0, "p95_budget_ms": 200,
                         "release_a": "1.1.0", "release_b": "1.2.0"})


def test_http_rejects_bad_requests_without_running():
    rec = Recorder()
    with serving({"load": rec, "verify": rec, "canary-check": rec}) as (base, _):
        assert call(base, "POST", "/runs", raw=b"{not json")[0] == 400
        assert call(base, "POST", "/runs", raw=b"x" * 5000)[0] == 413
        assert call(base, "POST", "/runs", {"kind": "verify"}, ctype="text/plain")[0] == 415
        assert call(base, "POST", "/runs/x", {"kind": "verify"})[0] == 404
        assert call(base, "GET", "/runs/../../etc")[0] == 404
        status, body = call(base, "POST", "/runs", {"kind": "load", "params": {"rps": 1000}})
        assert status == 400 and "rps" in body["error"]
    assert rec.calls == []


# --- sequencing ----------------------------------------------------------------------------------------------------
def test_one_run_at_a_time_and_progress():
    gate = threading.Event()

    def slow(params, emit):
        emit(json.dumps({"window": 0, "window_s": 10.0, "count": 50, "releases": {
            "1.1.0": {"count": 45, "errors": 0, "p95_ms": 640.0}, "1.2.0": {"count": 5, "errors": 0, "p95_ms": 22.4}}}))
        gate.wait(5)
        return 0, {"done": True}

    rec = Recorder()
    with serving({"load": slow, "verify": rec, "canary-check": rec}) as (base, runner):
        status, first = call(base, "POST", "/runs", {"kind": "load"})
        assert status == 202 and first["status"] == "running" and api.RUN_ID.fullmatch(first["id"])
        time.sleep(0.1)
        status, busy = call(base, "POST", "/runs", {"kind": "verify"})
        assert status == 409 and busy["active"] == first["id"] and "one run at a time" in busy["error"]
        live = call(base, "GET", f"/runs/{first['id']}")[1]
        assert live["progress"] == "Load 10/120 s, last window: 1.1.0 45 req p95 640 ms; 1.2.0 5 req p95 22 ms; errors 0"
        assert call(base, "GET", "/runs")[1]["active"] == first["id"]
        gate.set()
        done = wait_done(base, first["id"])
        assert done["status"] == "passed" and done["exit_code"] == 0 and done["result"] == {"done": True}
        second = call(base, "POST", "/runs", {"kind": "verify"})
        assert second[0] == 202 and wait_done(base, second[1]["id"])["status"] == "passed"
        listing = call(base, "GET", "/runs")[1]
        assert listing["active"] is None and [r["id"] for r in listing["runs"]] == [second[1]["id"], first["id"]]
        assert "lines" not in listing["runs"][0]
    assert rec.calls == [{"settle_s": 30.0}]


def test_failed_check_and_errors_are_reported():
    def failing(params, emit):
        emit("CANARY GATES FAILED")
        return 1, {"ok": False}

    def broken(params, emit):
        raise RuntimeError("cannot connect to source of store S01")

    def missing(params, emit):
        raise FileNotFoundError(2, "No such file", "/out/verify.json")

    with serving({"load": broken, "verify": missing, "canary-check": failing}) as (base, _):
        r = wait_done(base, call(base, "POST", "/runs", {"kind": "canary-check", "params": {"release_b": "1.2.0"}})[1]["id"])
        assert (r["status"], r["exit_code"], r["lines"], r["result"]) == ("failed", 1, ["CANARY GATES FAILED"], {"ok": False})
        r = wait_done(base, call(base, "POST", "/runs", {"kind": "load"})[1]["id"])
        assert r["status"] == "error" and r["exit_code"] == 2 and "cannot connect to source of store S01" in r["error"]
        r = wait_done(base, call(base, "POST", "/runs", {"kind": "verify"})[1]["id"])
        assert r["error"] == "/out/verify.json does not exist: run verify first"


def test_lines_are_capped():
    def chatty(params, emit):
        for i in range(api.MAX_LINES + 7):
            emit(f"line {i}")
        return 0, None
    with serving({"load": chatty, "verify": chatty, "canary-check": chatty}) as (base, _):
        r = wait_done(base, call(base, "POST", "/runs", {"kind": "verify"})[1]["id"])
    assert len(r["lines"]) == api.MAX_LINES and r["dropped_lines"] == 7


def test_only_the_last_runs_are_kept():
    rec = Recorder()
    runner = api.Runner({"verify": rec})
    ids = []
    for _ in range(api.KEEP_RUNS + 3):
        ids.append(runner.start("verify", {"settle_s": 0})["id"])
        deadline = time.monotonic() + 2
        while runner.list()["active"] and time.monotonic() < deadline:
            time.sleep(0.005)
    assert runner.get(ids[0]) is None and runner.get(ids[-1])["status"] == "passed"
    assert len(runner.list()["runs"]) == api.KEEP_RUNS


# --- same output as the make targets ------------------------------------------------------------------------------
def run_cli(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["scenario", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code, capsys.readouterr().out.splitlines()


def api_run(out_dir, kind, params):
    with serving(api.default_executors(str(out_dir))) as (base, _):
        status, run = call(base, "POST", "/runs", {"kind": kind, "params": params})
        assert status == 202, run
        return wait_done(base, run["id"])


def summary_doc(b_p95):
    rel = lambda n, p95: {"count": n, "errors": 0, "error_rate": 0.0, "status": {"200": n},  # noqa: E731
                          "p50_ms": 10.0, "p95_ms": p95, "p99_ms": p95}
    return {"rps": 5, "duration_s": 120, "seed": 42, "base_url": "http://x",
            "total": {"count": 600, "releases": {"1.1.0": rel(540, 650.0), "1.2.0": rel(60, b_p95)}}}


@pytest.mark.parametrize("b_p95,release_a,min_samples", [(23.0, "1.1.0", 30), (250.0, "1.1.0", 100), (23.0, None, 30)])
def test_canary_check_output_equals_make(tmp_path, monkeypatch, capsys, b_p95, release_a, min_samples):
    (tmp_path / "last.json").write_text(json.dumps(summary_doc(b_p95)))
    (tmp_path / "verify.json").write_text(json.dumps({"ok": True, "mismatches": 0, "sellable_mismatches": 0}))
    argv = ["canary-check", str(tmp_path / "last.json"), "--release-b", "1.2.0", "--min-samples", str(min_samples),
            "--verify-file", str(tmp_path / "verify.json")] + (["--release-a", release_a] if release_a else [])
    code, out = run_cli(monkeypatch, capsys, argv)
    params = {"release_b": "1.2.0", "min_samples": min_samples, **({"release_a": release_a} if release_a else {})}
    run = api_run(tmp_path, "canary-check", params)
    assert run["lines"] == out and run["exit_code"] == code
    assert run["status"] == ("passed" if code == 0 else "failed")
    assert [f"{'PASS' if g['passed'] else 'FAIL'} {g['name']}: {g['detail']}" for g in run["result"]["gates"]] == out[:-1]
    assert run["result"]["releases"]["1.2.0"]["p95_ms"] == b_p95 and run["result"]["summary_written_at"] > 0


def test_canary_check_without_load_summary_says_so(tmp_path):
    run = api_run(tmp_path, "canary-check", {"release_b": "1.2.0"})
    assert run["status"] == "error" and run["error"].endswith("last.json does not exist: run load first")


def test_verify_output_and_file_equal_make(tmp_path, monkeypatch, capsys):
    @contextlib.contextmanager
    def sources():
        yield {"S01": "c"}
    monkeypatch.setattr(runs, "open_sources", sources)
    monkeypatch.setattr(runs, "redis_connect", lambda: None)
    monkeypatch.setattr(runs, "verify", lambda conns, r, settle_s: (["S01/P0001: source 2, redis 3"], []))
    code, out = run_cli(monkeypatch, capsys, ["verify", "--json-out", str(tmp_path / "cli.json")])
    run = api_run(tmp_path, "verify", {})
    assert code == 1 and run["exit_code"] == 1 and run["status"] == "failed"
    assert run["lines"] == out == ["S01/P0001: source 2, redis 3",
                                   json.dumps({"ok": False, "mismatches": 1, "sellable_mismatches": 0})]
    assert (tmp_path / "verify.json").read_text() == (tmp_path / "cli.json").read_text()
    assert run["result"] == {"ok": False, "mismatches": 1, "sellable_mismatches": 0}


def test_load_output_and_summary_file_equal_make(tmp_path, monkeypatch, capsys):
    async def fake_load(base, rps, duration, seed, window, emit):
        assert (base, rps, duration, seed, window) == ("http://shop", 5.0, 120.0, 42, 10.0)
        emit(json.dumps({"window": 0, "window_s": window, "count": 1, "releases": {}}))
        return {"rps": rps, "duration_s": duration, "seed": seed, "base_url": base, "total": {"count": 0, "releases": {}}}
    monkeypatch.setenv("BASE_URL", "http://shop/")
    monkeypatch.setattr(runs, "run_load", fake_load)
    code, out = run_cli(monkeypatch, capsys, ["load", "--output", str(tmp_path / "cli.json"),
                                              "--duration", "120", "--rps", "5"])
    run = api_run(tmp_path, "load", {})
    assert code == 0 and run["lines"] == out and run["status"] == "passed"
    assert (tmp_path / "last.json").read_text() == (tmp_path / "cli.json").read_text()
    assert run["result"] == json.loads((tmp_path / "last.json").read_text())


def test_load_end_to_end_against_a_local_shop(tmp_path, monkeypatch):
    class Shop(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("X-Release", "1.2.0")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    shop = HTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=shop.serve_forever, daemon=True).start()
    monkeypatch.setenv("BASE_URL", f"http://127.0.0.1:{shop.server_port}")
    monkeypatch.setitem(api.BOUNDS, "load", {**api.BOUNDS["load"], "duration_s": (120.0, 1.0, 600.0, float)})
    try:
        run = api_run(tmp_path, "load", {"duration_s": 1, "rps": 20})
    finally:
        shop.shutdown()
    assert run["status"] == "passed" and run["result"]["total"]["releases"]["1.2.0"]["count"] == 20
    assert json.loads(run["lines"][-1]) == {"summary": run["result"]}
    assert run["progress"] == "load passed"


def test_cli_has_the_api_command():
    args = cli.build_parser().parse_args(["api"])
    assert args.fn is cli.cmd_api and args.port == 8090


def test_serve_fails_loudly_without_token_or_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SCENARIO_API_TOKEN", raising=False)
    with pytest.raises(api.ConfigError, match="SCENARIO_API_TOKEN"):
        api.serve(0)
    monkeypatch.setenv("SCENARIO_API_TOKEN", TOKEN)
    for n in ("BASE_URL", "STORE_HOSTS"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(api.ConfigError, match="BASE_URL"):
        api.serve(0)
