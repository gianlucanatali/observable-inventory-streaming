import logging

import pytest

from supplier_sim.config import Config, parse_store_hosts
from dataclasses import replace

from supplier_sim.core import Order, Supplier, draw_lead_time_s, due_ms, product_factor


class FakeProcurement:
    def __init__(self, orders, lead="60", jitter="20"):
        self.orders, self.lead, self.jitter, self.fail_mark = {o.request_id: o for o in orders}, lead, jitter, set()
        self.delivered, self.drawn_calls, self.fail_draw = [], [], False

    def jitter_pct_raw(self):
        return self.jitter

    def set_drawn(self, rid, drawn_s, base_s):
        if self.fail_draw:
            raise RuntimeError("db down")
        self.drawn_calls.append((rid, drawn_s, base_s))
        self.orders[rid] = replace(self.orders[rid], drawn_s=drawn_s, base_at_draw_s=base_s)

    def lead_time_raw(self):
        return self.lead

    def open_orders(self):
        return list(self.orders.values())

    def mark_delivered(self, rid):
        if rid in self.fail_mark:
            raise RuntimeError("db down")
        self.delivered.append(rid)
        del self.orders[rid]


class FakeSources:
    def __init__(self, down=()):
        self.calls, self.down = [], set(down)

    def restock(self, store, product, qty):
        if store in self.down:
            raise RuntimeError(f"{store} down")
        self.calls.append((store, product, qty))


class FakeEtas:
    def __init__(self, existing=()):
        self.etas = {p: 1 for p in existing}

    def set_eta(self, p, ms):
        self.etas[p] = ms

    def clear_eta(self, p):
        self.etas.pop(p, None)

    def eta_products(self):
        return set(self.etas)


class FakeMetrics:
    def __init__(self):
        self.open, self.delivered_stores, self.lead = None, [], None

    def orders_open(self, n):
        self.open = n

    def delivered(self, s):
        self.delivered_stores.append(s)

    def lead_time(self, s):
        self.lead = s


def o(rid, store="S01", product="P1", t=0, q=10, drawn=60.0, base=60):
    """Order already drawn at base 60 s with drawn lead 60 s (ratio 1), unless drawn=None."""
    return Order(rid, store, product, q, t, drawn, None if drawn is None else base)


def build(orders, lead="60", now=100_000, down=(), etas=(), jitter="20", c=1.0):
    p, s, e, m = FakeProcurement(orders, lead, jitter), FakeSources(down), FakeEtas(etas), FakeMetrics()
    clock = {"now": now, "c": c}
    return p, s, e, m, Supplier(p, s, e, m, 999, lambda: clock["now"], lambda: clock["c"]), clock


def test_due_delivered_not_due_kept():
    p, s, e, m, sup, _ = build([o("a", t=40_000), o("b", t=50_000)])  # lead 60 s, now 100 s: a due at 100 s
    sup.cycle()
    assert s.calls == [("S01", "P1", 10)] and p.delivered == ["a"]
    assert m.delivered_stores == ["S01"] and m.open == 1 and m.lead == 60
    assert e.etas == {"P1": 110_000}


def test_lead_time_change_applies_to_open_orders():
    p, s, e, m, sup, _ = build([o("a", t=90_000)], lead="172800")
    sup.cycle()
    assert s.calls == [] and e.etas == {"P1": 90_000 + 172_800_000} and m.open == 1
    p.lead = "5"
    sup.cycle()
    assert s.calls == [("S01", "P1", 10)] and m.open == 0 and e.etas == {}


def test_eta_is_earliest_per_product_and_stale_cleared():
    _, _, e, _, sup, _ = build([o("a", t=80_000, product="P1"), o("b", t=70_000, product="P1"),
                                o("c", t=95_000, product="P2")], etas=["P9"])
    sup.cycle()
    assert e.etas == {"P1": 130_000, "P2": 155_000}  # P9 removed, earliest due of P1 is b


def test_one_store_down_does_not_stop_others(caplog):
    p, s, e, m, sup, _ = build([o("a", store="S01"), o("b", store="S02")], down=["S01"])
    with caplog.at_level(logging.ERROR):
        sup.cycle()
    assert s.calls == [("S02", "P1", 10)] and p.delivered == ["b"]
    assert "a" in p.orders and m.open == 1 and "S01" in caplog.text
    assert e.etas == {"P1": 60_000}   # failed order keeps its ETA (already overdue)
    s.down.clear()
    sup.cycle()
    assert p.delivered == ["b", "a"] and e.etas == {}


def test_mark_failure_keeps_order_open_and_logs(caplog):
    p, s, e, m, sup, _ = build([o("a")])
    p.fail_mark.add("a")
    with caplog.at_level(logging.ERROR):
        sup.cycle()
    assert "a" in p.orders and m.delivered_stores == [] and "delivered again" in caplog.text


def test_missing_lead_time_row_uses_default_loudly(caplog):
    p, s, e, m, sup, _ = build([o("a", t=99_000)], lead=None)  # default 999 s
    with caplog.at_level(logging.ERROR):
        sup.cycle()
    assert "DEFAULT_LEAD_TIME_S" in caplog.text and m.lead == 999 and s.calls == []


def test_invalid_lead_time_raises():
    *_, sup, _ = build([o("a")], lead="soon")
    with pytest.raises(ValueError, match="lead_time_s"):
        sup.cycle()


def test_config_missing_and_store_hosts():
    with pytest.raises(SystemExit, match="STORE_HOSTS"):
        Config.from_env({})
    assert parse_store_hosts("S01=a,S02=b") == {"S01": "a", "S02": "b"}
    for bad in ("S01", "S01=a,S01=b", "S01=a, S02=b"):
        with pytest.raises(SystemExit):
            parse_store_hosts(bad)


