from datetime import datetime, timezone

import pytest

from sellable_dev.core import Aggregator, CartAtRiskTracker, DemandWindow, ForecastTracker, Restocker, decide, to_ms


def st(store, product, qty, deleted=False, ms=1000):
    return {"store_id": store, "product_id": product, "quantity": qty, "deleted": deleted,
            "changed_at": datetime.fromtimestamp(ms / 1000, tz=timezone.utc)}


def test_multiple_stores_sum():
    a = Aggregator()
    a.apply(st("S01", "P1", 2, ms=1000))
    key, v = a.apply(st("S02", "P1", 3, ms=2500))
    assert key == {"product_id": "P1"}
    assert v == {"product_id": "P1", "sellable": 5, "stores_reporting": 2, "last_changed_at_ms": 2500}


def test_update_replaces_not_adds():
    a = Aggregator()
    a.apply(st("S01", "P1", 5))
    _, v = a.apply(st("S01", "P1", 1, ms=3000))
    assert v["sellable"] == 1 and v["stores_reporting"] == 1 and v["last_changed_at_ms"] == 3000


def test_deleted_counts_zero_and_not_reporting():
    a = Aggregator()
    a.apply(st("S01", "P1", 4))
    _, v = a.apply(st("S02", "P1", 9, deleted=True, ms=5000))
    assert v["sellable"] == 4 and v["stores_reporting"] == 1
    assert v["last_changed_at_ms"] == 5000  # MAX over all rows, like the Flink SQL


def test_all_deleted_gives_zero():
    a = Aggregator()
    _, v = a.apply(st("S01", "P1", 4, deleted=True))
    assert v["sellable"] == 0 and v["stores_reporting"] == 0


def test_products_are_independent_and_probe_included():
    a = Aggregator()
    a.apply(st("S01", "P1", 4))
    _, v = a.apply(st("S01", "__probe__", 777, ms=9000))
    assert v == {"product_id": "__probe__", "sellable": 777, "stores_reporting": 1, "last_changed_at_ms": 9000}
    _, v1 = a.apply(st("S02", "P1", 1))
    assert v1["sellable"] == 5


def test_missing_field_fails_loudly():
    with pytest.raises(ValueError, match="quantity"):
        Aggregator().apply({"store_id": "S01", "product_id": "P1", "deleted": False, "changed_at": 1})


def test_to_ms():
    assert to_ms(1234) == 1234
    assert to_ms(datetime(1970, 1, 1, 0, 0, 1, 500000, tzinfo=timezone.utc)) == 1500
    with pytest.raises(ValueError):
        to_ms("x")


def rs(store, product, qty, rev, deleted=False, ms=1000):
    d = st(store, product, qty, deleted, ms)
    d["revision"] = rev
    return d


CFG = {"safety_factor": 1.2, "coverage_h": 24, "min_order_qty": 5, "default_demand_per_hour": 1.0, "lead_time_s": 3600,
       "time_compression": 1}  # C=1: business time equals real time (the C=60 cases are tested separately)


def cfg_rows(**over):
    c = {**CFG, **over}
    return [{"key": k, "value": float(v), "updated_at_ms": 0} for k, v in c.items()]


def loaded(**over):
    r = Restocker()
    for row in cfg_rows(**over):
        r.on_config(row)
    return r


def test_no_demand_no_forecast_uses_defaults():
    # default demand 1/h, base lead 1 h: rop = ceil(1 * 1 * 1.2) = 2, need = ceil(1 * 25) - 2 = 23
    assert decide(2, 0, None, None, 0, CFG) == 23
    assert decide(3, 0, None, None, 0, CFG) is None


def test_incomplete_config_requests_nothing():
    assert decide(0, 0, None, None, 0, {"safety_factor": 1.2}) is None
    r = Restocker()
    assert r.on_state(rs("S01", "P1", 0, 1)) == []


def test_position_includes_on_order():
    assert decide(0, 0, None, None, 0, CFG) is not None
    assert decide(0, 30, None, None, 0, CFG) is None  # 30 on order covers the rop of 2


def test_demand_raises_reorder_point():
    assert decide(10, 0, 1.0, None, 0, CFG) is None
    # demand 10/h, lead 1 h: rop = ceil(12) = 12 >= 10, need = ceil(10 * 25) - 10 = 240
    assert decide(10, 0, 10.0, None, 0, CFG) == 240


def test_observed_lead_time_replaces_base_only_with_deliveries():
    # forecast lead 7200 s with deliveries: rop = ceil(1 * 2 * 1.2) = 3
    assert decide(3, 0, 1.0, 7200.0, 2, CFG) is not None
    assert decide(3, 0, 1.0, 7200.0, 0, CFG) is None  # deliveries = 0 -> base 3600 s, rop 2


