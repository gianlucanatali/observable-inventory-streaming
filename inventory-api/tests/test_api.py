import json
import time
from pathlib import Path

import fakeredis
import pytest
import redis

from tests.conftest import STORES

IDS = ["P0042", "P0001", "P0002", "P9999"]


def get(app, p):
    return app.test_client().get(f"/api/availability/{p}")


def body(app, p):
    return get(app, p).get_json()


def set_feed(r, sid, state="ok", age_ms=0):
    r.hset(f"feed:status:{sid}", mapping={"state": state, "checked_at_ms": int(time.time() * 1000) - age_ms})


def test_available_full_contract(factory):
    r = get(factory(), "P0042")
    assert r.status_code == 200 and r.mimetype == "application/json"
    assert r.headers["X-Release"] == "1.1.0"
    assert r.get_json() == {
        "product_id": "P0042", "status": "available", "sellable": 9, "confirmed_min": 9, "at_least": False,
        "last_changed_at": "2025-10-09T08:53:20.123Z", "restock_eta": None, "unknown_reason": None, "feed": "ok",
        "stores": [{"store_id": s, "status": "available", "quantity": q, "revision": rev, "feed": "ok", "live": True}
                   for s, q, rev in zip(STORES, (2, 1, 3, 1, 2), (810, 811, 812, 813, 814))],
        "product": {"name": "Trailrunner GTX", "brand": "Alpenpace", "size": "EU 42", "image_url": "/img/P0042.svg"},
        "release": "1.1.0"}


def test_missing_stores_and_not_stocked_make_it_at_least(factory):
    b = body(factory(), "P0001")
    assert (b["status"], b["sellable"], b["confirmed_min"], b["at_least"], b["unknown_reason"]) == \
        ("available", 12, 12, True, None)
    st = {e["store_id"]: e for e in b["stores"]}
    assert [st[s]["live"] for s in STORES] == [True, True, False, False, False]
    assert st["S01"]["status"] == "available" and st["S01"]["quantity"] == 12
    assert (st["S02"]["status"], st["S02"]["quantity"], st["S02"]["revision"]) == ("not_stocked", None, 9)
    assert (st["S03"]["status"], st["S03"]["quantity"], st["S03"]["revision"]) == ("unknown", None, None)


def test_out_of_stock_when_all_stores_known(factory):
    b = body(factory(), "P0002")
    assert (b["status"], b["sellable"], b["confirmed_min"], b["at_least"], b["unknown_reason"]) == \
        ("out_of_stock", 0, 0, False, None)
    assert b["last_changed_at"] == "2025-10-09T08:53:20.999Z"


def test_sellable_zero_with_store_unknown_is_unknown(factory, redis_client):
    redis_client.delete("stock:n1:S04:P0002")
    b = body(factory(), "P0002")
    assert (b["status"], b["sellable"], b["at_least"], b["unknown_reason"]) == ("unknown", 0, True, "stores_unknown")
    assert next(e for e in b["stores"] if e["store_id"] == "S04")["status"] == "unknown"


def test_sellable_zero_with_stale_feed_is_unknown(factory, redis_client):
    set_feed(redis_client, "S03", "stale")
    b = body(factory(), "P0002")
    assert (b["status"], b["at_least"], b["unknown_reason"], b["feed"]) == ("unknown", True, "stores_unknown", "stale")


def test_stale_store_feed_makes_available_at_least(factory, redis_client):
    set_feed(redis_client, "S02", "stale")
    b = body(factory(), "P0042")
    assert (b["status"], b["sellable"], b["confirmed_min"], b["at_least"], b["feed"]) == \
        ("available", 9, 8, True, "stale")
    s2 = next(e for e in b["stores"] if e["store_id"] == "S02")
    assert (s2["status"], s2["quantity"], s2["feed"], s2["live"]) == ("available", 1, "stale", False)


RELEASES = [("none", "1.0.0"), ("per_request", "1.1.0"), ("startup", "1.2.0")]


