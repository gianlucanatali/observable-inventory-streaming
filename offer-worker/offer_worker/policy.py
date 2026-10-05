"""Permitted candidates and fixed terms. Policy owns eligibility, availability and every financial term."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

MAX_ALTERNATIVES = 2
SCAN_LIMIT = 12  # eligible products examined (closest price first) when looking for in-stock alternatives
NOTIFY_ID = "notify_me"

# Lookup of the sellable quantity for a product: int, or None when unknown. Never treated as available.
SellableLookup = Callable[[str], "int | None"]


@dataclass(frozen=True)
class Candidate:
    id: str                    # "notify_me" or "alt:<product_id>"
    offer_type: str            # ALTERNATIVE_PRODUCT | NOTIFY_ME (SAME_PRODUCT_OTHER_STORE is never offered: online sellable is 0 everywhere)
    product_id: str | None
    discount_pct: int
    description: str           # what Jev reads; facts only


def load_catalogue(path: str) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    return {p["product_id"]: p for p in items}


def _describe(p: dict) -> str:
    return f"{p['name']} by {p['brand']}, {p['category']}, size {p['size']}, {p['colour']['name']}, EUR {p['price_eur']:.2f}"


def build_candidates(original: dict, catalogue: dict[str, dict], sellable: SellableLookup, discount_pct: int) -> list[Candidate]:
    minimum_price = original["price_eur"] * 0.8
    maximum_price = original["price_eur"] * 1.2
    pool = sorted(
        (p for p in catalogue.values()
         if p["product_id"] != original["product_id"]
         and p["category"] == original["category"]
         and p["size"] == original["size"]
         and minimum_price <= p["price_eur"] <= maximum_price),
        key=lambda p: (abs(p["price_eur"] - original["price_eur"]), p["product_id"]),
    )
    cands: list[Candidate] = []
    for p in pool[:SCAN_LIMIT]:
        qty = sellable(p["product_id"])
        if qty is not None and qty > 0:
            cands.append(Candidate(f"alt:{p['product_id']}", "ALTERNATIVE_PRODUCT", p["product_id"], discount_pct,
                                   f"Similar in-stock product with {discount_pct}% off: {_describe(p)}"))
        if len(cands) >= MAX_ALTERNATIVES:
            break
    cands.append(Candidate(NOTIFY_ID, "NOTIFY_ME", None, 0,
                           "No replacement: tell the shopper when the original product is back in stock"))
    return cands


def rule_default(candidates: list[Candidate]) -> Candidate:
    """Policy's own pick: the closest-priced in-stock alternative, else notify-me (always last)."""
    return candidates[0]


def template_text(offer_type: str, original: dict, alt: dict | None, discount_pct: int) -> tuple[str, str]:
    if offer_type == "ALTERNATIVE_PRODUCT" and alt is not None:
        return (f"{original['name']} just sold out",
                f"{alt['name']} by {alt['brand']} is in stock and similar. Take {discount_pct}% off if you switch.")
    return (f"{original['name']} just sold out",
            "We will let you know as soon as it is back in stock.")
