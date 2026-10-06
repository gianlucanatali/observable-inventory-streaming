#!/usr/bin/env python3
"""Write backend/app/products.json: the storefront's own display catalogue for P0001..P0200.

A product id is a SKU: one model in one colour and one size. Stock, events and the availability API stay per SKU.
The storefront groups SKUs into models (MODELS below) so the shop can show colour and size variants.

The inventory API's generator (inventory-api/catalogue/generate.py) is only read, never changed, so its
catalogue hash and the 1.1.0 regression timing are untouched. The display catalogue is independent of it,
except that the demo SKU P0042 must match what the API returns for it (checked below).

PINNED SKUs keep their exact offer-relevant facts (name, brand, category, size, colour, price). They are the
sell-out SKU and the EU 42 footwear the offer policy may propose for it, so the offer candidates for P0042 are
the same as before the variants existed (checked by check_offer_pool).

Usage: python scripts/export_products.py   (from overlay/storefront or overlay/)
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATE = HERE.parent.parent / "inventory-api" / "catalogue" / "generate.py"
OUT = HERE.parent / "backend" / "app" / "products.json"
IMAGES = HERE.parent / "backend" / "app" / "product-images"
SEED, N = 42, 200

HEX = {
    "Forest green": "#2f6b4f", "Burnt orange": "#d2662d", "Navy": "#1f3a63", "Slate": "#5a6678",
    "Sand": "#c8b38a", "Ember red": "#b73a2e", "Glacier blue": "#4f93b8", "Charcoal": "#33373d",
}
USE = {
    "footwear": ["long ridge runs", "wet gravel descents", "technical scrambles", "early morning trail laps"],
    "apparel": ["changeable mountain weather", "cold starts above the treeline", "fast hikes in the wind", "long days out"],
    "accessories": ["day hikes", "alpine mornings", "long trail runs", "packing light"],
}
FEEL = ["Light and quick", "Durable and dependable", "Warm without bulk", "Balanced and responsive", "Compact and packable"]

APPAREL_SIZES = ["XS", "S", "M", "L", "XL"]
ACCESSORY_SIZES = ["S", "M", "L"]
ONE_SIZE = "One size"


def eu(lo: int, hi: int) -> list[str]:
    return [f"EU {n}" for n in range(lo, hi + 1)]


def run(sizes: list[str], first: str, last: str) -> list[str]:
    return sizes[sizes.index(first):sizes.index(last) + 1]


# (brand, name, category, price_eur, [(colour, sizes)]). The name is "<product type> <suffix>"; the type plus
# the colour selects the photo in backend/app/product-images. Colour order is the order shown on the page.
MODELS = [
    # Footwear. Outside the pinned models, a model that has EU 42 is priced outside the offer band of P0042
    # (219.90 +/- 20%), and the pinned models have EU 42 only in their pinned colour (see check_offer_pool).
    ("Alpenpace", "Trailrunner GTX", "footwear", 219.90,
     [("Forest green", eu(39, 45)), ("Slate", eu(37, 41)), ("Burnt orange", eu(43, 46))]),
    ("Alpenpace", "Brenta Storm", "footwear", 208.90,
     [("Burnt orange", eu(40, 44)), ("Navy", eu(37, 41))]),
    ("Alpenpace", "Pathfinder Air", "footwear", 204.90,
     [("Ember red", eu(40, 45)), ("Glacier blue", eu(36, 40))]),
    ("Pietra Grigia", "Sentinel Storm", "footwear", 180.90,
     [("Forest green", eu(40, 45)), ("Slate", eu(43, 46))]),
    ("Ridgeline", "Dolomia Evo", "footwear", 164.90,
     [("Charcoal", eu(39, 46)), ("Sand", eu(37, 41)), ("Glacier blue", eu(41, 43))]),
    ("Montebruno", "Cresta Alpine", "footwear", 289.90,
     [("Ember red", eu(40, 46)), ("Burnt orange", eu(38, 43))]),
    ("Nordvento", "Rifugio Pro", "footwear", 149.90,
     [("Slate", eu(38, 45)), ("Forest green", eu(39, 44))]),
    ("Corvina", "Pathfinder Lite", "footwear", 119.90,
     [("Navy", eu(39, 44)), ("Charcoal", eu(36, 41))]),
    # Apparel
    ("Lagoverde", "Rain Jacket Storm", "apparel", 169.90,
     [("Navy", APPAREL_SIZES), ("Ember red", run(APPAREL_SIZES, "S", "XL")), ("Glacier blue", run(APPAREL_SIZES, "XS", "L"))]),
    ("Corvina", "Rain Jacket Lite", "apparel", 119.90,
     [("Forest green", run(APPAREL_SIZES, "S", "XL")), ("Slate", run(APPAREL_SIZES, "XS", "L")),
      ("Charcoal", run(APPAREL_SIZES, "S", "L"))]),
    ("Faggeto", "Softshell Pro", "apparel", 139.90,
     [("Slate", run(APPAREL_SIZES, "S", "XL")), ("Forest green", run(APPAREL_SIZES, "XS", "L"))]),
    ("Tramonto", "Windshell Lite", "apparel", 89.90,
     [("Charcoal", APPAREL_SIZES), ("Ember red", run(APPAREL_SIZES, "S", "L"))]),
    ("Vetta", "Fleece Midlayer Evo", "apparel", 79.90,
     [("Navy", APPAREL_SIZES), ("Burnt orange", run(APPAREL_SIZES, "S", "XL")), ("Sand", run(APPAREL_SIZES, "S", "L"))]),
    ("Sentiero", "Base Layer Zero", "apparel", 59.90,
     [("Charcoal", APPAREL_SIZES), ("Glacier blue", run(APPAREL_SIZES, "S", "L")), ("Forest green", run(APPAREL_SIZES, "S", "L"))]),
    ("Aquila Nera", "Trek Shorts Air", "apparel", 54.90,
     [("Sand", APPAREL_SIZES), ("Slate", run(APPAREL_SIZES, "S", "L"))]),
    # Accessories
    ("Sentiero", "Hydration Vest Max", "accessories", 74.90,
     [("Slate", ACCESSORY_SIZES), ("Ember red", ACCESSORY_SIZES), ("Navy", run(ACCESSORY_SIZES, "S", "M"))]),
    ("Montebruno", "Gaiters Pro", "accessories", 39.90,
     [("Charcoal", ACCESSORY_SIZES), ("Glacier blue", ACCESSORY_SIZES)]),
    ("Ridgeline", "Trekking Poles Alpine", "accessories", 69.90,
     [(c, [ONE_SIZE]) for c in ("Charcoal", "Glacier blue", "Burnt orange", "Forest green")]),
    ("Pietra Grigia", "Trekking Poles Lite", "accessories", 49.90,
     [(c, [ONE_SIZE]) for c in ("Sand", "Navy")]),
    ("Faggeto", "Merino Beanie Plus", "accessories", 29.90,
     [(c, [ONE_SIZE]) for c in ("Burnt orange", "Charcoal", "Slate")]),
    ("Vetta", "Trail Cap Air", "accessories", 24.90,
     [(c, [ONE_SIZE]) for c in ("Navy", "Sand", "Ember red", "Charcoal")]),
]

# SKU id -> (model name, colour, size). Exact facts must not change: see the module docstring.
PINNED = {
    "P0042": ("Trailrunner GTX", "Forest green", "EU 42"),   # the sell-out SKU
    "P0160": ("Brenta Storm", "Burnt orange", "EU 42"),      # offer candidates for P0042, closest price first
    "P0061": ("Pathfinder Air", "Ember red", "EU 42"),
    "P0146": ("Sentinel Storm", "Forest green", "EU 42"),
}
# Offer pool of P0042 before the variants existed (same category and size, price within +/- 20%, closest first).
P0042_OFFER_POOL = ["P0160", "P0061", "P0146"]
DESCRIPTION = {"Trailrunner GTX": "Balanced and responsive, built for early morning trail laps."}


def h(key: str, salt: str) -> int:
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:12], 16)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def describe(name: str, category: str) -> str:
    if name in DESCRIPTION:
        return DESCRIPTION[name]
    feel = FEEL[h(name, "feel") % len(FEEL)]
    use = USE[category][h(name, "use") % len(USE[category])]
    return f"{feel}, built for {use}."


def build() -> list[dict]:
    """All SKUs, sorted by product id. Pinned SKUs keep their id; the others take the free ids in model order."""
    slots = []  # (model, colour, size, variant_rank) in model, colour, size order
    for brand, name, category, price, colourways in MODELS:
        rank = 0
        for colour, sizes in colourways:
            for size in sizes:
                slots.append(((brand, name, category, price), colour, size, rank))
                rank += 1
    if len(slots) != N:
        raise SystemExit(f"export_products.py: MODELS define {len(slots)} SKUs, expected exactly {N}")
    by_key = {(m[1], c, s): (m, c, s, r) for m, c, s, r in slots}
    if len(by_key) != N:
        raise SystemExit("export_products.py: a (model, colour, size) appears twice in MODELS")
    pinned_keys = set(PINNED.values())
    missing = pinned_keys - set(by_key)
    if missing:
        raise SystemExit(f"export_products.py: pinned SKUs not in MODELS: {sorted(missing)}")
    free_ids = [f"P{i:04d}" for i in range(1, N + 1) if f"P{i:04d}" not in PINNED]
    assignment = dict(PINNED)
    free_slots = [(m[1], c, s) for m, c, s, _ in slots if (m[1], c, s) not in pinned_keys]
    assignment.update(zip(free_ids, free_slots))

    items = []
    for pid in sorted(assignment):
        (brand, name, category, price), colour, size, rank = by_key[assignment[pid]]
        items.append({
            "product_id": pid, "name": name, "brand": brand, "category": category, "size": size,
            "price_eur": price,
            "colour": {"name": colour, "hex": HEX[colour]},
            "description": describe(name, category),
            "model_id": slug(f"{brand} {name}"),
            "variant_rank": rank,
        })
    return items


def offer_pool(original: dict, items: list[dict]) -> list[str]:
    """Mirror of offer-worker policy.build_candidates eligibility and order (before the stock check)."""
    lo, hi = original["price_eur"] * 0.8, original["price_eur"] * 1.2
    pool = [p for p in items if p["product_id"] != original["product_id"] and p["category"] == original["category"]
            and p["size"] == original["size"] and lo <= p["price_eur"] <= hi]
    pool.sort(key=lambda p: (abs(p["price_eur"] - original["price_eur"]), p["product_id"]))
    return [p["product_id"] for p in pool]


def check(items: list[dict], generated: list[dict]) -> None:
    by_id = {p["product_id"]: p for p in items}
    api42 = next(p for p in generated if p["product_id"] == "P0042")
    mine = by_id["P0042"]
    if (mine["name"], mine["brand"], mine["category"], mine["size"]) != (api42["name"], api42["brand"], api42["category"], api42["size"]):
        raise SystemExit(f"export_products.py: P0042 differs from the inventory API catalogue: {mine} vs {api42}")
    pool = offer_pool(by_id["P0042"], items)
    if pool != P0042_OFFER_POOL:
        raise SystemExit(f"export_products.py: offer pool of P0042 changed to {pool}, expected {P0042_OFFER_POOL}")
    for p in items:
        photo = IMAGES / f"{slug(' '.join(p['name'].split()[:-1]))}--{slug(p['colour']['name'])}.jpg"
        if not photo.is_file():
            raise SystemExit(f"export_products.py: {p['product_id']} has no photo {photo.name}")


def load_generator():
    spec = importlib.util.spec_from_file_location("catalogue_generate", GENERATE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"export_products.py: cannot load {GENERATE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    items = build()
    check(items, load_generator().build(seed=SEED, n=N)["products"])
    OUT.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(items)} SKUs, {len(MODELS)} models)", file=sys.stderr)


if __name__ == "__main__":
    main()