@pytest.mark.parametrize("mode,version", RELEASES)
def test_all_live_confirmed_min_is_the_flink_total(factory, mode, version):
    b = body(factory(mode, version), "P0042")
    assert (b["status"], b["sellable"], b["confirmed_min"], b["at_least"]) == ("available", 9, 9, False)
    assert all(e["live"] for e in b["stores"])


@pytest.mark.parametrize("mode,version", RELEASES)
def test_stale_store_with_stock_is_last_seen_not_counted(factory, redis_client, mode, version):
    """Lab 2.1: Bologna S03 paused with 3 units. The minimum counts only the live stores: 2+1+1+2 = 6."""
    set_feed(redis_client, "S03", "stale")
    app = factory(mode, version)
    b = body(app, "P0042")
    assert (b["status"], b["sellable"], b["confirmed_min"], b["at_least"], b["unknown_reason"]) == \
        ("available", 9, 6, True, None)
    s3 = next(e for e in b["stores"] if e["store_id"] == "S03")
    assert (s3["status"], s3["quantity"], s3["feed"], s3["live"]) == ("available", 3, "stale", False)
    assert [e["live"] for e in b["stores"]] == [True, True, False, True, True]
    assert ("stock.lookup.result", ("status:available", "unknown_reason:none", "at_least:true")) \
        in app.extensions["fake_statsd"].calls


@pytest.mark.parametrize("mode,version", RELEASES)
def test_stale_store_only_one_with_stock_is_unknown_not_out_of_stock(factory, redis_client, mode, version):
    """Every live store is at zero; only the quiet store's last seen value has stock."""
    for sid, q in zip(STORES, (0, 0, 3, 0, 0)):
        redis_client.hset(f"stock:n1:{sid}:P0042", mapping={"quantity": q, "revision": 900})
    redis_client.hset("sellable:P0042", "sellable", 3)
    set_feed(redis_client, "S03", "stale")
    b = body(factory(mode, version), "P0042")
    assert (b["status"], b["sellable"], b["confirmed_min"], b["at_least"], b["unknown_reason"]) == \
        ("unknown", 3, 0, True, "stores_unknown")
    assert b["status"] != "out_of_stock"


@pytest.mark.parametrize("mode,version", RELEASES)
def test_quiet_store_sold_out_during_outage(factory, redis_client, mode, version):
    """Bologna sells its 3 units while its feed is paused: the source has 6, the serving view still shows S03 = 3
    and Flink 9. The shop must never promise more than 6, and after resume it shows the true 6."""
    set_feed(redis_client, "S03", "stale")
    during = body(factory(mode, version), "P0042")
    assert (during["status"], during["sellable"], during["confirmed_min"], during["at_least"]) == \
        ("available", 9, 6, True)  # 6 is the true total while Bologna is quiet: never more
    # resume: the paused events arrive, S03 = 0, Flink = 6, feed ok again
    redis_client.hset("stock:n1:S03:P0042", mapping={"quantity": 0, "revision": 900})
    redis_client.hset("sellable:P0042", "sellable", 6)
    set_feed(redis_client, "S03", "ok")
    after = body(factory(mode, version), "P0042")
    assert (after["status"], after["sellable"], after["confirmed_min"], after["at_least"]) == ("available", 6, 6, False)


def test_confirmed_min_capped_by_flink_total(factory, redis_client):
    """Flink lags a sale at a live store: the minimum never exceeds the Flink total either."""
    set_feed(redis_client, "S03", "stale")
    redis_client.hset("sellable:P0042", "sellable", 5)
    b = body(factory(), "P0042")
    assert (b["status"], b["sellable"], b["confirmed_min"]) == ("available", 5, 5)


def test_unknown_answer_has_null_confirmed_min(factory, redis_client):
    redis_client.hset("stock:n1:meta", "ready", "0")
    b = body(factory(), "P0042")
    assert (b["status"], b["confirmed_min"]) == ("unknown", None)
    assert not any(e["live"] for e in b["stores"])


