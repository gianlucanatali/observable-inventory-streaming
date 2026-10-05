import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import fakeredis
import pytest

from scenario.canary import evaluate
from scenario.load import Sample, percentile, request_plan, run_load, summarize
from scenario import cli
from scenario.config import ConfigError, parse_store_hosts
from scenario.redisview import (ResetTimeout, diff_source_vs_redis, feed_ok, fetch_positions,
                                find_missing, reset, verify, wait_for_baseline)
from scenario.seed_data import (SELL_OUT_QUANTITIES, product_weights, expected_sellable, seed_hash, seed_rows,
                                seed_rows_for)

NS = "n1"


def test_seed_is_deterministic_and_shaped():
    rows = seed_rows()
    assert rows == seed_rows() and len(rows) == 1000
    assert len({(s, p) for s, p, _ in rows}) == 1000
    assert all(0 <= q <= 40 for *_, q in rows)
    assert {s: q for s, p, q in rows if p == "P0042"} == {"S01": 2, "S02": 1, "S03": 3, "S04": 1, "S05": 2}
    assert expected_sellable(rows)["P0042"] == 9
    assert seed_hash() == "71e7275f78d34a653b7a7870ebdae596f30abbb6540055fa2a7d716446307f46"


def test_each_source_gets_only_its_own_rows():
    for store in ("S01", "S02", "S03", "S04", "S05"):
        rows = seed_rows_for(store)
        assert len(rows) == 200 and {s for s, *_ in rows} == {store}
    assert seed_rows_for("S09") == [] and SELL_OUT_QUANTITIES["S03"] == 3


def test_store_hosts_parsing():
    assert parse_store_hosts("S01=a,S02=b") == [("S01", "a"), ("S02", "b")]
    for bad in ("S01", "S01=", "S01=a,S01=b", "S01=a,", "S01=a, S02=b"):
        with pytest.raises(ConfigError, match="STORE_HOSTS"):
            parse_store_hosts(bad)


def put(r, key, qty, rev, deleted="0"):
    r.hset(f"stock:{NS}:{key[0]}:{key[1]}", mapping={"quantity": qty, "revision": rev, "deleted": deleted})


def test_diff_source_vs_redis():
    source = {("S01", "P1"): (5, 10, False), ("S01", "P2"): (3, 11, False),
              ("S01", "P3"): (0, 12, True), ("S01", "P4"): (1, 13, False)}
    r = fakeredis.FakeRedis(decode_responses=True)
    put(r, ("S01", "P1"), 5, 10)
    put(r, ("S01", "P2"), 4, 11)          # quantity differs
    put(r, ("S01", "P3"), 0, 12, "0")     # deleted differs
    problems = diff_source_vs_redis(source, fetch_positions(r, NS, source))
    assert len(problems) == 3
    assert any("P2" in p for p in problems) and any("P3" in p for p in problems)
    assert any("P4" in p and "missing" in p for p in problems)


def test_find_missing_requires_revision_at_least_written():
    expected = {("S01", "P1"): (5, 10), ("S01", "P2"): (3, 11)}
    r = fakeredis.FakeRedis(decode_responses=True)
    put(r, ("S01", "P1"), 5, 9)    # right quantity, older revision: not yet applied
    put(r, ("S01", "P2"), 3, 12)   # newer revision is fine
    assert find_missing(expected, fetch_positions(r, NS, expected)) == ["S01/P1"]


class FakeTime:
    def __init__(self):
        self.t = 5000.0
        self.on_sleep = None

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep()


def test_wait_for_baseline_converges_when_feed_ok():
    ft = FakeTime()
    expected = {("S01", "P1"): (5, 10)}
    r = fakeredis.FakeRedis(decode_responses=True)
    put(r, ("S01", "P1"), 9, 3)

    def projector_catches_up():
        put(r, ("S01", "P1"), 5, 10)
        r.hset("feed:status", mapping={"state": "ok", "probe_age_ms": 0,
                                       "checked_at_ms": int(ft.t * 1000)})
    ft.on_sleep = projector_catches_up
    wait_for_baseline(r, NS, expected, 30, clock=ft.clock, sleep=ft.sleep)


