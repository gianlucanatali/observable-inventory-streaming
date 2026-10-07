from offer_worker import prompt
from offer_worker.policy import Candidate, restock_notice_eligible
from offer_worker.prompt import Cart, Restock, business_duration

ORIGINAL = {"product_id": "P0042", "name": "Trailrunner GTX", "brand": "Alpenpace", "category": "footwear", "size": "EU 42",
            "price_eur": 219.9, "colour": {"name": "Forest green"}, "kind": "trail running shoe", "waterproof": True,
            "description": "Light and quick, built for long ridge runs."}
CATALOGUE = {
    "P0042": ORIGINAL,
    "P0061": {**ORIGINAL, "product_id": "P0061", "name": "Pathfinder Air", "price_eur": 204.9,
              "colour": {"name": "Ember red"}, "kind": "hiking shoe", "waterproof": False,
              "description": "Light and quick, built for wet gravel descents."},
    "P0070": {**ORIGINAL, "product_id": "P0070", "name": "Ridge Pro", "brand": "Northline", "price_eur": 239.9,
              "description": "Durable and dependable, built for long ridge runs."},
}
CANDS = [Candidate("alt:P0061", "ALTERNATIVE_PRODUCT", "P0061", 10, 7),
         Candidate("alt:P0070", "ALTERNATIVE_PRODUCT", "P0070", 10, 2),
         Candidate("notify_me", "NOTIFY_ME", None, 0)]
DAY = 86_400


def build(restock, cart=Cart(219.9, 1, True), near_days=7.0):
    options = CANDS if restock.business_s is not None else CANDS[:2]  # policy drops notify-me without a date
    return prompt.build(ORIGINAL, CATALOGUE, options, restock, cart, False, near_days)


def test_no_restock_date_leaves_notify_me_out_and_says_why():
    state, instructions, criteria = build(Restock(None, "no open purchase order"))
    assert list(criteria) == ["alt:P0061", "alt:P0070"]
    assert "Restock: no date (no open purchase order)" in state
    assert "notify-me" not in instructions


def test_restock_notice_needs_a_date_within_the_near_restock_limit():
    assert not restock_notice_eligible(None, 7)
    assert restock_notice_eligible(3 * DAY, 7) and restock_notice_eligible(7 * DAY, 7)
    assert not restock_notice_eligible(7.5 * DAY, 7)


def test_near_restock_date_is_concrete_and_within_the_limit():
    state, instructions, criteria = build(Restock(3 * DAY))
    assert "Restock: expected in about 3 days" in state
    assert criteria["notify_me"] == ("No replacement: tell the shopper when Trailrunner GTX is back in stock, "
                                     "expected in about 3 days (within the 7-day limit).")
    assert "Choose notify-me only if the restock is within 7 days" in instructions


def test_far_restock_date_is_marked_beyond_the_limit():
    _, instructions, criteria = build(Restock(12 * DAY), near_days=5)
    assert "about 12 days (longer than the 5-day limit)" in criteria["notify_me"]
    assert "within 5 days" in instructions


def test_original_and_alternatives_share_one_attribute_set():
    state, _, criteria = build(Restock(None))
    assert ("Trailrunner GTX; brand Alpenpace; footwear; kind: trail running shoe; waterproof; use: long ridge runs; "
            "feel: light and quick; "
            "colour Forest green; size EU 42; EUR 219.90.") in state
    assert criteria["alt:P0061"].startswith("Pathfinder Air; brand Alpenpace; footwear; kind: hiking shoe; not waterproof; "
                                            "use: wet gravel descents; feel: light and quick; colour Ember red; size EU 42; "
                                            "EUR 204.90. Match: ")


def test_match_summary_compares_each_alternative_with_the_original():
    _, instructions, criteria = build(Restock(None))
    assert criteria["alt:P0061"].endswith(
        "Match: different kind: hiking shoe vs trail running shoe; not waterproof, the sold-out product is; "
        "different use: wet gravel descents vs long ridge runs; same feel; same brand; same size in stock (7 units); "
        "different colour: Ember red vs Forest green; -EUR 15.00 / -6.8%; EUR 184.41 after 10% off.")
    other = criteria["alt:P0070"]
    assert other.split("Match: ")[1].startswith("same kind: trail running shoe; both waterproof; same use: long ridge runs; "
                                                 "different feel: durable and dependable vs light and quick")
    assert "different brand: Northline vs Alpenpace" in other and "same colour" in other and "+EUR 20.00 / +9.1%" in other
    assert "closest to it in kind, waterproofing and use, then in price; colour is a secondary preference" in instructions


def test_waterproofing_is_compared_both_ways():
    dry = {**ORIGINAL, "waterproof": False}
    _, _, criteria = prompt.build(dry, CATALOGUE, CANDS[:2], Restock(None), Cart(1, 1, True), False, 7)
    assert "; neither waterproof; " in criteria["alt:P0061"]
    assert "; waterproof, the sold-out product is not; " in criteria["alt:P0070"]


def test_description_in_another_format_is_passed_through_as_the_use():
    odd = {**CATALOGUE["P0061"], "description": "Waterproof trail shoe"}
    _, _, criteria = prompt.build(ORIGINAL, {**CATALOGUE, "P0061": odd}, CANDS[:2], Restock(None), Cart(1, 1, True), False, 7)
    assert "use: Waterproof trail shoe; colour Ember red" in criteria["alt:P0061"]
    assert "different use: Waterproof trail shoe vs long ridge runs" in criteria["alt:P0061"]


def test_cart_signals_are_consistent_with_the_cart():
    state, _, _ = build(Restock(None), Cart(439.8, 2, True))
    assert "Cart: 2 items, EUR 439.80." in state and "new shopper" in state
    state, _, _ = build(Restock(None), Cart(219.9, 1, False))
    assert "Cart: at least 1 item, at least EUR 219.90 (contents not available)." in state


def test_no_ids_in_the_text():
    state, instructions, criteria = build(Restock(DAY))
    text = state + instructions + " ".join(criteria.values())
    assert "P0061" not in text and "P0042" not in text


def test_business_duration_matches_the_shop_wording():
    assert [business_duration(s) for s in (0, 20, 60, 1800, 3600, 47 * 3600, 2 * DAY, 60 * 3600)] == [
        "any moment now", "less than a minute", "about 1 minute", "about 30 minutes", "about 1 hour", "about 47 hours", "about 2 days", "about 3 days"]