@pytest.mark.parametrize("state", ["stale", "unknown"])
def test_feed_states_per_store(factory, redis_client, state):
    set_feed(redis_client, "S05", state)
    b = body(factory(), "P0042")
    assert [e["feed"] for e in b["stores"]] == ["ok"] * 4 + [state] and b["feed"] == state


def test_feed_old_or_missing_is_unknown(factory, redis_client):
    set_feed(redis_client, "S01", "ok", age_ms=11_000)
    redis_client.delete("feed:status:S02")
    b = body(factory(), "P0042")
    assert [e["feed"] for e in b["stores"]][:2] == ["unknown", "unknown"]
    assert b["feed"] == "unknown" and b["at_least"] is True


def test_missing_sellable_key_is_unknown_not_found(factory, redis_client):
    redis_client.delete("sellable:P0042")
    b = body(factory(), "P0042")
    assert (b["status"], b["sellable"], b["unknown_reason"], b["last_changed_at"]) == ("unknown", None, "not_found", None)


def test_unknown_product_has_no_sellable(factory):
    b = body(factory(), "P9999")
    assert (b["status"], b["unknown_reason"], b["sellable"], b["product"]) == ("unknown", "not_found", None, None)


def test_not_ready_meta(factory, redis_client):
    redis_client.hset("stock:n1:meta", "ready", "0")
    b = body(factory(), "P0042")
    assert (b["status"], b["unknown_reason"], b["sellable"], b["at_least"]) == ("unknown", "not_ready", None, True)


def test_not_ready_namespace_missing(factory, redis_client):
    redis_client.delete("stock:active_ns")
    b = body(factory(), "P0042")
    assert (b["status"], b["unknown_reason"]) == ("unknown", "not_ready")
    assert len(b["stores"]) == 5 and all(e["status"] == "unknown" for e in b["stores"])


def test_active_ns_switch(factory, redis_client):
    redis_client.set("stock:active_ns", "n2")
    b = body(factory(), "P0042")
    assert (b["status"], b["unknown_reason"]) == ("unknown", "not_ready")


class BrokenRedis(fakeredis.FakeRedis):
    def pipeline(self, *a, **k):
        raise redis.ConnectionError("boom")


def test_redis_failure_is_unknown_not_500(factory, caplog):
    app = factory(client=BrokenRedis(decode_responses=True))
    with caplog.at_level("ERROR"):
        r = get(app, "P0042")
    assert r.status_code == 200
    b = r.get_json()
    assert (b["status"], b["unknown_reason"], b["sellable"], b["at_least"]) == ("unknown", "redis_error", None, True)
    assert len(b["stores"]) == 5
    assert any("boom" in rec.getMessage() and rec.exc_info for rec in caplog.records)
    assert ("stock.lookup.result", ("status:unknown", "unknown_reason:redis_error", "at_least:true")) \
        in app.extensions["fake_statsd"].calls


def test_malformed_sellable_is_500(factory, redis_client):
    redis_client.hset("sellable:P0042", "sellable", "many")
    app = factory()
    app.testing = False
    assert get(app, "P0042").status_code == 500


@pytest.mark.parametrize("pid", ["__probe__", "P42", "P00421", "p0042", "X0042"])
def test_ids_not_matching_are_404(factory, pid):
    assert get(factory(), pid).status_code == 404


def test_old_stock_route_removed(factory):
    assert factory().test_client().get("/api/stock/S03/P0042").status_code == 404


def test_metric_emitted(factory):
    app = factory()
    get(app, "P0042")
    get(app, "P0001")
    calls = app.extensions["fake_statsd"].calls
    assert ("stock.lookup.result", ("status:available", "unknown_reason:none", "at_least:false")) in calls
    assert ("stock.lookup.result", ("status:available", "unknown_reason:none", "at_least:true")) in calls


def test_stock_read_span_wraps_reads(factory, monkeypatch):
    import app.main as m
    names = []
    real = m.tracer.trace

    def spy(name, *a, **k):
        names.append(name)
        return real(name, *a, **k)
    monkeypatch.setattr(m.tracer, "trace", spy)
    get(factory(), "P0042")
    assert "stock.read" in names and "catalogue.prepare" in names