def test_wait_for_baseline_times_out_listing_keys_and_requires_feed_ok():
    ft = FakeTime()
    expected = {("S01", "P1"): (5, 10), ("S01", "P2"): (1, 11)}
    r = fakeredis.FakeRedis(decode_responses=True)
    put(r, ("S01", "P1"), 5, 10)
    r.hset("feed:status", mapping={"state": "stale", "probe_age_ms": 99999, "checked_at_ms": int(ft.t * 1000)})
    with pytest.raises(ResetTimeout) as e:
        wait_for_baseline(r, NS, expected, 5, clock=ft.clock, sleep=ft.sleep)
    assert "S01/P2" in str(e.value) and "S01/P1" not in str(e.value) and "stale" in str(e.value)


def test_feed_ok_rejects_old_check():
    assert feed_ok({"state": "ok", "checked_at_ms": "1000000"}, 1000.0)
    assert not feed_ok({"state": "ok", "checked_at_ms": "1000000"}, 1011.0)
    assert not feed_ok({}, 1000.0)


def test_percentile_nearest_rank():
    vals = list(map(float, range(1, 101)))
    assert (percentile(vals, 50), percentile(vals, 95), percentile(vals, 99)) == (50, 95, 99)
    assert percentile([], 95) is None and percentile([7.0], 99) == 7.0


def test_summarize_counts_per_release_and_errors():
    samples = ([Sample("1.1.0", "200", 100.0)] * 8 + [Sample("1.1.0", "503", 5.0)]
               + [Sample("1.2.0", "200", 10.0)] * 4 + [Sample("unknown", "error", 1.0)])
    s = summarize(samples)
    a, b = s["releases"]["1.1.0"], s["releases"]["1.2.0"]
    assert s["count"] == 14 and a["count"] == 9 and a["errors"] == 1
    assert a["status"] == {"200": 8, "503": 1} and b["p95_ms"] == 10.0 and b["errors"] == 0
    assert s["releases"]["unknown"]["errors"] == 1 and s["releases"]["unknown"]["p95_ms"] is None


def test_request_plan_deterministic_and_skewed():
    plan = request_plan(42, 5000)
    assert plan == request_plan(42, 5000) and plan != request_plan(43, 5000)
    assert all(p.startswith("P") and 1 <= int(p[1:]) <= 200 for p in plan)
    hot = sum(1 for p in plan if p == "P0001")
    cold = sum(1 for p in plan if p == "P0200")
    assert hot > 10 * max(cold, 1)


def test_run_load_against_local_server_counts_per_release():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path.startswith("/api/availability/P"), self.path
            rel = "1.1.0" if self.path.endswith("0") or self.path.endswith("1") else "1.2.0"
            self.send_response(200)
            self.send_header("X-Release", rel)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lines = []
    try:
        out = asyncio.run(run_load(f"http://127.0.0.1:{srv.server_port}", rps=50, duration_s=2,
                                   seed=42, window_s=1, emit=lines.append))
    finally:
        srv.shutdown()
    total = out["total"]
    assert total["count"] == 100
    assert sum(r["count"] for r in total["releases"].values()) == 100
    assert set(total["releases"]) <= {"1.1.0", "1.2.0"}
    windows = [json.loads(line) for line in lines if '"window"' in line]
    assert [w["window"] for w in windows] == [0, 1] and sum(w["count"] for w in windows) == 100


def summary(a_count=150, b_count=150, b_p95=120.0, b_err=0, a_err=0):
    def rel(count, err, p95):
        return {"count": count, "errors": err, "error_rate": err / count if count else 0,
                "status": {}, "p50_ms": 1, "p95_ms": p95, "p99_ms": p95}
    return {"total": {"count": a_count + b_count, "releases": {
        "1.1.0": rel(a_count, a_err, 700.0), "1.2.0": rel(b_count, b_err, b_p95)}}}


