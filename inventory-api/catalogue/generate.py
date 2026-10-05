#!/usr/bin/env python3
"""Deterministic fictional retail catalogue generator.

P0001..P0200 are the stocked products (P0042 is the demo's last pair). Products
P0201..PN are padding with rich nested attributes so that parsing and indexing
cost is real. Same seed and N always give byte-identical output.

Usage: python catalogue/generate.py --seed 42 --products N --out catalogue/catalogue.json
"""
import argparse
import hashlib
import json
import random
import sys

STOCKED = 200

BRANDS = ["Alpenpace", "Ridgeline", "Corvina", "Nordvento", "Tramonto", "Pietra Grigia",
          "Lagoverde", "Montebruno", "Aquila Nera", "Sentiero", "Faggeto", "Vetta"]
CATEGORIES = {
    "footwear": ["Trailrunner", "Pathfinder", "Cresta", "Sentinel", "Brenta", "Dolomia", "Rifugio"],
    "apparel": ["Windshell", "Fleece Midlayer", "Softshell", "Base Layer", "Rain Jacket", "Trek Shorts"],
    "accessories": ["Trail Cap", "Merino Beanie", "Gaiters", "Hydration Vest", "Trekking Poles"],
}
SUFFIX = ["GTX", "Pro", "Lite", "Evo", "Alpine", "Storm", "Air", "Max", "Zero", "Plus"]
COLOURS = ["black", "slate", "forest green", "burnt orange", "navy", "sand", "ember red", "glacier blue"]
MATERIALS = ["recycled polyester", "merino wool", "ripstop nylon", "gore-tex membrane", "vibram rubber",
             "organic cotton", "eva foam", "dyneema mesh"]
TAGS = ["waterproof", "breathable", "lightweight", "winter", "summer", "vegan", "recycled", "reflective",
        "packable", "trail", "hiking", "running", "unisex", "new", "bestseller", "outlet"]
WORDS = ("engineered for long days on rough terrain with a balanced fit durable seams and a "
         "responsive feel underfoot designed in the north of Italy tested on ridges and gravel "
         "roads breathable lining quick drying panels reinforced toe cap adjustable hood stretch "
         "waistband secure pocket compact pack size low weight dependable grip").split()
SHOE_SIZES = [f"EU {n}" for n in range(36, 48)]
APPAREL_SIZES = ["XS", "S", "M", "L", "XL", "XXL"]


def sentence(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def product(rng, idx, rich):
    pid = f"P{idx:04d}"
    cat = rng.choice(list(CATEGORIES))
    name = f"{rng.choice(CATEGORIES[cat])} {rng.choice(SUFFIX)}"
    brand = rng.choice(BRANDS)
    sizes = SHOE_SIZES if cat == "footwear" else APPAREL_SIZES
    p = {
        "product_id": pid,
        "name": name,
        "brand": brand,
        "category": cat,
        "size": rng.choice(sizes),
        "image_url": f"/img/{pid}.svg",
    }
    if rich:
        n_var = rng.randint(4, 8)
        p["description"] = " ".join(sentence(rng, rng.randint(8, 16)) for _ in range(rng.randint(4, 8)))
        p["materials"] = rng.sample(MATERIALS, rng.randint(2, 4))
        p["tags"] = rng.sample(TAGS, rng.randint(4, 8))
        p["variants"] = [
            {
                "sku": f"{pid}-{v:02d}",
                "colour": rng.choice(COLOURS),
                "size": rng.choice(sizes),
                "price_eur": round(rng.uniform(19, 249), 2),
                "weight_g": rng.randint(80, 1400),
                "attributes": {"fit": rng.choice(["regular", "slim", "wide"]),
                               "care": sentence(rng, 6),
                               "season": rng.choice(["SS", "FW", "all"])},
            }
            for v in range(n_var)
        ]
        p["related"] = [f"P{rng.randint(1, idx):04d}" for _ in range(rng.randint(3, 6))]
        p["reviews"] = [{"rating": rng.randint(1, 5), "text": sentence(rng, rng.randint(6, 14))}
                        for _ in range(rng.randint(2, 5))]
    return p


def build(seed, n):
    if n < STOCKED:
        raise SystemExit(f"generate.py: --products must be >= {STOCKED}, got {n}")
    rng = random.Random(seed)
    products = []
    for i in range(1, n + 1):
        p = product(rng, i, rich=i > STOCKED)
        if i == 42:
            p.update(name="Trailrunner GTX", brand="Alpenpace", category="footwear", size="EU 42")
        products.append(p)
    return {"schema": 1, "seed": seed, "products": products}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--products", type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    data = json.dumps(build(a.seed, a.products), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw = data.encode("ascii")
    with open(a.out, "wb") as f:
        f.write(raw)
    print(f"{a.out} products={a.products} bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
