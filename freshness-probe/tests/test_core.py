import fakeredis
import pytest

from freshness_probe.config import ConfigError, load_config
from freshness_probe.core import FreshnessProbe, check_connect

NS = "n1"
STORES = ("S01", "S02")


def pkey(store):
    return f"stock:{NS}:{store}:__probe__"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Metrics:
    def __init__(self):
        self.gauges, self.counts = {}, {}

    def gauge(self, name, value, tags=None):
        self.gauges[(name, tuple(tags or ()))] = value

    def increment(self, name, tags=None):
        k = (name, tuple(tags or ()))
        self.counts[k] = self.counts.get(k, 0) + 1

    def g(self, name, store=None):
        return self.gauges[(name, (f"store:{store}",) if store else ())]


class FakeWriter:
    def __init__(self, clock):
        self.clock, self.rev, self.fail = clock, 100, False

    def write(self, quantity):
        if self.fail:
            raise ConnectionError("pg down")
        self.rev += 1
        return self.rev, int(self.clock() * 1000)


@pytest.fixture
def env():
    clock, metrics = Clock(), Metrics()
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("stock:active_ns", NS)
    writers = {s: FakeWriter(clock) for s in STORES}
    wd = FreshnessProbe(writers=writers, redis=r, metrics=metrics, clock=clock, stale_after_s=15)
    return wd, writers, r, clock, metrics


def consume(r, store, rev):  # the projector applying the store's probe
    r.hset(pkey(store), mapping={"revision": rev, "quantity": 1})


def consume_all(r, writers):
    for s, w in writers.items():
        consume(r, s, w.rev)


def test_caught_up_is_ok_age_zero(env):
    wd, writers, r, clock, m = env
    wd.probe_write()
    consume_all(r, writers)
    clock.t += 1
    res = wd.check()
    assert {s: (x.state, x.probe_age_ms) for s, x in res.items()} == {"S01": ("ok", 0), "S02": ("ok", 0)}
    assert r.hgetall("feed:status:S01")["state"] == "ok"
    agg = r.hgetall("feed:status")
    assert agg["state"] == "ok" and agg["probe_age_ms"] == "0"
    assert m.g("stock.feed.state", "S02") == 1 and m.g("stock.probe.age", "S01") == 0


def test_one_store_stopped_goes_stale_and_aggregate_is_worst(env):
    wd, writers, r, clock, m = env
    wd.probe_write()
    consume_all(r, writers)
    states = []
    for _ in range(5):  # S01's consumer path is stopped, S02 keeps up
        clock.t += 5
        wd.probe_write()
        consume(r, "S02", writers["S02"].rev)
        states.append(wd.check())
    assert [x["S01"].probe_age_ms for x in states] == [0, 5000, 10000, 15000, 20000]
    assert states[3]["S01"].state == "ok"  # exactly at the limit
    assert states[-1]["S01"].state == "stale" and states[-1]["S02"].state == "ok"
    assert r.hgetall("feed:status:S01")["state"] == "stale" and r.hgetall("feed:status:S02")["state"] == "ok"
    agg = r.hgetall("feed:status")
    assert agg["state"] == "stale" and agg["probe_age_ms"] == "20000"
    assert m.g("stock.feed.state", "S01") == 0 and m.g("stock.probe.age", "S01") == 20
    consume_all(r, writers)
    assert wd.check()["S01"].state == "ok" and r.hgetall("feed:status")["state"] == "ok"


def test_age_uses_oldest_pending_not_newest(env):
    wd, writers, r, clock, _ = env
    wd.probe_write()
    first = writers["S01"].rev
    clock.t += 10
    wd.probe_write()
    consume(r, "S01", first)
    res = wd.check()["S01"]
    assert res.probe_age_ms == 0
    assert wd.probes["S01"].pending == [(writers["S01"].rev, int(clock.t * 1000))]
    clock.t += 3
    assert wd.check()["S01"].probe_age_ms == 3000


def test_one_db_failing_does_not_stop_the_others(env):
    wd, writers, r, clock, m = env
    writers["S01"].fail = True
    wd.probe_write()
    assert wd.check()["S01"].state == "unknown"  # never wrote
    assert writers["S02"].rev == 101  # S02 was still probed
    consume(r, "S02", writers["S02"].rev)
    res = wd.check()
    assert res["S02"].state == "ok" and res["S01"].state == "unknown"
    assert r.hgetall("feed:status")["state"] == "unknown"  # worst
    writers["S01"].fail = False
    wd.probe_write()
    consume_all(r, writers)
    assert wd.check()["S01"].state == "ok"
    writers["S01"].fail = True
    clock.t += 5
    wd.probe_write()
    assert wd.check()["S01"].state == "unknown" and r.hgetall("feed:status:S01")["state"] == "unknown"
    assert m.counts[("stock.probe.write_errors", ("store:S01",))] == 2
    assert ("stock.probe.write_errors", ("store:S02",)) not in m.counts
    assert m.g("stock.feed.state", "S01") == -1