def run_gates(s, ver={"ok": True, "mismatches": 0}, a="1.1.0"):
    gates = evaluate(s, a, "1.2.0", 100, 0.0, 200.0, ver)
    return {g.name: g.passed for g in gates}


def test_canary_all_pass():
    assert all(run_gates(summary()).values())


def test_canary_each_gate_fails_independently():
    assert not run_gates(summary(b_count=99))["min_samples_b"]
    assert not run_gates(summary(a_count=10))["min_samples_a"]
    assert not run_gates(summary(b_err=1))["error_rate_b"]
    assert not run_gates(summary(b_p95=250.0))["p95_b"]
    assert not run_gates(summary(), ver={"ok": False, "mismatches": 3})["correctness"]
    assert not run_gates(summary(), ver=None)["correctness"]


def test_canary_missing_release_b_fails_and_no_release_a_skips_a_gates():
    s = summary()
    del s["total"]["releases"]["1.2.0"]
    g = run_gates(s)
    assert not g["min_samples_b"] and not g["error_rate_b"] and not g["p95_b"]
    assert not any(k.endswith("_a") for k in run_gates(summary(), a=None))


class NoSleep:
    def __init__(self, clock):
        self.clock = clock

    def __call__(self, s):
        self.clock.t += s


def test_verify_checks_positions_per_store_and_sellable_sum(monkeypatch):
    import scenario.source as src
    rows = {"S01": {("S01", "P1"): (2, 10, False), ("S01", "P2"): (1, 11, True)},
            "S02": {("S02", "P1"): (3, 5, False)}}
    monkeypatch.setattr(src, "read_all", lambda conn: rows[conn])
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    put(r, ("S01", "P1"), 2, 10)
    put(r, ("S01", "P2"), 1, 11, "1")
    put(r, ("S02", "P1"), 3, 5)
    r.hset("sellable:P1", mapping={"sellable": 5})
    r.hset("sellable:P2", mapping={"sellable": 0})  # soft-deleted rows do not count
    ft = FakeTime()
    assert verify({"S01": "S01", "S02": "S02"}, r, 3, clock=ft.clock, sleep=ft.sleep) == ([], [])
    r.hset("sellable:P1", mapping={"sellable": 4})
    put(r, ("S02", "P1"), 9, 5)
    pos, sell_p = verify({"S01": "S01", "S02": "S02"}, r, 3, clock=ft.clock, sleep=ft.sleep)
    assert len(pos) == 1 and "S02/P1" in pos[0]
    assert sell_p == ["sellable P1: sources sum 5, redis 4"]


def test_verify_sellable_retries_until_flink_catches_up(monkeypatch):
    import scenario.source as src
    monkeypatch.setattr(src, "read_all", lambda conn: {("S01", "P1"): (2, 10, False)})
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    put(r, ("S01", "P1"), 2, 10)
    ft = FakeTime()
    ft.on_sleep = lambda: r.hset("sellable:P1", mapping={"sellable": 2}) if ft.t > 5002 else None
    assert verify({"S01": "x"}, r, 30, clock=ft.clock, sleep=ft.sleep) == ([], [])


def test_verify_flags_foreign_rows_in_a_source(monkeypatch):
    import scenario.source as src
    monkeypatch.setattr(src, "read_all", lambda conn: {("S02", "P1"): (2, 10, False)})
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    put(r, ("S02", "P1"), 2, 10)
    r.hset("sellable:P1", mapping={"sellable": 2})
    pos, _ = verify({"S01": "x"}, r, 0)
    assert any("other store" in p for p in pos)


