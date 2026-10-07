"""Confirmed minimum: the API's trust rule in the offer worker. The shared table
contracts/stock-trust-cases.json is also run by inventory-api's tests against the real endpoint."""
import json

import fakeredis
import pytest
import redis

from conftest import NOW, STOCK_CASES, STORES, FakeMetrics, make_cfg, risk, set_feed, set_position
from offer_worker.policy import build_candidates, rule_default
from offer_worker.stock import FEED_MAX_AGE_S, ConfirmedStock, MalformedStock
from offer_worker.worker import OfferWorker

TABLE = json.loads(STOCK_CASES.read_text())


def load_case(r, case, product_id, stores):
    """Write one table case into Redis exactly as the projector, the probe and the sink would."""
    if case["ns"] is not None:
        r.set("stock:active_ns", case["ns"])
    ns = case["ns"] or "n1"
    r.hset(f"stock:{ns}:meta", mapping={"ready": "1" if case["ready"] else "0"})
    if case["sellable"] is not None:
        r.hset(f"sellable:{product_id}", mapping={"product_id": product_id, "sellable": case["sellable"],
                                                  "stores_reporting": len(stores), "last_changed_at_ms": 1})
    for sid, pos, feed in zip(stores, case["positions"], case["feeds"], strict=True):
        if pos == "not_stocked":
            set_position(r, sid, product_id, 0, deleted=1, ns=ns)
        elif pos is not None:
            set_position(r, sid, product_id, pos, ns=ns)
        if isinstance(feed, dict):
            set_feed(r, sid, feed["state"], feed["age_ms"])
        elif feed is not None:
            set_feed(r, sid, feed)


def test_table_matches_the_offer_worker_constants():
    assert TABLE["feed_max_age_s"] == FEED_MAX_AGE_S
    assert TABLE["stores"] == STORES


@pytest.mark.parametrize("decode", [False, True], ids=["bytes", "str"])
@pytest.mark.parametrize("case", TABLE["cases"], ids=[c["name"] for c in TABLE["cases"]])
def test_shared_case_table(case, decode):
    r = fakeredis.FakeRedis(decode_responses=decode)
    load_case(r, case, TABLE["product_id"], TABLE["stores"])
    a = ConfirmedStock(r, TABLE["stores"], lambda: NOW).read(TABLE["product_id"])
    got = {"confirmed_min": a.confirmed_min, "at_least": a.at_least, "status": a.status, "unknown_reason": a.unknown_reason}
    assert got == case["expect"]


def test_redis_error_propagates_to_the_caller():
    class Down:
        def pipeline(self, transaction=False):
            raise redis.ConnectionError("down")

    with pytest.raises(redis.RedisError):
        ConfirmedStock(Down(), STORES, lambda: NOW).read("P0042")


def test_malformed_position_is_reported():
    r = fakeredis.FakeRedis()
    case = next(c for c in TABLE["cases"] if c["name"] == "all_live_is_the_flink_total")
    load_case(r, case, "P0042", STORES)
    r.hset("stock:n1:S02:P0042", "quantity", "two")
    with pytest.raises(MalformedStock):
        ConfirmedStock(r, STORES, lambda: NOW).read("P0042")


# --- in the worker ------------------------------------------------------------------------------------------------
def make_worker(catalogue, redis_client, jev=None):
    out, m = [], FakeMetrics()
    w = OfferWorker(make_cfg(), redis_client, catalogue, lambda k, v: out.append((k, v)), m, jev, None, clock=lambda: NOW)
    return w, out, m


def only_in_quiet_store(r, pid, quantity=3, quiet="S03"):
    """pid's only stock sits in `quiet`, whose feed has stopped; the Flink total still counts it."""
    for s in STORES:
        set_position(r, s, pid, quantity if s == quiet else 0)
    r.hset(f"sellable:{pid}", "sellable", quantity)
    set_feed(r, quiet, "stale")


def test_build_candidates_skips_an_alternative_held_only_by_a_stale_store(catalogue, redis_client):
    w, _, m = make_worker(catalogue, redis_client)
    before = rule_default(build_candidates(catalogue["P0042"], catalogue, w._confirmed_min, 10))
    assert before.id == "alt:P0160"

    only_in_quiet_store(redis_client, "P0160")
    cands = build_candidates(catalogue["P0042"], catalogue, w._confirmed_min, 10)

    assert "alt:P0160" not in [c.id for c in cands]
    assert ("stock_unconfirmed", "stores_unknown") in m.calls
    # Other alternatives still have live stock in S01, S02, S04, S05 (S03 is quiet for them too).
    assert rule_default(cands).product_id not in (None, "P0160")


def test_every_alternative_only_in_a_stale_store_means_no_offer(catalogue, redis_client):
    for pid in catalogue:
        if pid != "P0042":
            only_in_quiet_store(redis_client, pid)
    w, out, _ = make_worker(catalogue, redis_client)
    assert w.handle(risk()) == "published"
    o = out[0][1]
    assert o["product_id"] is None and o["chosen_choice"] is None and o["decision_reason"] == "no_alternative"


def test_store_going_quiet_before_publish_fails_revalidation(catalogue, redis_client):
    from offer_worker.jev import JevChoice

    class GoesQuiet:
        def choose(self, state, instructions, criteria):
            alt = next(k for k in criteria if k.startswith("alt:"))
            only_in_quiet_store(redis_client, alt[4:])  # its stock now only in a quiet store
            return JevChoice(alt, 0.99)

    w, out, _ = make_worker(catalogue, redis_client, GoesQuiet())
    w.handle(risk())
    o = out[0][1]
    assert (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", "invalid_choice")
    assert o["jev_choice"].startswith("alt:") and o["chosen_choice"] != o["jev_choice"]  # the quiet one is never offered


@pytest.mark.parametrize("break_it,reason", [
    (lambda r: r.hset("stock:n1:meta", "ready", "0"), "not_ready"),
    (lambda r: r.delete("stock:active_ns"), "not_ready"),
    (lambda r: r.delete(*r.keys("sellable:*")), "not_found"),
    (lambda r: [r.hset(k, "quantity", "x") for k in r.keys("stock:n1:S01:*")], "malformed"),
])
def test_uncomputable_stock_is_never_eligible_and_is_counted(catalogue, redis_client, break_it, reason):
    break_it(redis_client)
    w, out, m = make_worker(catalogue, redis_client)
    w.handle(risk())
    assert out[0][1]["offer_type"] == "NOTIFY_ME"
    assert ("stock_unconfirmed", reason) in m.calls


def test_redis_error_is_never_eligible_and_is_counted(catalogue, redis_client, monkeypatch, caplog):
    w, out, m = make_worker(catalogue, redis_client)

    def down(product_id):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(w._stock, "read", down)
    w.handle(risk())
    assert out[0][1]["offer_type"] == "NOTIFY_ME"
    assert ("stock_unconfirmed", "redis_error") in m.calls
    assert any("stock lookup failed" in r.message for r in caplog.records)


def test_feed_older_than_ten_seconds_is_not_live(catalogue, redis_client):
    w, _, _ = make_worker(catalogue, redis_client)
    only_in_quiet_store(redis_client, "P0160")
    set_feed(redis_client, "S03", "ok", age_ms=FEED_MAX_AGE_S * 1000 + 1)  # ok, but too old
    assert w._confirmed_min("P0160") == 0
    set_feed(redis_client, "S03", "ok", age_ms=0)
    assert w._confirmed_min("P0160") == 3
