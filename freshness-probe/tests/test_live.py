import logging

import fakeredis

from freshness_probe.core import FreshnessProbe, run_loop
from freshness_probe.live import LiveConfig
from test_core import Clock, FakeWriter, Metrics, NS, consume_all


def make(r, stale=15):
    clock = Clock()
    writers = {"S01": FakeWriter(clock)}
    wd = FreshnessProbe(writers=writers, redis=r, metrics=Metrics(), clock=clock, stale_after_s=stale, live=LiveConfig(r))
    return wd, writers, clock


def test_live_value_and_fallback_logged_once(caplog):
    r = fakeredis.FakeRedis(decode_responses=True)
    lc = LiveConfig(r)
    with caplog.at_level(logging.WARNING, logger="freshness_probe"):
        assert lc.get("stale_after_s", 15) == 15
        assert lc.get("stale_after_s", 15) == 15
    assert len([x for x in caplog.records if "live config" in x.message]) == 1
    r.hset("demo:config", "stale_after_s", "3")
    assert lc.get("stale_after_s", 15) == 3.0


def test_invalid_value_falls_back():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.hset("demo:config", mapping={"a": "x", "b": "-1"})
    lc = LiveConfig(r)
    assert lc.get("a", 7) == 7 and lc.get("b", 8) == 8


def test_stale_after_is_read_every_check():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    wd, writers, clock = make(r)
    wd.probe_write()
    clock.t += 5
    assert wd.check()["S01"].state == "ok"
    r.hset("demo:config", "stale_after_s", "2")  # live change, no restart
    assert wd.check()["S01"].state == "stale"
    r.hset("demo:config", "stale_after_s", "60")
    assert wd.check()["S01"].state == "ok"


def test_probe_interval_is_read_every_cycle():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    wd, writers, clock = make(r)
    r.hset("demo:config", "probe_interval_s", "2")
    writes = []
    orig = wd.probe_write
    wd.probe_write = lambda: (writes.append(clock.t), orig())
    ticks = {"n": 0}

    def sleep(s):
        clock.t += s
        ticks["n"] += 1

    run_loop(wd, lambda: None, 5, 1, 1000, sleep=sleep, stop=lambda: ticks["n"] >= 6)
    assert writes == [1_000_000.0, 1_000_002.0, 1_000_004.0]