def test_reset_restores_all_sources_and_waits_for_sellable(monkeypatch):
    import scenario.source as src
    written = {}

    weights_written = {}

    def fake_upsert(conn, rows, reason):
        assert reason == "reset"
        written[conn] = rows
        return {(s, p): (q, 100) for s, p, q in rows}
    monkeypatch.setattr(src, "upsert_positions", fake_upsert)
    monkeypatch.setattr(src, "write_product_weights", lambda conn, w: weights_written.__setitem__(conn, w))
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    r.hset("demo:config", "demand_skew", "2")
    for s_, p_, q in seed_rows():
        put(r, (s_, p_), q, 100)
    ft = FakeTime()
    r.hset("feed:status", mapping={"state": "ok", "probe_age_ms": 0, "checked_at_ms": int(ft.t * 1000)})
    # sellable not there yet: times out and says so
    with pytest.raises(ResetTimeout, match="sellable mismatch"):
        reset({s: s for s in ("S01", "S02", "S03", "S04", "S05")}, r, 2, now=ft.clock, sleep=ft.sleep)
    assert {rows[0][0] for rows in written.values()} == {"S01", "S02", "S03", "S04", "S05"}
    assert all(len({x[0] for x in rows}) == 1 for rows in written.values())
    for product, total in expected_sellable(seed_rows()).items():
        r.hset(f"sellable:{product}", mapping={"sellable": total})
    r.hset("feed:status", mapping={"state": "ok", "probe_age_ms": 0, "checked_at_ms": int(ft.t * 1000)})
    res = reset({s: s for s in ("S01", "S02", "S03", "S04", "S05")}, r, 5, now=ft.clock, sleep=ft.sleep)
    assert res.positions == 1000 and r.get("scenario:current") == res.scenario_id
    assert set(weights_written) == {"S01", "S02", "S03", "S04", "S05"}
    assert all(w == product_weights(2.0) for w in weights_written.values())  # skew read from demo:config


def test_sell_out_sells_each_stores_whole_quantity_in_order(monkeypatch, capsys):
    import contextlib
    qty = {"S01": 2, "S02": 1, "S03": 0, "S04": 1, "S05": 2}
    sold, sleeps = [], []

    @contextlib.contextmanager
    def fake_sources():
        yield {s: s for s in qty}
    monkeypatch.setattr(cli, "open_sources", fake_sources)
    monkeypatch.setattr(cli, "read_quantity", lambda conn, store, product: qty[store])

    def fake_sell(conn, store, product, n):
        sold.append((conn, store, product, n))
        return (qty[store] - n, 1, None)
    monkeypatch.setattr(cli, "sell", fake_sell)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    args = cli.build_parser().parse_args(["sell-out", "--product", "P0042", "--gap-s", "1.5"])
    assert args.fn(args) == 0
    assert sold == [("S01", "S01", "P0042", 2), ("S02", "S02", "P0042", 1),
                    ("S04", "S04", "P0042", 1), ("S05", "S05", "P0042", 2)]  # S03 had nothing
    assert sleeps == [1.5] * 4  # between stores, not after the last
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0] == {"store": "S01", "sold": 2, "remaining_store": 0, "step": 1}
    assert lines[2] == {"store": "S03", "sold": 0, "remaining_store": 0, "step": 3}
    assert [x["step"] for x in lines] == [1, 2, 3, 4, 5]


# --- restock layer (procurement) ---

class FakeCursor:
    def __init__(self, db):
        self.db, self.rowcount = db, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((" ".join(sql.split()), params))
        self.rowcount = self.db.rowcount


class FakeConn:
    def __init__(self, rowcount=0):
        self.executed, self.rowcount, self.closed = [], rowcount, False

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()

    def close(self):
        self.closed = True


def test_set_lead_time_upserts_and_rejects_negative():
    from scenario.procurement import set_lead_time
    c = FakeConn()
    set_lead_time(c, 60)
    sql, params = c.executed[0]
    assert "procurement_config" in sql and "lead_time_s" in sql and params == ("60",)
    with pytest.raises(ValueError):
        set_lead_time(c, -1)