def test_redis_down_is_unknown(env):
    wd, _, r, clock, m = env
    wd.probe_write()

    class Down:
        def get(self, *_):
            raise ConnectionError("redis down")

        hget = hset = get

    wd.redis = Down()
    res = wd.check()
    assert all(x.state == "unknown" for x in res.values()) and m.g("stock.feed.state", "S01") == -1


def test_missing_active_ns_is_unknown(env):
    wd, _, r, _, _ = env
    wd.probe_write()
    r.delete("stock:active_ns")
    assert wd.check()["S01"].state == "unknown"


def test_probe_never_visible_goes_stale(env):
    wd, _, _, clock, _ = env
    wd.probe_write()
    clock.t += 16
    assert wd.check()["S01"].state == "stale"


def test_sellable_age(env):
    wd, writers, r, clock, m = env
    assert wd.check() and ("stock.sellable.age", ()) not in m.gauges  # nothing written yet
    wd.probe_write()
    newest = int(clock.t * 1000)
    clock.t += 4
    wd.check()
    assert m.g("stock.sellable.age") == 4.0  # sellable:__probe__ missing: behind
    r.hset("sellable:__probe__", mapping={"sellable": 5, "last_changed_at_ms": newest - 1})
    wd.check()
    assert m.g("stock.sellable.age") == 4.0
    r.hset("sellable:__probe__", mapping={"last_changed_at_ms": newest})
    wd.check()
    assert m.g("stock.sellable.age") == 0.0


class Http:
    def __init__(self, bodies=None, exc=None):
        self.bodies, self.exc, self.urls = bodies or {}, exc, []

    def __call__(self, url):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return self.bodies[url.split("/")[-2]]


UP = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]}
NAMES = ["inventory-s01", "inventory-s02", "sellable-redis"]


def run(http):
    m = Metrics()
    return check_connect(http, "http://c:8083", NAMES, m), m


def test_connect_all_running_tagged_per_connector():
    out, m = run(Http({n: UP for n in NAMES}))
    assert all(out.values())
    assert [m.gauges[("stock.connect.task_running", (f"connector:{n}",))] for n in NAMES] == [1, 1, 1]


def test_connect_one_task_failed_only_that_connector_is_zero():
    bad = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}, {"state": "FAILED"}]}
    out, m = run(Http({"inventory-s01": UP, "inventory-s02": bad, "sellable-redis": UP}))
    assert out == {"inventory-s01": True, "inventory-s02": False, "sellable-redis": True}
    assert m.gauges[("stock.connect.task_running", ("connector:inventory-s02",))] == 0


def test_connect_no_tasks_unreachable_malformed():
    assert not any(run(Http({n: {"connector": {"state": "RUNNING"}, "tasks": []} for n in NAMES}))[0].values())
    out, m = run(Http(exc=ConnectionError("down")))
    assert not any(out.values()) and len(m.gauges) == 3
    assert not any(run(Http({n: {"weird": 1} for n in NAMES}))[0].values())


ENV = {"STORE_HOSTS": "S01=store-s01,S02=store-s02", "PG_DATABASE": "inventory", "PG_WRITER_USER": "w",
       "PG_WRITER_PASSWORD": "p", "REDIS_URL": "redis://r", "CONNECT_URL": "http://c:8083/"}


def test_config_fails_loudly():
    with pytest.raises(ConfigError) as e:
        load_config({"PROBE_INTERVAL_S": "abc"})
    assert "STORE_HOSTS" in str(e.value) and "PROBE_INTERVAL_S" in str(e.value)


def test_config_stores_and_connectors():
    cfg = load_config(ENV)
    assert cfg.stores == (("S01", "store-s01"), ("S02", "store-s02"))
    assert cfg.connectors == ("inventory-s01", "inventory-s02", "sellable-redis")


@pytest.mark.parametrize("raw", ["S01", "S01=", "S01=a,S01=b", "S01=a,", "S01=a, S02=b"])
def test_config_store_hosts_strict(raw):
    with pytest.raises(ConfigError, match="STORE_HOSTS"):
        load_config({**ENV, "STORE_HOSTS": raw})
