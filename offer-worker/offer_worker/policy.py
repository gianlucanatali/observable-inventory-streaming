"""Permitted candidates and fixed terms. Policy owns eligibility, availability and every financial term."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

MAX_ALTERNATIVES = 2
SCAN_LIMIT = 12  # eligible products examined (most comparable first) when looking for in-stock alternatives
NOTIFY_ID = "notify_me"

# Lookup of a product's confirmed minimum (offer_worker/stock.py): the quantity counted only from live
# stores, the same number the shop promises. None when it cannot be computed. Only > 0 is eligible; None never is.
ConfirmedMinLookup = Callable[[str], "int | None"]


@dataclass(frozen=True)
class Candidate:
    id: str                    # "notify_me" or "alt:<product_id>"
    offer_type: str            # ALTERNATIVE_PRODUCT | NOTIFY_ME (SAME_PRODUCT_OTHER_STORE is never offered: online sellable is 0 everywhere)
    product_id: str | None
    discount_pct: int
    stock: int | None = None   # confirmed minimum when the candidate was built (alternatives only); context for Jev


def load_catalogue(path: str) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    return {p["product_id"]: p for p in items}


def build_candidates(original: dict, catalogue: dict[str, dict], confirmed_min: ConfirmedMinLookup, discount_pct: int,
                     restock_notice: bool = True) -> list[Candidate]:
    """Eligible alternatives (most comparable first, at most MAX_ALTERNATIVES), then the Restock notice when it is
    eligible (`restock_notice`: an open purchase order due within the near-restock limit). Empty when neither exists:
    then there is no Offer at all."""
    minimum_price = original["price_eur"] * 0.8
    maximum_price = original["price_eur"] * 1.2
    pool = sorted(
        (p for p in catalogue.values()
         if p["product_id"] != original["product_id"]
         and p["category"] == original["category"]
         and p["size"] == original["size"]
         and minimum_price <= p["price_eur"] <= maximum_price),
        # Most comparable first: same kind, then same waterproofing, then closest price.
        key=lambda p: (p["kind"] != original["kind"], p["waterproof"] != original["waterproof"],
                       abs(p["price_eur"] - original["price_eur"]), p["product_id"]),
    )
    cands: list[Candidate] = []
    for p in pool[:SCAN_LIMIT]:
        qty = confirmed_min(p["product_id"])
        if qty is not None and qty > 0:
            cands.append(Candidate(f"alt:{p['product_id']}", "ALTERNATIVE_PRODUCT", p["product_id"], discount_pct, qty))
        if len(cands) >= MAX_ALTERNATIVES:
            break
    if restock_notice:
        cands.append(Candidate(NOTIFY_ID, "NOTIFY_ME", None, 0))
    return cands


def restock_notice_eligible(restock_business_s: float | None, near_days: float) -> bool:
    """A Restock notice needs a restock date (an open purchase order) due within the near-restock limit."""
    return restock_business_s is not None and restock_business_s / 86_400 <= near_days


def rule_default(candidates: list[Candidate]) -> Candidate | None:
    """Policy's own pick (the Safe rule): the most comparable in-stock alternative (same kind, then same
    waterproofing, then closest price), else the Restock notice when eligible (always last), else None: no Offer."""
    return candidates[0] if candidates else None


def _same_model(original: dict, alt: dict) -> bool:
    if original.get("model_id") and alt.get("model_id"):
        return original["model_id"] == alt["model_id"]
    return (original["name"], original["brand"]) == (alt["name"], alt["brand"])


NO_OFFER_BODY = "Sold out everywhere, and nothing comparable is in stock right now."


def template_text(offer_type: str | None, original: dict, alt: dict | None, discount_pct: int,
                  restock_wait: str | None = None) -> tuple[str, str]:
    """Headline and body. `offer_type` None means no Offer. The alternative's colour is named whenever it differs
    from the sold-out item's; the same model in another colour is called "the same <name>"."""
    headline = f"{original['name']} just sold out"
    if offer_type == "ALTERNATIVE_PRODUCT" and alt is not None:
        take = f"Take {discount_pct}% off if you switch."
        other_colour = alt["colour"]["name"] != original["colour"]["name"]
        if _same_model(original, alt):
            colour = f" in {alt['colour']['name']}" if other_colour else ""
            return headline, f"The same {alt['name']}{colour} is in stock. {take}"
        colour = f", {alt['colour']['name']}," if other_colour else ""
        return headline, f"{alt['name']} by {alt['brand']}{colour} is in stock and similar. {take}"
    if offer_type == "NOTIFY_ME":
        back = f"Back in {restock_wait}. " if restock_wait else ""
        return headline, f"{back}We will let you know as soon as it is back in stock."
    return headline, NO_OFFER_BODY
