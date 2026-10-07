"""Jev request text (state, instructions, criteria) built from policy candidates and product facts only.

The sold-out product and every alternative carry the same catalogue attributes (name, brand, category, kind,
waterproofing, use and feel from the description, colour, size, price), and each alternative adds a match summary
against the original. No cart, shopper or offer ids are sent.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .policy import NOTIFY_ID, Candidate


@dataclass(frozen=True)
class Restock:
    """Restock of the sold-out product. `business_s` is the business-time wait (demo clock applied); None = no date."""
    business_s: float | None
    why_undated: str = "no open purchase order"


@dataclass(frozen=True)
class Cart:
    """Cart totals. `exact` False means the contents were not readable: value and items are lower bounds
    (at least the sold-out item)."""
    value_eur: float
    items: int
    exact: bool


def business_duration(seconds: float) -> str:
    """Same wording as the shop (storefront stockView.formatBusinessDuration): 'about 2 days', 'about 5 hours'."""
    def js_round(x: float) -> int:  # JavaScript Math.round: halves go up
        return math.floor(x + 0.5)

    if seconds <= 0:
        return "any moment now"
    minutes = js_round(seconds / 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"about {minutes} minute{'' if minutes == 1 else 's'}"
    hours = js_round(minutes / 60)
    if hours < 48:
        return f"about {hours} hour{'' if hours == 1 else 's'}"
    return f"about {js_round(hours / 24)} days"


_DESCRIPTION = re.compile(r"^(?P<feel>[^,]+), built for (?P<use>.+?)\.?$")


def _use_feel(p: dict) -> tuple[str, str | None]:
    """Use and feel from the catalogue description ("<feel>, built for <use>.", storefront/scripts/export_products.py).
    The catalogue has no separate use or feel fields. Any other wording is passed through as the use."""
    m = _DESCRIPTION.match(p["description"].strip())
    return (m["use"], m["feel"].lower()) if m else (p["description"].strip(), None)


def _attributes(p: dict) -> str:
    """The same attribute set for the sold-out product and every alternative, so they compare line by line."""
    use, feel = _use_feel(p)
    return (f"{p['name']}; brand {p['brand']}; {p['category']}; kind: {p['kind']}; "
            f"{'waterproof' if p['waterproof'] else 'not waterproof'}; use: {use}" + (f"; feel: {feel}" if feel else "")
            + f"; colour {p['colour']['name']}; size {p['size']}; EUR {p['price_eur']:.2f}")


def _alternative(c: Candidate, alt: dict, original: dict) -> str:
    alt_use, alt_feel = _use_feel(alt)
    use, feel = _use_feel(original)
    diff = alt["price_eur"] - original["price_eur"]
    pct = diff / original["price_eur"] * 100
    match = [
        f"same kind: {alt['kind']}" if alt["kind"] == original["kind"] else f"different kind: {alt['kind']} vs {original['kind']}",
        _waterproof_match(alt["waterproof"], original["waterproof"]),
        f"same use: {use}" if alt_use == use else f"different use: {alt_use} vs {use}",
        *([] if alt_feel is None or feel is None else
          ["same feel" if alt_feel == feel else f"different feel: {alt_feel} vs {feel}"]),
        "same brand" if alt["brand"] == original["brand"] else f"different brand: {alt['brand']} vs {original['brand']}",
        "same size in stock" + (f" ({c.stock} units)" if c.stock is not None else ""),
        ("same colour" if alt["colour"]["name"] == original["colour"]["name"]
         else f"different colour: {alt['colour']['name']} vs {original['colour']['name']}"),
        "same price" if abs(diff) < 0.005 else f"{'-' if diff < 0 else '+'}EUR {abs(diff):.2f} / {'-' if diff < 0 else '+'}{abs(pct):.1f}%",
        f"EUR {alt['price_eur'] * (100 - c.discount_pct) / 100:.2f} after {c.discount_pct}% off",
    ]
    return f"{_attributes(alt)}. Match: {'; '.join(match)}."


def _waterproof_match(alt: bool, original: bool) -> str:
    if alt == original:
        return "both waterproof" if alt else "neither waterproof"
    return "waterproof, the sold-out product is not" if alt else "not waterproof, the sold-out product is"


def _notify(original: dict, restock: Restock, near_days: float) -> str:
    wait = business_duration(restock.business_s)
    days = restock.business_s / 86_400
    fit = f"within the {near_days:g}-day limit" if days <= near_days else f"longer than the {near_days:g}-day limit"
    return (f"No replacement: tell the shopper when {original['name']} is back in stock, expected in {wait} "
            f"({fit}).")


def build(original: dict, catalogue: dict[str, dict], options: list[Candidate], restock: Restock, cart: Cart,
          returning_shopper: bool, near_days: float) -> tuple[str, str, dict[str, str]]:
    """(state, instructions, criteria) for Jev's choice question. `options` are policy's Jev options
    (policy.build_candidates): notify-me is among them only when the Restock notice is eligible (a restock date
    within the near-restock limit)."""
    criteria: dict[str, str] = {}
    for c in options:
        if c.id == NOTIFY_ID:
            criteria[c.id] = _notify(original, restock, near_days)
        else:
            criteria[c.id] = _alternative(c, catalogue[c.product_id], original)
    restock_txt = (f"expected in {business_duration(restock.business_s)}" if restock.business_s is not None
                   else f"no date ({restock.why_undated})")
    cart_txt = (f"{cart.items} item{'' if cart.items == 1 else 's'}, EUR {cart.value_eur:.2f}" if cart.exact
                else f"at least 1 item, at least EUR {cart.value_eur:.2f} (contents not available)")
    state = (f"A shopper's cart holds a product that just sold out everywhere: {_attributes(original)}. "
             f"Restock: {restock_txt}. Cart: {cart_txt}. "
             f"Synthetic shopper signal: {'returning' if returning_shopper else 'new'} shopper.")
    instructions = ("Choose the best option for this shopper among the supplied ones. Every alternative is in stock in "
                    "the same size and category; each has a match summary against the sold-out product. Prefer the "
                    "alternative closest to it in kind, waterproofing and use, then in price; colour is a secondary "
                    "preference.")
    if NOTIFY_ID in criteria:
        instructions += (f" Choose notify-me only if the restock is within {near_days:g} days and no alternative "
                         "is a close match.")
    return state, instructions, criteria