def test_release_1_0_0_has_null_product(factory):
    b = body(factory("none", "1.0.0"), "P0042")
    assert b["product"] is None and b["release"] == "1.0.0"


@pytest.mark.parametrize("mode,version", [("per_request", "1.1.0"), ("startup", "1.2.0")])
def test_product_present_in_1_1_0_and_1_2_0(factory, mode, version):
    assert body(factory(mode, version), "P0042")["product"]["name"] == "Trailrunner GTX"


def test_unknown_product_in_catalogue(factory, redis_client):
    redis_client.hset("sellable:P9998", mapping={"sellable": 3, "last_changed_at_ms": 1})
    assert body(factory(), "P9998")["product"] is None


def test_1_1_0_and_1_2_0_equivalent(factory):
    a, b = factory("per_request", "1.1.0"), factory("startup", "1.2.0")
    for p in IDS:
        ra, rb = get(a, p), get(b, p)
        ja, jb = ra.get_json(), rb.get_json()
        assert ja.pop("release") == "1.1.0" and jb.pop("release") == "1.2.0"
        assert json.dumps(ja) == json.dumps(jb)
        assert ra.headers["X-Release"] == "1.1.0" and rb.headers["X-Release"] == "1.2.0"


def test_startup_mode_does_not_reread_file(factory, monkeypatch):
    import app.catalogue as c
    app = factory("startup", "1.2.0")
    cat = app.extensions["catalogue"]
    assert cat.loads == 1
    calls = []
    real = c.build_index
    monkeypatch.setattr(c, "build_index", lambda p: calls.append(p) or real(p))
    for _ in range(5):
        get(app, "P0042")
    assert cat.loads == 1 and calls == []


def test_per_request_mode_rereads_file(factory, monkeypatch):
    import app.catalogue as c
    app = factory("per_request", "1.1.0")
    cat = app.extensions["catalogue"]
    assert cat.loads == 0
    calls = []
    real = c.build_index
    monkeypatch.setattr(c, "build_index", lambda p: calls.append(p) or real(p))
    for _ in range(3):
        get(app, "P0042")
    assert cat.loads == 3 and len(calls) == 3


def test_health_and_ready(factory):
    app = factory("startup", "1.2.0")
    c = app.test_client()
    assert c.get("/healthz").status_code == 200
    r = c.get("/readyz")
    assert r.status_code == 200
    assert len(r.get_json()["catalogue_sha256"]) == 64


def test_not_ready_before_startup_preparation(factory):
    app = factory("startup", "1.2.0", started=False)
    r = app.test_client().get("/readyz")
    assert r.status_code == 503
    assert app.test_client().get("/healthz").status_code == 200
    app.extensions["prepare"]()
    assert app.test_client().get("/readyz").status_code == 200


def test_readyz_redis_down(factory):
    class NoPing(fakeredis.FakeRedis):
        def ping(self, *a, **k):
            raise redis.ConnectionError("down")
    r = factory(client=NoPing(decode_responses=True)).test_client().get("/readyz")
    assert r.status_code == 503


def test_config_fails_loudly():
    from app.config import Config
    ok = {"REDIS_URL": "x", "DD_VERSION": "1", "CATALOGUE_MODE": "none", "STORE_HOSTS": "S01=a,S02=b"}
    assert Config(ok).stores == ["S01", "S02"]
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        Config({k: v for k, v in ok.items() if k != "REDIS_URL"})
    with pytest.raises(RuntimeError, match="CATALOGUE_MODE"):
        Config({**ok, "CATALOGUE_MODE": "bogus"})
    with pytest.raises(RuntimeError, match="STORE_HOSTS"):
        Config({k: v for k, v in ok.items() if k != "STORE_HOSTS"})


@pytest.mark.parametrize("raw,msg", [("S01", "S01=store-s01"), ("S01=", "S01=store-s01"), ("X1=a", "must look like S01"),
                                     ("S01=a,S01=b", "twice"), ("S01=a,,S02=b", "S01=store-s01")])
