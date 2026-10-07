"""Comparable products for a sold-out item, in the offer-worker's order.

Mirror of the pool in overlay/offer-worker/offer_worker/policy.py `build_candidates` (same category, same size,
price within +-20%, sorted by kind mismatch, waterproofing mismatch, price distance, product id; the first
SCAN_LIMIT examined). The worker then keeps the first MAX_ALTERNATIVES with confirmed stock > 0; the shop does the
same in the browser with the availability API (frontend/src/alternatives.js). The two services have separate
images, so this is a copy: tests/test_alternatives.py runs both on the real catalogue and fails when they differ.
Change both together.
"""
from __future__ import annotations

MAX_ALTERNATIVES = 2  # policy.MAX_ALTERNATIVES
SCAN_LIMIT = 12       # policy.SCAN_LIMIT


def comparable_pool(original: dict, products: list[dict]) -> list[dict]:
    minimum_price = original["price_eur"] * 0.8
    maximum_price = original["price_eur"] * 1.2
    pool = sorted(
        (p for p in products
         if p["product_id"] != original["product_id"]
         and p["category"] == original["category"]
         and p["size"] == original["size"]
         and minimum_price <= p["price_eur"] <= maximum_price),
        key=lambda p: (p["kind"] != original["kind"], p["waterproof"] != original["waterproof"],
                       abs(p["price_eur"] - original["price_eur"]), p["product_id"]),
    )
    return pool[:SCAN_LIMIT]


def display(product: dict) -> dict:
    """What the cart drawer shows for one alternative."""
    return {"product_id": product["product_id"], "name": product["name"], "brand": product["brand"],
            "size": product["size"], "colour": (product.get("colour") or {}).get("name"), "kind": product.get("kind"),
            "waterproof": product.get("waterproof"), "price_eur": product["price_eur"]}