def test_factor_is_deterministic_and_in_range():
    fs = [product_factor(f"P{i:04d}") for i in range(1, 201)]
    assert all(0.5 <= f <= 2.0 for f in fs)
    assert fs == [product_factor(f"P{i:04d}") for i in range(1, 201)]
    assert min(fs) < 0.7 and max(fs) > 1.8  # actually spread over the range


def test_draw_within_jitter_band_and_deterministic():
    base, pct = 1000, 20
    f = product_factor("P0007")
    ds = [draw_lead_time_s(f"S01|P0007|{r}", "P0007", base, pct) for r in range(300)]
    assert all(base * f * 0.8 - 1e-6 <= d <= base * f * 1.2 + 1e-6 for d in ds)
    assert max(ds) - min(ds) > 0.3 * base * f  # jitter really varies
    assert ds == [draw_lead_time_s(f"S01|P0007|{r}", "P0007", base, pct) for r in range(300)]
    assert draw_lead_time_s("x", "P0007", base, 0) == pytest.approx(base * f)


def test_first_sighting_draws_stores_and_keeps_it():
    p, s, e, m, sup, clock = build([o("a", product="P0007", t=0, drawn=None)], lead="1000", now=1)
    sup.cycle()
    [(rid, drawn, base)] = p.drawn_calls
    assert rid == "a" and base == 1000
    assert drawn == pytest.approx(draw_lead_time_s("a", "P0007", 1000, 20))
    assert e.etas["P0007"] == int(drawn * 1000)
    p.jitter = "90"
    sup.cycle()
    assert len(p.drawn_calls) == 1  # never redrawn once stored


def test_base_change_rescales_open_order_consistently():
    ord_ = o("a", t=0, drawn=120.0, base=60)  # drawn 120 s at base 60 (factor x jitter = 2)
    assert due_ms(ord_, 60) == 120_000
    assert due_ms(ord_, 30) == 60_000
    assert due_ms(ord_, 600) == 1_200_000
    p, s, e, m, sup, clock = build([ord_], lead="60", now=100_000)
    sup.cycle()
    assert s.calls == [] and e.etas == {"P1": 120_000}
    p.lead = "30"  # halves every open order's remaining wait
    sup.cycle()
    assert s.calls == [("S01", "P1", 10)] and e.etas == {}


def test_draw_store_failure_keeps_in_memory_draw(caplog):
    p, s, e, m, sup, _ = build([o("a", t=0, drawn=None)], lead="60", now=1)
    p.fail_draw = True
    with caplog.at_level(logging.ERROR):
        sup.cycle()
    assert "storing the drawn lead time" in caplog.text and m.open == 1 and "P1" in e.etas


def test_missing_jitter_row_uses_default_once(caplog):
    p, s, e, m, sup, _ = build([o("a", t=0, drawn=None)], jitter=None, now=1)
    with caplog.at_level(logging.ERROR):
        sup.cycle()
        sup.cycle()
    assert caplog.text.count("lead_time_jitter_pct") == 1


def test_invalid_jitter_raises():
    *_, sup, _ = build([o("a")], jitter="200")
    with pytest.raises(ValueError, match="lead_time_jitter_pct"):
        sup.cycle()


# --- demo clock (contracts 13b) ------------------------------------------------------------------------------------
def test_real_wait_is_business_lead_time_over_c():
    # business lead 48 h = 172800 s, C=60 -> real wait 2880 s = 48 min; ETA stays a real epoch time
    p, s, e, m, sup, clock = build([o("a", t=0, drawn=172800.0, base=172800)], lead="172800", now=2_879_000, c=60.0)
    sup.cycle()
    assert s.calls == [] and e.etas == {"P1": 2_880_000} and m.lead == 172800
    clock["now"] = 2_880_000
    sup.cycle()
    assert s.calls == [("S01", "P1", 10)] and e.etas == {}


def test_change_of_c_applies_to_open_orders():
    p, s, e, m, sup, clock = build([o("a", t=0, drawn=172800.0, base=172800)], lead="172800", now=60_000, c=60.0)
    sup.cycle()
    assert s.calls == [] and e.etas == {"P1": 2_880_000}
    clock["c"] = 3600.0  # 1 s of real time = 1 business hour: wait is now 48 s, already over at t=60 s
    sup.cycle()
    assert s.calls == [("S01", "P1", 10)] and e.etas == {}
    # and slowing the clock down stretches an order that is still open
    p2, s2, e2, _, sup2, clock2 = build([o("b", t=0, drawn=172800.0, base=172800)], lead="172800", now=60_000, c=3600.0)
    clock2["c"] = 60.0
    sup2.cycle()
    assert s2.calls == [] and e2.etas == {"P1": 2_880_000}


def test_invalid_c_fails_the_cycle():
    _, _, _, _, sup, _ = build([o("a")], c=0.0)
    with pytest.raises(ValueError, match="time_compression"):
        sup.cycle()


def test_redis_compression_reads_live_and_falls_back_once(caplog):
    from supplier_sim.adapters import RedisCompression

    class FakeRedis:
        def __init__(self):
            self.h = {}

        def hget(self, name, field):
            return self.h.get((name, field))

        def hset(self, name, field, value):
            self.h[(name, field)] = value

    r = FakeRedis()
    comp = RedisCompression(r)
    with caplog.at_level(logging.ERROR, logger="supplier_sim"):
        assert comp() == 60.0 and comp() == 60.0
    assert len([x for x in caplog.records if "time_compression" in x.getMessage()]) == 1
    r.hset("demo:config", "time_compression", "120")
    assert comp() == 120.0
    r.hset("demo:config", "time_compression", "abc")
    with pytest.raises(ValueError, match="not a number"):
        comp()
