"""RedisBackend.seed_defaults: fills only absent keys, prefers the other store's value, skips the kill switch."""
from demo_control.backends import RedisBackend
from demo_control.registry import Param


class FakeRedis:
    def __init__(self, h=None):
        self.h = dict(h or {})

    def hsetnx(self, name, key, value):
        if key in self.h:
            return 0
        self.h[key] = value
        return 1


def _p(key, stores, default):
    return Param(key=key, label=key, unit="", default=default, min=0, max=10_000, store=",".join(stores),
                 consumers=(), apply="", layer="core", doc="")


def test_seed_fills_absent_prefers_current_and_skips_kill_switch():
    r = FakeRedis({"poll_ms": "500"})
    params = [_p("time_compression", ["kafka_config", "redis"], 60), _p("poll_ms", ["redis"], 1000),
              _p("demand_skew", ["redis"], 1), _p("offers_kill_switch", ["redis"], 0), _p("lead_time_s", ["procurement_db"], 1)]
    seeded = RedisBackend(r).seed_defaults(params, {"time_compression": 30.0})
    assert seeded == ["time_compression", "demand_skew"]
    assert r.h == {"poll_ms": "500", "time_compression": "30", "demand_skew": "1"}
