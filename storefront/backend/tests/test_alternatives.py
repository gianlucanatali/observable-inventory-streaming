"""The shop's comparable pool must match the offer-worker's candidate rule (app/alternatives.py docstring)."""
import importlib.util
import random
import sys
from pathlib import Path

import pytest

from app.alternatives import MAX_ALTERNATIVES, SCAN_LIMIT, comparable_pool
from app.catalogue import load_products

POLICY = Path(__file__).resolve().parents[3] / "offer-worker" / "offer_worker" / "policy.py"


@pytest.fixture(scope="module")
def policy():
    if not POLICY.is_file():
        raise AssertionError(f"offer-worker policy not found at {POLICY}; this test runs from the repo checkout")
    spec = importlib.util.spec_from_file_location("offer_worker_policy_copy", POLICY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


def _shop_pick(original, products, stock):
    """What the browser does with the pool: the first MAX_ALTERNATIVES with stock > 0."""
    return [p["product_id"] for p in comparable_pool(original, products) if (stock(p["product_id"]) or 0) > 0][:MAX_ALTERNATIVES]


def _worker_pick(policy, original, catalogue, stock):
    return [c.product_id for c in policy.build_candidates(original, catalogue, stock, 10) if c.product_id]


def test_constants_match(policy):
    assert (MAX_ALTERNATIVES, SCAN_LIMIT) == (policy.MAX_ALTERNATIVES, policy.SCAN_LIMIT)


@pytest.mark.parametrize("product_id,expected", [("P0042", ["P0160", "P0061"]), ("P0048", ["P0059", "P0079"])])
def test_demo_products_match_worker(policy, product_id, expected):
    products = load_products()
    catalogue = {p["product_id"]: p for p in products}
    in_stock = lambda pid: 5  # noqa: E731
    assert _shop_pick(catalogue[product_id], products, in_stock) == expected
    assert _worker_pick(policy, catalogue[product_id], catalogue, in_stock) == expected


def test_whole_catalogue_matches_worker_with_random_stock(policy):
    products = load_products()
    catalogue = {p["product_id"]: p for p in products}
    rng = random.Random(7)
    stock = {pid: rng.choice([None, 0, 0, 3]) for pid in catalogue}
    for p in products:
        assert _shop_pick(p, products, stock.get) == _worker_pick(policy, p, catalogue, stock.get), p["product_id"]
