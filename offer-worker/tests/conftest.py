import os

os.environ.setdefault("DD_TRACE_ENABLED", "false")  # no agent in unit tests

import json
from pathlib import Path

import fakeredis
import pytest

from offer_worker.config import Config
from offer_worker.policy import load_catalogue

CONTRACT_AVRO = Path(__file__).parents[2] / "contracts" / "avro"
PRODUCTS = Path(__file__).parents[2] / "storefront" / "backend" / "app" / "products.json"
NOW = 1_800_000_000.0
STORES = ["S01", "S02", "S03", "S04", "S05"]
STORE_HOSTS = ",".join(f"{s}=store-{s.lower()}" for s in STORES)
STOCK_CASES = Path(__file__).parents[2] / "contracts" / "stock-trust-cases.json"


class FakeMetrics:
    def __init__(self):
        self.calls = []

    def decision(self, route, reason): self.calls.append(("decision", route, reason))
    def text(self, route, reason): self.calls.append(("text", route, reason))
    def completed(self, offer_type, restock_included): self.calls.append(("completed", offer_type, restock_included))
    def stock_unconfirmed(self, reason): self.calls.append(("stock_unconfirmed", reason))


def make_cfg(**over):
    env = {"KAFKA_BOOTSTRAP": "k:9092", "SR_URL": "http://sr", "REDIS_URL": "redis://r", "DD_AGENT_HOST": "a",
           "DD_ENV": "dd-demo", "DD_SERVICE": "offer-worker", "DD_VERSION": "1.0.0", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
           "STORE_HOSTS": STORE_HOSTS}
    env.update(over)
    return Config.from_env(env)


@pytest.fixture
def catalogue():
    return load_catalogue(str(PRODUCTS))


def set_position(r, store, pid, quantity, deleted=0, ns="n1"):
    r.hset(f"stock:{ns}:{store}:{pid}", mapping={"quantity": quantity, "revision": 1, "deleted": deleted,
                                                 "changed_at_ms": 1, "applied_at_ms": 1})


def set_feed(r, store, state="ok", age_ms=0):
    r.hset(f"feed:status:{store}", mapping={"state": state, "probe_age_ms": 0, "checked_at_ms": int(NOW * 1000) - age_ms})


@pytest.fixture
def redis_client(catalogue):
    """Every store live; every product 1 unit in each store (Flink total 5), P0042 sold out everywhere."""
    r = fakeredis.FakeRedis()
    r.set("scenario:current", "S1")
    r.set("stock:active_ns", "n1")
    r.hset("stock:n1:meta", mapping={"ready": "1", "snapshot_done": "1"})
    for s in STORES:
        set_feed(r, s)
    for pid in catalogue:
        q = 0 if pid == "P0042" else 1
        for s in STORES:
            set_position(r, s, pid, q)
        r.hset(f"sellable:{pid}", mapping={"product_id": pid, "sellable": q * len(STORES), "stores_reporting": len(STORES),
                                           "last_changed_at_ms": 1})
    return r


def risk(rid="S1|C1|P0042|1000", product="P0042", detected=NOW):
    return {"risk_id": rid, "scenario_id": "S1", "cart_id": "C1", "shopper_id": "sh1", "product_id": product,
            "cart_value_eur": 189.5, "returning_shopper": True, "item_count": 3,
            "sellable_changed_at_ms": 1000, "detected_at": int(detected * 1000)}