def test_store_hosts_parsed_strictly(raw, msg):
    from app.config import parse_store_hosts
    with pytest.raises(RuntimeError, match=msg):
        parse_store_hosts(raw)


def test_generator_is_deterministic_and_has_demo_product(catalogue_file, tmp_path):
    import subprocess, sys, os, hashlib
    out = tmp_path / "c.json"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, os.path.join(root, "catalogue", "generate.py"), "--seed", "42",
                    "--products", "400", "--out", str(out)], check=True, stderr=subprocess.DEVNULL)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(open(catalogue_file, "rb").read()).hexdigest()
    prods = {p["product_id"]: p for p in json.loads(out.read_text())["products"]}
    assert prods["P0042"]["name"] == "Trailrunner GTX" and prods["P0042"]["size"] == "EU 42"
    assert "P0200" in prods and "variants" not in prods["P0200"] and "variants" in prods["P0201"]


def test_restock_eta_iso_when_present_and_null_when_absent(factory, redis_client):
    app = factory()
    assert body(app, "P0002")["restock_eta"] is None
    redis_client.set("restock:eta:P0002", "1760000000123")
    b = body(app, "P0002")
    assert b["restock_eta"] == "2025-10-09T08:53:20.123Z"
    assert b["status"] == "out_of_stock"


def test_malformed_restock_eta_is_null_not_an_error(factory, redis_client):
    redis_client.set("restock:eta:P0002", "soon")
    r = get(factory(), "P0002")
    assert r.status_code == 200 and r.get_json()["restock_eta"] is None


# --- shared confirmed-minimum table: offer-worker runs the same cases (offer-worker/tests/test_stock.py) ---
STOCK_CASES = json.loads((Path(__file__).parents[2] / "contracts" / "stock-trust-cases.json").read_text())


def test_stock_case_table_matches_the_api_constants():
    from app.config import Config
    assert STOCK_CASES["stores"] == STORES
    assert Config({"REDIS_URL": "x", "DD_VERSION": "1", "CATALOGUE_MODE": "none",
                   "STORE_HOSTS": "S01=a"}).feed_max_age_s == STOCK_CASES["feed_max_age_s"]


@pytest.mark.parametrize("case", STOCK_CASES["cases"], ids=[c["name"] for c in STOCK_CASES["cases"]])
def test_shared_stock_case_table(factory, monkeypatch, case):
    now_ms = 1_800_000_000_000
    monkeypatch.setattr(time, "time", lambda: now_ms / 1000)  # exact feed ages (the 10 s boundary case)
    r = fakeredis.FakeRedis(decode_responses=True)
    pid = STOCK_CASES["product_id"]
    if case["ns"] is not None:
        r.set("stock:active_ns", case["ns"])
    ns = case["ns"] or "n1"
    r.hset(f"stock:{ns}:meta", mapping={"ready": "1" if case["ready"] else "0"})
    if case["sellable"] is not None:
        r.hset(f"sellable:{pid}", mapping={"product_id": pid, "sellable": case["sellable"],
                                           "stores_reporting": len(STORES), "last_changed_at_ms": 1760000000123})
    for sid, pos, feed in zip(STOCK_CASES["stores"], case["positions"], case["feeds"], strict=True):
        if pos is not None:
            r.hset(f"stock:{ns}:{sid}:{pid}", mapping={"quantity": 0 if pos == "not_stocked" else pos, "revision": 1,
                                                       "deleted": 1 if pos == "not_stocked" else 0,
                                                       "changed_at_ms": 1, "applied_at_ms": 1})
        if feed is not None:
            state, age = (feed["state"], feed["age_ms"]) if isinstance(feed, dict) else (feed, 0)
            r.hset(f"feed:status:{sid}", mapping={"state": state, "probe_age_ms": 0, "checked_at_ms": now_ms - age})
    b = body(factory(client=r), pid)
    got = {k: b[k] for k in ("confirmed_min", "at_least", "status", "unknown_reason")}
    assert got == case["expect"]
