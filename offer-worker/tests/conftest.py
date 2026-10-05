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


class FakeMetrics:
    def __init__(self):
        self.calls = []

    def decision(self, route, reason): self.calls.append(("decision", route, reason))
    def text(self, route, reason): self.calls.append(("text", route, reason))
    def completed(self, offer_type): self.calls.append(("completed", offer_type))


def make_cfg(**over):
    env = {"KAFKA_BOOTSTRAP": "k:9092", "SR_URL": "http://sr", "REDIS_URL": "redis://r", "DD_AGENT_HOST": "a",
           "DD_ENV": "dd-demo", "DD_SERVICE": "offer-worker", "DD_VERSION": "1.0.0", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}
    env.update(over)
    return Config.from_env(env)


@pytest.fixture
def catalogue():
    return load_catalogue(str(PRODUCTS))


@pytest.fixture
def redis_client(catalogue):
    r = fakeredis.FakeRedis()
    r.set("scenario:current", "S1")
    for pid in catalogue:
        r.hset(f"sellable:{pid}", mapping={"sellable": 5})
    r.hset("sellable:P0042", mapping={"sellable": 0})
    return r


def risk(rid="S1|C1|P0042|1000", product="P0042", detected=NOW):
    return {"risk_id": rid, "scenario_id": "S1", "cart_id": "C1", "shopper_id": "sh1", "product_id": product,
            "cart_value_eur": 189.5, "returning_shopper": True, "item_count": 3,
            "sellable_changed_at_ms": 1000, "detected_at": int(detected * 1000)}