def test_cancel_open_orders_only_touches_open():
    from scenario.procurement import cancel_open_orders
    c = FakeConn(rowcount=3)
    assert cancel_open_orders(c) == 3
    assert "delivered_at IS NULL AND cancelled_at IS NULL" in c.executed[0][0]
    assert "DELETE" not in c.executed[0][0].upper()


def test_clear_eta_keys_only_eta():
    from scenario.procurement import clear_eta_keys
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("restock:eta:P1", "1")
    r.set("restock:eta:P2", "2")
    r.set("sellable:P1", "x")
    assert clear_eta_keys(r) == 2 and r.keys("*") == ["sellable:P1"]
    assert clear_eta_keys(r) == 0


def _patch_cli(monkeypatch, conn, r):
    calls = []
    monkeypatch.setattr(cli, "procurement_connect", lambda: conn)
    monkeypatch.setattr(cli, "redis_connect", lambda: r)

    import contextlib

    @contextlib.contextmanager
    def fake_sources():
        yield {"S01": "c"}
    monkeypatch.setattr(cli, "open_sources", fake_sources)
    monkeypatch.setattr(cli, "reset", lambda conns, rr, t: (calls.append("reset") or
                        type("R", (), {"scenario_id": "sc-x", "positions": 1})()))
    return calls


def test_reset_cancels_orders_and_clears_eta_when_layer_on(monkeypatch, capsys):
    monkeypatch.setenv("PROCUREMENT_HOST", "procurement-db")
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("restock:eta:P1", "1")
    conn = FakeConn(rowcount=2)
    calls = _patch_cli(monkeypatch, conn, r)
    assert cli.cmd_reset(type("A", (), {"timeout": 5})()) == 0
    assert calls == ["reset"] and conn.closed and r.keys("restock:eta:*") == []
    assert "2 open purchase order(s) cancelled" in capsys.readouterr().out


def test_reset_leaves_procurement_alone_when_layer_off(monkeypatch, capsys):
    monkeypatch.delenv("PROCUREMENT_HOST", raising=False)
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("restock:eta:P1", "1")
    conn = FakeConn()
    monkeypatch.setattr(cli, "procurement_connect", lambda: pytest.fail("procurement must not be touched"))
    _patch_cli(monkeypatch, conn, r)
    monkeypatch.setattr(cli, "procurement_connect", lambda: pytest.fail("procurement must not be touched"))
    assert cli.cmd_reset(type("A", (), {"timeout": 5})()) == 0
    assert r.keys("restock:eta:*") == ["restock:eta:P1"] and "cancelled" not in capsys.readouterr().out


def test_lead_time_command(monkeypatch, capsys):
    monkeypatch.setenv("PROCUREMENT_HOST", "procurement-db")
    conn = FakeConn()
    monkeypatch.setattr(cli, "procurement_connect", lambda: conn)
    args = cli.build_parser().parse_args(["lead-time", "--seconds", "60"])
    assert args.fn(args) == 0 and conn.executed[0][1] == ("60",) and conn.closed
    monkeypatch.delenv("PROCUREMENT_HOST")
    with pytest.raises(RuntimeError, match="PROCUREMENT_HOST"):
        args.fn(args)


# --- demand weights, change_reason, demo:config ---

def test_product_weights_shape_and_determinism():
    w = product_weights(1.0)
    assert w == product_weights(1.0) and len(w) == 200
    assert w["P0042"] == 0.0  # excluded from background sales
    selling = [v for p, v in w.items() if p != "P0042"]
    assert sum(selling) / len(selling) == pytest.approx(1.0)
    assert max(selling) > 10 * min(selling) and min(selling) > 0  # Zipf-like skew
    flat = product_weights(0.0)
    assert {round(v, 9) for p, v in flat.items() if p != "P0042"} == {1.0}
    assert product_weights(2.0)["P0042"] == 0.0
    with pytest.raises(ValueError):
        product_weights(-1)