def test_quantity_formula_and_min_order():
    # need = ceil(0.1 * 25) - 0 = 3 < min 5 -> 5; quantity 0 forces a request although rop = 1
    assert decide(0, 0, 0.1, None, 0, CFG) == 5
    # rop == 0 and stock left: no request
    assert decide(1, 0, 0.0, None, 0, CFG) is None
    # zero stock with zero demand: still requests the minimum
    assert decide(0, 0, 0.0, None, 0, CFG) == 5


def test_no_repeat_while_on_order_and_no_duplicate_emit():
    r = loaded()
    out = r.on_state(rs("S01", "P1", 1, 7))
    assert len(out) == 1 and out[0][0] == {"request_id": "S01|P1|7"}
    assert out[0][1]["quantity_requested"] == 24 and out[0][1]["requested_at_ms"] == 1000
    assert r.on_state(rs("S01", "P1", 1, 7)) == []  # redelivery of the same revision
    # the order shows up as on-order: position 25 > rop, nothing new
    f = {"store_id": "S01", "product_id": "P1", "lead_time_s": 0.0, "on_order": 24, "deliveries": 0}
    assert r.on_forecast(f) == []
    assert r.on_state(rs("S01", "P1", 0, 8)) == []  # sold again, position 24 still above rop


def test_restock_cycle_new_revision_new_request():
    r = loaded()
    assert len(r.on_state(rs("S01", "P1", 0, 1))) == 1
    assert r.on_state(rs("S01", "P1", 30, 2)) == []
    out = r.on_state(rs("S01", "P1", 0, 3))
    assert out[0][0] == {"request_id": "S01|P1|3"}


def test_probe_and_deleted_excluded():
    r = loaded()
    assert r.on_state(rs("S01", "__probe__", 0, 1)) == []
    assert r.on_state(rs("S01", "P1", 0, 1, deleted=True)) == []


def test_config_change_reevaluates_positions():
    r = loaded()
    assert r.on_state(rs("S01", "P1", 3, 1)) == []
    out = r.on_config({"key": "safety_factor", "value": 2.0, "updated_at_ms": 1})  # rop = ceil(2) = 2 still
    assert out == []
    out = r.on_config({"key": "default_demand_per_hour", "value": 5.0, "updated_at_ms": 2})  # rop = 6
    assert len(out) == 1 and out[0][1]["quantity_requested"] == 5 * 25 - 3 + 0


def test_demand_event_triggers_request():
    r = loaded()
    assert r.on_state(rs("S01", "P1", 10, 1)) == []
    out = r.on_demand({"store_id": "S01", "product_id": "P1", "units_per_hour": 10.0, "window_end_ms": 1})
    assert len(out) == 1 and out[0][1]["quantity_requested"] == 240


def mv(store, product, delta, ms, kind=None):
    return {"store_id": store, "product_id": product, "delta": delta, "changed_at_ms": ms,
            "kind": kind or ("SALE" if delta < 0 else "RESTOCK")}


def test_demand_window_sums_sales_times_six():
    w = DemandWindow()
    w.apply(mv("S01", "P1", -2, 0))
    key, v = w.apply(mv("S01", "P1", -1, 60_000))
    assert key == {"store_id": "S01", "product_id": "P1"}
    assert v == {"store_id": "S01", "product_id": "P1", "units_per_hour": 18.0, "window_end_ms": 60_000}


def test_demand_window_slides_and_ignores_other_kinds():
    w = DemandWindow()
    w.apply(mv("S01", "P1", -5, 0))
    assert w.apply(mv("S01", "P1", 9, 1000)) is None
    assert w.apply(mv("S01", "P1", -3, 2000, kind="ADJUST")) is None
    assert w.apply(mv("S01", "__probe__", -1, 3000)) is None
    _, v = w.apply(mv("S01", "P1", -1, 10 * 60_000 + 1))  # the first sale left the window
    assert v["units_per_hour"] == 6.0
    _, other = w.apply(mv("S02", "P1", -1, 10 * 60_000 + 2))
    assert other["units_per_hour"] == 6.0  # per store


def order(rid, qty=10, req=0, delivered=None, cancelled=None, op="c", store="S01", product="P1"):
    row = {"request_id": rid, "store_id": store, "product_id": product, "quantity_requested": qty,
           "requested_at_ms": req, "delivered_at": delivered, "cancelled_at": cancelled}
    return {"op": op, "before": None, "after": row}


def test_forecast_on_order_and_cancel():
    t = ForecastTracker()
    t.apply(order("a", 10))
    key, v = t.apply(order("b", 5))
    assert v == {"store_id": "S01", "product_id": "P1", "lead_time_s": 0.0, "on_order": 15, "deliveries": 0}
    _, v = t.apply(order("b", 5, cancelled=1000, op="u"))
    assert v["on_order"] == 10


