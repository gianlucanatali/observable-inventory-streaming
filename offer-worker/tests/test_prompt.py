import pytest

from offer_worker import prompt
from offer_worker.policy import Candidate, restock_notice_eligible
from offer_worker.prompt import Cart, business_duration

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
         Candidate("alt:P0070", "ALTERNATIVE_PRODUCT", "P0070", 10, 2)]
DAY = 86_400


def build(cart=Cart(219.9, 1, True), alts=CANDS):
    return prompt.build(ORIGINAL, CATALOGUE, alts, cart, False)


def test_the_question_is_which_alternative_is_a_good_substitute_or_none():
    state, instructions, criteria = build()
    assert list(criteria) == ["alt:P0061", "alt:P0070", "none"]
    assert criteria["none"] == ("None of these is a good substitute for Trailrunner GTX: offer no replacement "
                                "for this shopper.")
    assert "good substitute" in instructions and "Choose none if no alternative is a good substitute" in instructions


def test_the_restock_date_is_not_part_of_the_question():
    state, instructions, criteria = build()
    text = state + instructions + " ".join(criteria.values())
    assert "notify" not in text.lower() and "restock" not in text.lower() and "back in stock" not in text.lower()


def test_one_alternative_is_still_a_question_with_none():
    _, _, criteria = build(alts=CANDS[:1])
    assert list(criteria) == ["alt:P0061", "none"]


def test_no_alternative_is_not_a_question():
    with pytest.raises(ValueError, match="at least one eligible alternative"):
        build(alts=[])


def test_restock_notice_needs_a_date_within_the_near_restock_limit():
    assert not restock_notice_eligible(None, 7)
    assert restock_notice_eligible(3 * DAY, 7) and restock_notice_eligible(7 * DAY, 7)
    assert not restock_notice_eligible(7.5 * DAY, 7)


def test_original_and_alternatives_share_one_attribute_set():
    state, _, criteria = build()
    assert ("Trailrunner GTX; brand Alpenpace; footwear; kind: trail running shoe; waterproof; use: long ridge runs; "
            "feel: light and quick; "
            "colour Forest green; size EU 42; EUR 219.90.") in state
    assert criteria["alt:P0061"].startswith("Pathfinder Air; brand Alpenpace; footwear; kind: hiking shoe; not waterproof; "
                                            "use: wet gravel descents; feel: light and quick; colour Ember red; size EU 42; "
                                            "EUR 204.90. Match: ")


def test_match_summary_compares_each_alternative_with_the_original():
    _, instructions, criteria = build()
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
    _, _, criteria = prompt.build(dry, CATALOGUE, CANDS, Cart(1, 1, True), False)
    assert "; neither waterproof; " in criteria["alt:P0061"]
    assert "; waterproof, the sold-out product is not; " in criteria["alt:P0070"]


def test_description_in_another_format_is_passed_through_as_the_use():
    odd = {**CATALOGUE["P0061"], "description": "Waterproof trail shoe"}
    _, _, criteria = prompt.build(ORIGINAL, {**CATALOGUE, "P0061": odd}, CANDS, Cart(1, 1, True), False)
    assert "use: Waterproof trail shoe; colour Ember red" in criteria["alt:P0061"]
    assert "different use: Waterproof trail shoe vs long ridge runs" in criteria["alt:P0061"]


def test_cart_signals_are_consistent_with_the_cart():
    state, _, _ = build(Cart(439.8, 2, True))
    assert "Cart: 2 items, EUR 439.80." in state and "new shopper" in state
    state, _, _ = build(Cart(219.9, 1, False))
    assert "Cart: at least 1 item, at least EUR 219.90 (contents not available)." in state


def test_no_ids_in_the_text():
    state, instructions, criteria = build()
    text = state + instructions + " ".join(criteria.values())
    assert "P0061" not in text and "P0042" not in text


def test_business_duration_matches_the_shop_wording():
    assert [business_duration(s) for s in (0, 20, 60, 1800, 3600, 47 * 3600, 2 * DAY, 60 * 3600)] == [
        "any moment now", "less than a minute", "about 1 minute", "about 30 minutes", "about 1 hour", "about 47 hours", "about 2 days", "about 3 days"]