def test_seed_hash_unchanged_by_weights():
    product_weights(1.0)
    assert seed_hash().startswith("71e7275f")


def test_upsert_sets_change_reason_in_the_transaction_first():
    import scenario.source as src
    conn = FakeConn()
    FakeCursor.fetchone = lambda self: (7, None)  # RETURNING revision, changed_at
    try:
        src.upsert_positions(conn, [("S01", "P0001", 3)], "seed")
    finally:
        del FakeCursor.fetchone
    sqls = [c[0] for c in conn.executed]
    assert "app.change_reason" in sqls[0] and conn.executed[0][1] == ("seed",)
    assert "INSERT INTO stock_position" in sqls[1]
    with pytest.raises(ValueError, match="change reason"):
        src.upsert_positions(conn, [], "sale")


def test_write_product_weights_upserts_each_product():
    import scenario.source as src
    conn = FakeConn()
    src.write_product_weights(conn, {"P0001": 1.5, "P0042": 0.0})
    assert [c[1] for c in conn.executed] == [("P0001", 1.5), ("P0042", 0.0)]
    assert all("ON CONFLICT (product_id)" in c[0] for c in conn.executed)


def test_read_param_default_valid_and_invalid(capsys):
    from scenario.demo_config import read_param
    r = fakeredis.FakeRedis(decode_responses=True)
    assert read_param(r, "sell_out_gap_s") == 1.5 and "no sell_out_gap_s" in capsys.readouterr().err
    r.hset("demo:config", mapping={"sell_out_gap_s": "0.5", "demand_skew": "9"})
    assert read_param(r, "sell_out_gap_s") == 0.5
    with pytest.raises(ValueError, match="outside"):
        read_param(r, "demand_skew")
    r.hset("demo:config", "demand_skew", "lots")
    with pytest.raises(ValueError, match="not a number"):
        read_param(r, "demand_skew")


def test_sell_out_gap_from_demo_config_when_not_given(monkeypatch):
    import contextlib
    r = fakeredis.FakeRedis(decode_responses=True)
    r.hset("demo:config", "sell_out_gap_s", "0.25")
    sleeps = []

    @contextlib.contextmanager
    def fake_sources():
        yield {"S01": "S01", "S02": "S02"}
    monkeypatch.setattr(cli, "open_sources", fake_sources)
    monkeypatch.setattr(cli, "redis_connect", lambda: r)
    monkeypatch.setattr(cli, "read_quantity", lambda conn, store, product: 1)
    monkeypatch.setattr(cli, "sell", lambda conn, store, product, n: (0, 1, None))
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    args = cli.build_parser().parse_args(["sell-out"])
    assert args.gap_s is None and args.fn(args) == 0
    assert sleeps == [0.25]


def test_seed_command_writes_weights_with_skew_from_redis(monkeypatch, capsys):
    import contextlib
    r = fakeredis.FakeRedis(decode_responses=True)
    r.hset("demo:config", "demand_skew", "0")
    calls = []

    @contextlib.contextmanager
    def fake_sources():
        yield {"S01": "c1", "S02": "c2"}
    monkeypatch.setattr(cli, "open_sources", fake_sources)
    monkeypatch.setattr(cli, "redis_connect", lambda: r)
    monkeypatch.setattr(cli, "upsert_positions", lambda conn, rows, reason: calls.append((conn, reason)) or rows)
    monkeypatch.setattr(cli, "write_product_weights", lambda conn, w: calls.append((conn, w["P0001"], w["P0042"])))
    args = cli.build_parser().parse_args(["seed"])
    assert args.fn(args) == 0
    assert calls == [("c1", "seed"), ("c1", 1.0, 0.0), ("c2", "seed"), ("c2", 1.0, 0.0)]
