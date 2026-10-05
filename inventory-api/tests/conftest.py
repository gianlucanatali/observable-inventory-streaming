import os
import subprocess
import sys
import time

import fakeredis
import pytest

from app.catalogue import Catalogue
from app.config import Config
from app.main import create_app

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORES = ["S01", "S02", "S03", "S04", "S05"]
STORE_HOSTS = ",".join(f"{s}=store-{s.lower()}" for s in STORES)


class FakeStatsd:
    def __init__(self):
        self.calls = []

    def increment(self, name, value=1, tags=None):
        self.calls.append((name, tuple(tags or ())))


@pytest.fixture(scope="session")
def catalogue_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("cat") / "catalogue.json"
    subprocess.run([sys.executable, os.path.join(ROOT, "catalogue", "generate.py"), "--seed", "42",
                    "--products", "400", "--out", str(path)], check=True, stderr=subprocess.DEVNULL)
    return str(path)


@pytest.fixture
def redis_client():
    r = fakeredis.FakeRedis(decode_responses=True)
    now = int(time.time() * 1000)
    r.set("stock:active_ns", "n1")
    r.hset("stock:n1:meta", mapping={"ready": "1", "snapshot_done": "1"})
    for sid in STORES:
        r.hset(f"feed:status:{sid}", mapping={"state": "ok", "probe_age_ms": 0, "checked_at_ms": now})
    pos = lambda q, rev, d=0: {"quantity": q, "revision": rev, "deleted": d,
                               "changed_at_ms": 1760000000123, "applied_at_ms": 1760000000200}
    # P0042: 2+1+3+1+2 = 9
    for sid, q, rev in zip(STORES, (2, 1, 3, 1, 2), (810, 811, 812, 813, 814)):
        r.hset(f"stock:n1:{sid}:P0042", mapping=pos(q, rev))
    r.hset("sellable:P0042", mapping={"product_id": "P0042", "sellable": 9, "stores_reporting": 5,
                                      "last_changed_at_ms": 1760000000123})
    # P0001: only S01 holds it (12); S02 delisted it (deleted=1); S03..S05 have no row at all
    r.hset("stock:n1:S01:P0001", mapping=pos(12, 7))
    r.hset("stock:n1:S02:P0001", mapping=pos(5, 9, 1))
    r.hset("sellable:P0001", mapping={"product_id": "P0001", "sellable": 12, "stores_reporting": 1,
                                      "last_changed_at_ms": 1760000000123})
    # P0002: every store holds it with 0
    for sid in STORES:
        r.hset(f"stock:n1:{sid}:P0002", mapping=pos(0, 8))
    r.hset("sellable:P0002", mapping={"product_id": "P0002", "sellable": 0, "stores_reporting": 5,
                                      "last_changed_at_ms": 1760000000999})
    return r


def make_app(mode, version, redis_client, catalogue_file, started=True):
    env = {"REDIS_URL": "redis://unused", "DD_VERSION": version, "CATALOGUE_MODE": mode,
           "CATALOGUE_PATH": catalogue_file, "STORE_HOSTS": STORE_HOSTS}
    sd = FakeStatsd()
    app = create_app(Config(env), redis_client=redis_client, statsd=sd)
    if started:
        app.extensions["prepare"]()
    app.testing = True
    app.extensions["fake_statsd"] = sd
    return app


@pytest.fixture
def factory(redis_client, catalogue_file):
    def f(mode="per_request", version="1.1.0", started=True, client=None):
        return make_app(mode, version, client or redis_client, catalogue_file, started)
    return f
