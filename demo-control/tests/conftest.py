import os

os.environ.setdefault("DD_TRACE_ENABLED", "false")

import base64
from contextlib import contextmanager
from pathlib import Path

import fakeredis
import pytest

from demo_control.app import create_app
from demo_control.backends import (KafkaConfigBackend, ProcurementBackend, ReadOnlyBackend, RedisBackend, StoreDbsBackend)
from demo_control.registry import load_registry
from demo_control.service import Control
from demo_control.actions import Actions

REGISTRY = str(Path(__file__).parents[2] / "contracts" / "demo-params.json")
PASSWORD = "s3cret"


class FakeStatsd:
    def __init__(self):
        self.events = []

    def event(self, title, message, alert_type=None, tags=None, **kw):
        self.events.append({"title": title, "message": message, "tags": tags})


class FakeProducer:
    def __init__(self, topic):
        self.topic, self.sent, self.fail = topic, [], None

    def send(self, key, value):
        if self.fail:
            raise self.fail
        self.sent.append((key, value))
        self.topic[key["key"]] = value["value"]


class FakeCursor:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        if self.db.fail:
            raise self.db.fail
        self.db.executed.append((sql, params))
        if sql.startswith("SELECT"):
            self.rows = [(k, v) for k, v in self.db.table.items()]
        elif sql.startswith("INSERT"):
            k, v = params
            self.db.table[k] = v

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, db):
        self.db = db

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return FakeCursor(self.db)

    @contextmanager
    def transaction(self):
        self.db.transactions += 1
        yield


class FakeDb:
    def __init__(self, text_values=False):
        self.table, self.executed, self.fail, self.transactions = {}, [], None, 0

    def connect(self, *_):
        if self.fail and isinstance(self.fail, ConnectionError):
            raise self.fail
        return FakeConn(self)


@pytest.fixture
def parts():
    registry = load_registry(REGISTRY)
    r = fakeredis.FakeRedis()
    topic = {}
    producer = FakeProducer(topic)
    proc, s01, s02 = FakeDb(), FakeDb(), FakeDb()
    stores = {"store-s01": s01, "store-s02": s02}
    backends = {
        "redis": RedisBackend(r),
        "kafka_config": KafkaConfigBackend(lambda: dict(topic), producer, clock=lambda: 1_800_000_000.0),
        "procurement_db": ProcurementBackend(proc.connect),
        "store_dbs": StoreDbsBackend((("S01", "store-s01"), ("S02", "store-s02")), lambda host: stores[host].connect()),
        "flink_statement": ReadOnlyBackend(),
    }
    statsd = FakeStatsd()
    class FakeActions:
        def __init__(self):
            self.calls, self.fail_sell = [], None
            self._actions = Actions(self.sell, self.reset)
        def sell(self, product, progress):
            self.calls.append(("sell-out", product))
            if self.fail_sell: raise self.fail_sell
            progress("sold S01")
        def reset(self, progress):
            self.calls.append(("reset", None)); progress("verified Redis")
        def start(self, *args): return self._actions.start(*args)
        def status(self): return self._actions.status()
    actions = FakeActions()
    control = Control(registry, backends, r, statsd, "dev", actions=actions)
    return dict(registry=registry, redis=r, topic=topic, producer=producer, proc=proc, s01=s01, s02=s02,
                backends=backends, statsd=statsd, control=control, actions=actions)


@pytest.fixture
def client(parts):
    return create_app(parts["control"], PASSWORD).test_client()


def auth(user="demo", pw=PASSWORD):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