def test_forecast_lead_time_is_average_of_last_five():
    t = ForecastTracker()
    # six deliveries, lead 100 s for the oldest, 10 s for the five most recent
    t.apply(order("old", req=0, delivered=100_000))
    for i in range(5):
        t.apply(order(f"n{i}", req=1_000_000 + i, delivered=1_010_000 + i))
    _, v = t.apply(order("open", 7, req=2_000_000))
    assert v["lead_time_s"] == 10.0 and v["deliveries"] == 6 and v["on_order"] == 7


def test_forecast_debezium_timestamp_string_and_envelope_check():
    t = ForecastTracker()
    _, v = t.apply(order("a", req=0, delivered="1970-01-01T00:00:30Z"))
    assert v["lead_time_s"] == 30.0
    with pytest.raises(ValueError, match="Debezium"):
        t.apply({"request_id": "x"})
    assert t.apply({"op": "d", "before": None, "after": None}) is None


# --- demo clock (contracts 13b): C business hours per real hour ----------------------------------------------------
CFG60 = {**CFG, "time_compression": 60, "default_demand_per_hour": 0.12, "lead_time_s": 48 * 3600}


def test_demand_is_converted_from_real_to_business_hours():
    # 0.12 units per real minute = 7.2 per real hour = 0.12 per business hour at C=60. Observed lead time 48 real
    # minutes = 48 business hours. rop = ceil(0.12 * 48 * 1.2) = ceil(6.9) = 7: stock 7 requests, stock 8 does not.
    real_units_per_hour = 0.12 * 60
    real_lead_s = 48 * 60
    assert decide(7, 0, real_units_per_hour, real_lead_s, 3, CFG60) is not None
    assert decide(8, 0, real_units_per_hour, real_lead_s, 3, CFG60) is None
    # without the conversion the demand would be 60 x too high and 8 units would trigger a request
    assert decide(7, 0, None, None, 0, CFG60) is not None  # defaults: 0.12 per business h, 48 business h -> rop 7
    assert decide(8, 0, None, None, 0, CFG60) is None


def test_quantity_uses_business_coverage():
    # rop 7, position 0: need = ceil(0.12 * (48 + 24)) - 0 = ceil(8.64) = 9
    assert decide(0, 0, 0.12 * 60, 48 * 60, 3, CFG60) == 9


def test_changing_c_reevaluates_positions():
    r = loaded(time_compression=1, default_demand_per_hour=0.12, lead_time_s=48 * 3600)
    assert r.on_state(rs("S01", "P1", 7, 1)) != []  # C=1 default path: rop 7
    r.on_demand({"store_id": "S01", "product_id": "P1", "units_per_hour": 7.2, "window_end_ms": 0})
    r.on_forecast({"store_id": "S01", "product_id": "P1", "lead_time_s": 2880.0, "on_order": 0, "deliveries": 3})
    # now C=60: demand 7.2 / 60 = 0.12 per business hour, lead 2880 * 60 s = 48 h -> still rop 7, same quantity
    out = r.on_config({"key": "time_compression", "value": 60.0, "updated_at_ms": 0})
    # re-evaluated with the new C: needed = ceil(0.12 * 72) - 7 = 2 < min_order_qty 5
    assert len(out) == 1 and out[0][1]["quantity_requested"] == 5


# ---- cart at risk (mirror of cart_at_risk.sql)

T0 = 10_000_000


def cart(ev="ADD", cart_id="C1", product="P1", store="ONLINE", ms=T0, scenario="sc1"):
    return {"event_id": "e", "scenario_id": scenario, "cart_id": cart_id, "shopper_id": "SH1",
            "store_id": store, "product_id": product, "event_type": ev, "event_time": ms,
            "cart_value_eur": 189.5, "returning_shopper": True, "item_count": 3}


def sell(product="P1", qty=0, changed=T0 - 1000):
    return {"product_id": product, "sellable": qty, "stores_reporting": 1, "last_changed_at_ms": changed}


def test_risk_when_cart_then_zero_stock():
    t = CartAtRiskTracker()
    assert t.on_cart(cart(), T0) == []
    (key, val), = t.on_sellable(sell(changed=T0 + 500), T0 + 600)
    assert key == {"scenario_id": "sc1", "cart_id": "C1", "product_id": "P1"}
    assert val == {"risk_id": "sc1|C1|P1|%d" % (T0 + 500), "scenario_id": "sc1", "cart_id": "C1", "shopper_id": "SH1",
                   "product_id": "P1", "cart_value_eur": 189.5, "returning_shopper": True, "item_count": 3,
                   "sellable_changed_at_ms": T0 + 500, "detected_at": T0 + 500}


