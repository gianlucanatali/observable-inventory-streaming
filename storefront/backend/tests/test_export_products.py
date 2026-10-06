"""The display catalogue groups SKUs into models; the checked-in products.json is exactly what the script writes."""
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "export_products.py"
PRODUCTS = Path(__file__).parents[1] / "app" / "products.json"


def load_script():
    spec = importlib.util.spec_from_file_location("export_products", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_grouping_is_deterministic_and_checked_in():
    mod = load_script()
    first, second = mod.build(), mod.build()
    assert first == second
    assert json.loads(PRODUCTS.read_text(encoding="utf-8")) == first
    assert [p["product_id"] for p in first] == [f"P{i:04d}" for i in range(1, 201)]


def test_models_have_colour_and_size_variants():
    items = load_script().build()
    models = defaultdict(list)
    for p in items:
        models[p["model_id"]].append(p)
    assert 20 <= len(models) <= 40
    for skus in models.values():
        assert len({(p["name"], p["brand"], p["category"], p["price_eur"], p["description"]) for p in skus}) == 1
        assert 2 <= len({p["colour"]["name"] for p in skus}) <= 4
        assert len({(p["colour"]["name"], p["size"]) for p in skus}) == len(skus)
        assert sorted(p["variant_rank"] for p in skus) == list(range(len(skus)))
        sizes = {p["size"] for p in skus}
        if skus[0]["category"] == "footwear":
            assert all(s.startswith("EU ") for s in sizes)
        elif skus[0]["category"] == "apparel":
            assert sizes <= {"XS", "S", "M", "L", "XL"}
        else:
            assert sizes <= {"One size"} or sizes <= {"S", "M", "L"}


def test_p0042_is_the_green_trailrunner_42_with_siblings():
    items = load_script().build()
    p42 = next(p for p in items if p["product_id"] == "P0042")
    assert (p42["brand"], p42["name"], p42["size"], p42["colour"]["name"], p42["price_eur"]) == \
        ("Alpenpace", "Trailrunner GTX", "EU 42", "Forest green", 219.9)
    siblings = [p for p in items if p["model_id"] == p42["model_id"] and p["product_id"] != "P0042"]
    assert {"EU 41", "EU 43"} <= {p["size"] for p in siblings if p["colour"]["name"] == "Forest green"}
    assert {p["colour"]["name"] for p in siblings} >= {"Slate", "Burnt orange"}


def test_offer_pool_for_the_sell_out_sku_is_unchanged():
    mod = load_script()
    items = mod.build()
    by_id = {p["product_id"]: p for p in items}
    assert mod.offer_pool(by_id["P0042"], items) == ["P0160", "P0061", "P0146"]
    # The facts the offer worker sends to Jev for these SKUs are the ones the demo was calibrated with.
    facts = {pid: (by_id[pid]["name"], by_id[pid]["brand"], by_id[pid]["size"], by_id[pid]["colour"]["name"], by_id[pid]["price_eur"])
             for pid in ("P0160", "P0061", "P0146")}
    assert facts == {
        "P0160": ("Brenta Storm", "Alpenpace", "EU 42", "Burnt orange", 208.9),
        "P0061": ("Pathfinder Air", "Alpenpace", "EU 42", "Ember red", 204.9),
        "P0146": ("Sentinel Storm", "Pietra Grigia", "EU 42", "Forest green", 180.9),
    }
