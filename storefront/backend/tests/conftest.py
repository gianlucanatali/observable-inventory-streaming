from datetime import datetime, timezone

import pytest

from app import Deps, create_app
from app.config import Config
from app.offers import OfferStore


class FakePublisher:
    def __init__(self):
        self.sent = []
        self.fail = None

    def publish(self, key, value):
        if self.fail:
            raise self.fail
        self.sent.append((key, value))


class FakeRedis:
    def __init__(self, scenario="sc-1"):
        self.data = {"scenario:current": scenario} if scenario else {}
        self.ttl = {}
        self.fail = None

    def get(self, key):
        if self.fail:
            raise self.fail
        return self.data.get(key)

    def hget(self, key, field):
        if self.fail:
            raise self.fail
        return self.data.get(key, {}).get(field)

    def ping(self):
        if self.fail:
            raise self.fail
        return True

    # Hash commands used by the cart contents read model (redis-py semantics, decode_responses=True).
    def hincrby(self, key, field, amount=1):
        if self.fail:
            raise self.fail
        h = self.data.setdefault(key, {})
        h[field] = str(int(h.get(field, 0)) + amount)
        return int(h[field])

    def hdel(self, key, *fields):
        if self.fail:
            raise self.fail
        h = self.data.get(key, {})
        removed = sum(1 for f in fields if h.pop(f, None) is not None)
        if key in self.data and not h:
            del self.data[key]
            self.ttl.pop(key, None)
        return removed

    def expire(self, key, seconds):
        if self.fail:
            raise self.fail
        if key not in self.data:
            return False
        self.ttl[key] = seconds
        return True

    def hgetall(self, key):
        if self.fail:
            raise self.fail
        return dict(self.data.get(key, {}))

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self._redis, self._calls = redis, []

    def __getattr__(self, name):
        def queue(*args):
            self._calls.append((name, args))
            return self
        return queue

    def execute(self):
        return [getattr(self._redis, name)(*args) for name, args in self._calls]


class FakeStatsd:
    def __init__(self):
        self.calls = []

    def distribution(self, name, value, tags=None):
        self.calls.append(("distribution", name, value, tags))

    def increment(self, name, tags=None):
        self.calls.append(("increment", name, 1, tags))


ENV = {
    "KAFKA_BOOTSTRAP": "b:9092", "KAFKA_API_KEY": "k", "KAFKA_API_SECRET": "s",
    "SR_URL": "mock://x", "SR_API_KEY": "k", "SR_API_SECRET": "s", "REDIS_URL": "redis://x",
    "DD_ENV": "dd-demo", "DD_VERSION": "1.0.0", "DD_SERVICE": "storefront",
}


@pytest.fixture
def env():
    return dict(ENV)


@pytest.fixture
def make(tmp_path, env):
    def _make(offers_enabled=True, consumer=None, scenario="sc-1", now=None, **extra_env):
        (tmp_path / "index.html").write_text("<html>ui</html>")
        (tmp_path / "app.js").write_text("console.log(1)")
        e = {**env, "STATIC_DIR": str(tmp_path), "OFFERS_ENABLED": "true" if offers_enabled else "false", **extra_env}
        cfg = Config.from_env(e)
        deps = Deps(publisher=FakePublisher(), redis=FakeRedis(scenario), statsd=FakeStatsd(),
                    offer_consumer=consumer, offer_store=OfferStore(),
                    **({"clock": lambda: now} if now else {}))
        return create_app(cfg, deps).test_client(), deps
    return _make
