#!/usr/bin/env python3
"""Write backend/app/products.json: the storefront's own display catalogue for P0001..P0200.

Names, brands, categories and sizes come from the inventory API's generator (seed 42, so they match
what the API returns for releases 1.1.0/1.2.0). Price, colour and description are derived from a hash of
the product id, never from the generator's rng, so the API catalogue hash is untouched.

Usage: python scripts/export_products.py
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATE = HERE.parent.parent / "inventory-api" / "catalogue" / "generate.py"
OUT = HERE.parent / "backend" / "app" / "products.json"
SEED, N = 42, 200

PRICE_RANGE = {"footwear": (89, 219), "apparel": (39, 189), "accessories": (15, 79)}
PALETTE = [
    ("Forest green", "#2f6b4f"), ("Burnt orange", "#d2662d"), ("Navy", "#1f3a63"), ("Slate", "#5a6678"),
    ("Sand", "#c8b38a"), ("Ember red", "#b73a2e"), ("Glacier blue", "#4f93b8"), ("Charcoal", "#33373d"),
]
USE = {
    "footwear": ["long ridge runs", "wet gravel descents", "technical scrambles", "early morning trail laps"],
    "apparel": ["changeable mountain weather", "cold starts above the treeline", "fast hikes in the wind", "long days out"],
    "accessories": ["day hikes", "alpine mornings", "long trail runs", "packing light"],
}
FEEL = ["Light and quick", "Durable and dependable", "Warm without bulk", "Balanced and responsive", "Compact and packable"]


def h(product_id: str, salt: str) -> int:
    return int(hashlib.sha256(f"{salt}:{product_id}".encode()).hexdigest()[:12], 16)


def load_generator():
    spec = importlib.util.spec_from_file_location("catalogue_generate", GENERATE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"export_products.py: cannot load {GENERATE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def display(p: dict) -> dict:
    pid, cat = p["product_id"], p["category"]
    lo, hi = PRICE_RANGE[cat]
    euros = lo + h(pid, "price") % (hi - lo + 1)
    colour, hex_ = PALETTE[h(pid, "colour") % len(PALETTE)]
    feel = FEEL[h(pid, "feel") % len(FEEL)]
    use = USE[cat][h(pid, "use") % len(USE[cat])]
    return {
        "product_id": pid, "name": p["name"], "brand": p["brand"], "category": cat, "size": p["size"],
        "price_eur": euros + 0.90,
        "colour": {"name": colour, "hex": hex_},
        "description": f"{feel}, built for {use}.",
    }


def main():
    catalogue = load_generator().build(seed=SEED, n=N)
    items = [display(p) for p in catalogue["products"] if int(p["product_id"][1:]) <= N]
    if len(items) != N:
        raise SystemExit(f"export_products.py: expected {N} products, got {len(items)}")
    OUT.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(items)} products)", file=sys.stderr)


if __name__ == "__main__":
    main()