def test_risk_when_zero_stock_then_cart_detected_at_is_cart_time():
    t = CartAtRiskTracker()
    t.on_sellable(sell(changed=T0 - 5000), T0)
    (_, val), = t.on_cart(cart(ms=T0), T0)
    assert val["detected_at"] == T0 and val["sellable_changed_at_ms"] == T0 - 5000


def test_old_cart_event_without_shopper_signals_uses_contract_defaults():
    old_event = cart()
    for field in ("cart_value_eur", "returning_shopper", "item_count"):
        del old_event[field]
    t = CartAtRiskTracker()
    t.on_cart(old_event, T0)
    (_, value), = t.on_sellable(sell(), T0)
    assert value is not None
    assert (value["cart_value_eur"], value["returning_shopper"], value["item_count"]) == (0.0, False, 1)


def test_no_risk_with_stock_or_without_stock_row_or_store_event():
    t = CartAtRiskTracker()
    assert t.on_sellable(sell(qty=3), T0) == []
    assert t.on_cart(cart(), T0) == []
    assert t.on_cart(cart(product="P9"), T0) == []
    t.on_sellable(sell(product="P2", qty=0), T0)
    assert t.on_cart(cart(store="S01", product="P2"), T0) == []


def test_zero_with_no_stores_reporting_still_counts():
    t = CartAtRiskTracker()
    t.on_cart(cart(), T0)
    out = t.on_sellable({"product_id": "P1", "sellable": 0, "stores_reporting": 0, "last_changed_at_ms": T0}, T0)
    assert len(out) == 1 and out[0][1] is not None


def test_not_reemitted_when_identical():
    t = CartAtRiskTracker()
    t.on_cart(cart(), T0)
    assert len(t.on_sellable(sell(), T0)) == 1
    assert t.on_sellable(sell(), T0 + 1) == []
    assert t.on_cart(cart(ms=T0), T0 + 2) == []
    assert t.sweep(T0 + 3) == []


def test_abandon_retracts():
    t = CartAtRiskTracker()
    t.on_sellable(sell(), T0)
    (k, _), = t.on_cart(cart(), T0)
    assert t.on_cart(cart("ABANDON", ms=T0 + 5), T0 + 5) == [(k, None)]
    assert t.on_cart(cart("ABANDON", ms=T0 + 6), T0 + 6) == []


def test_restock_retracts_and_zero_again_new_risk():
    t = CartAtRiskTracker()
    t.on_cart(cart(), T0)
    (k1, _), = t.on_sellable(sell(changed=T0), T0)
    assert t.on_sellable(sell(qty=4, changed=T0 + 10), T0 + 10) == [(k1, None)]
    (k2, v2), = t.on_sellable(sell(qty=0, changed=T0 + 20), T0 + 20)
    assert k2 == k1 and v2["sellable_changed_at_ms"] == T0 + 20
    assert v2["risk_id"] == "sc1|C1|P1|%d" % (T0 + 20)


def test_zero_to_zero_newer_change_updates_the_item_key_with_a_new_risk_id():
    t = CartAtRiskTracker()
    t.on_cart(cart(), T0)
    (k1, v1), = t.on_sellable(sell(changed=T0), T0)
    (k2, v2), = t.on_sellable(sell(changed=T0 + 7), T0 + 7)
    assert k2 == k1 and v2 is not None and v2["sellable_changed_at_ms"] == T0 + 7
    assert v2["risk_id"] != v1["risk_id"]


def test_window_expiry_and_stale_cart():
    t = CartAtRiskTracker()
    t.on_sellable(sell(), T0)
    (k, _), = t.on_cart(cart(ms=T0), T0)
    assert t.sweep(T0 + 30 * 60 * 1000 - 1) == []
    assert t.sweep(T0 + 30 * 60 * 1000) == [(k, None)]
    assert t.sweep(T0 + 31 * 60 * 1000) == []
    assert t.on_cart(cart(cart_id="C2", ms=T0), T0 + 40 * 60 * 1000) == []


def test_carts_are_independent_per_scenario_cart_product():
    t = CartAtRiskTracker()
    t.on_sellable(sell(), T0)
    t.on_cart(cart(), T0)
    t.on_cart(cart(cart_id="C2"), T0)
    t.on_cart(cart(scenario="sc0"), T0)
    out = t.on_cart(cart("ABANDON", cart_id="C2", ms=T0 + 1), T0 + 1)
    assert out == [({"scenario_id": "sc1", "cart_id": "C2", "product_id": "P1"}, None)]


def test_cart_event_validation_fails_loudly():
    with pytest.raises(ValueError, match="event_type"):
        CartAtRiskTracker().on_cart({**cart(), "event_type": "REMOVE"}, T0)
    bad = cart()
    del bad["shopper_id"]
    with pytest.raises(ValueError, match="shopper_id"):
        CartAtRiskTracker().on_cart(bad, T0)
