"""Demo data from contracts/README.md section 1. Pure and deterministic."""
from __future__ import annotations

import hashlib
import random

STORES = [f"S{i:02d}" for i in range(1, 6)]
PRODUCTS = [f"P{i:04d}" for i in range(1, 201)]
SELL_OUT_PRODUCT = "P0042"
SELL_OUT_QUANTITIES = {"S01": 2, "S02": 1, "S03": 3, "S04": 1, "S05": 2}  # sellable 9
SEED = 42


def seed_rows() -> list[tuple[str, str, int]]:
    rng = random.Random(SEED)
    rows = []
    for store in STORES:
        for product in PRODUCTS:
            qty = rng.randint(0, 40)
            if product == SELL_OUT_PRODUCT:
                qty = SELL_OUT_QUANTITIES[store]
            rows.append((store, product, qty))
    return rows


def seed_rows_for(store: str) -> list[tuple[str, str, int]]:
    """Only this store's rows: each source holds its own store only."""
    return [r for r in seed_rows() if r[0] == store]


def expected_sellable(rows: list[tuple[str, str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for _, product, qty in rows:
        out[product] = out.get(product, 0) + qty
    return out


def seed_hash(rows: list[tuple[str, str, int]] | None = None) -> str:
    rows = seed_rows() if rows is None else rows
    text = "".join(f"{s},{p},{q}\n" for s, p, q in rows)
    return hashlib.sha256(text.encode()).hexdigest()


def product_weights(skew: float) -> dict[str, float]:
    """Relative demand per product for the background sales (table product_weight).

    Zipf-like: ranks are a seeded shuffle of the products, weight ~ 1 / rank**skew (skew 0 = all equal),
    normalised so the mean weight over the selling products is 1. The sell-out product gets weight 0: only
    `sell-out` ever sells it. Same weights in every source."""
    if skew < 0:
        raise ValueError(f"demand skew must be >= 0, got {skew}")
    selling = [p for p in PRODUCTS if p != SELL_OUT_PRODUCT]
    random.Random(SEED + 1).shuffle(selling)  # separate stream: the stock seed above stays unchanged
    raw = {p: 1.0 / (rank ** skew) for rank, p in enumerate(selling, start=1)}
    mean = sum(raw.values()) / len(raw)
    weights = {p: w / mean for p, w in raw.items()}
    weights[SELL_OUT_PRODUCT] = 0.0
    return weights
