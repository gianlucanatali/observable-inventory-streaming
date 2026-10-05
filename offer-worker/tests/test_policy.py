from offer_worker.policy import build_candidates, rule_default


def product(product_id, *, category="footwear", size="EU 42", price_eur=100.0):
    return {
        "product_id": product_id,
        "name": product_id,
        "brand": "Brand",
        "category": category,
        "size": size,
        "price_eur": price_eur,
        "colour": {"name": "Blue"},
    }


def test_candidates_accept_price_band_boundaries_and_reject_ineligible_products():
    original = product("P0042")
    catalogue = {
        original["product_id"]: original,
        "P0001": product("P0001", price_eur=80.0),
        "P0002": product("P0002", price_eur=120.0),
        "P0003": product("P0003", size="EU 37"),
        "P0004": product("P0004", category="accessories"),
        "P0005": product("P0005", price_eur=79.99),
        "P0006": product("P0006", price_eur=120.01),
        "P0007": product("P0007"),
        "P0008": product("P0008"),
    }
    sellable = {"P0001": 3, "P0002": 1, "P0003": 5, "P0004": 5, "P0005": 5, "P0006": 5, "P0007": 0, "P0008": None}

    candidates = build_candidates(original, catalogue, sellable.get, 10)

    assert [candidate.id for candidate in candidates] == ["alt:P0001", "alt:P0002", "notify_me"]
    assert rule_default(candidates).id == "alt:P0001"


def test_p0042_candidates_reject_wrong_size(catalogue, redis_client):
    original = catalogue["P0042"]

    candidates = build_candidates(original, catalogue, lambda product_id: int(redis_client.hget(f"sellable:{product_id}", "sellable")), 10)
    alternatives = [candidate for candidate in candidates if candidate.product_id is not None]

    assert alternatives
    assert all(catalogue[candidate.product_id]["category"] == original["category"] for candidate in alternatives)
    assert all(catalogue[candidate.product_id]["size"] == original["size"] for candidate in alternatives)
    assert all(0.8 * original["price_eur"] <= catalogue[candidate.product_id]["price_eur"] <= 1.2 * original["price_eur"] for candidate in alternatives)
    assert rule_default(candidates) in alternatives
