import json
import logging

import fastavro
import pytest

import offer_worker.worker as worker_mod
from conftest import CONTRACT_AVRO, NOW, FakeMetrics, make_cfg, risk
from offer_worker.bedrock import TextError, TextTimeout, allowed_numbers, validate_text
from offer_worker.config import Config
from offer_worker.jev import JevChoice, JevError, JevRateLimited, JevTimeout
from offer_worker.logs import JsonFormatter
from offer_worker.policy import build_candidates, rule_default
from offer_worker.worker import OfferWorker


class FakeJev:
    def __init__(self, choice=None, confidence=0.95, exc=None, pick="first_alt"):
        self.choice, self.confidence, self.exc, self.calls = choice, confidence, exc, []

    def choose(self, state, instructions, criteria):
        self.calls.append((state, instructions, criteria))
        if self.exc:
            raise self.exc
        return JevChoice(self.choice or next(iter(criteria)), self.confidence)


class FakeText:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.calls = text, exc, []

    def write(self, facts):
        self.calls.append(facts)
        if self.exc:
            raise self.exc
        return self.text


def build(cfg, catalogue, redis_client, jev=None, text=None):
    out, m = [], FakeMetrics()
    w = OfferWorker(cfg, redis_client, catalogue, lambda k, v: out.append((k, v)), m, jev, text, clock=lambda: NOW)
    return w, out, m


def only(out):
    assert len(out) == 1
    return out[0][1]


def test_high_confidence_valid_choice_is_accepted(catalogue, redis_client):
    jev = FakeJev(choice=None)  # first criterion = best alternative
    w, out, m = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    assert w.handle(risk()) == "published"
    o = only(out)
    assert (o["decision_route"], o["decision_reason"]) == ("JEV", "accepted")
    assert o["offer_type"] == "ALTERNATIVE_PRODUCT" and o["discount_pct"] == 10
    assert o["original_product_id"] == "P0042" and o["product_id"] != "P0042"
    assert ("decision", "JEV", "accepted") in m.calls and ("completed", "ALTERNATIVE_PRODUCT") in m.calls
    assert "C1" not in jev.calls[0][0] and "sh1" not in jev.calls[0][0]  # no ids sent to Jev


def test_decision_span_and_offer_publish_bounded_metadata_without_shopper_identity(catalogue, redis_client, monkeypatch):
    class Span:
        def __init__(self, name):
            self.name, self.tags = name, {}

        def set_tag(self, key, value):
            self.tags[key] = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Tracer:
        def __init__(self):
            self.spans = []

        def trace(self, name, **kwargs):
            span = Span(name)
            self.spans.append(span)
            return span

    tracer = Tracer()
    monkeypatch.setattr(worker_mod, "tracer", tracer)
    redis_client.set("restock:eta:P0042", str(int(NOW * 1000) + 3_600_000))  # dated restock: notify-me is offered
    w, out, _ = build(make_cfg(JEV_API_KEY="secret-key"), catalogue, redis_client,
                      FakeJev(choice="notify_me", confidence=0.76))

    assert w.handle(risk()) == "published"

    offer = only(out)
    assert {key: offer[key] for key in ("jev_choice", "jev_confidence", "min_confidence", "rule_choice", "chosen_choice")} == {
        "jev_choice": "notify_me", "jev_confidence": 0.76, "min_confidence": 0.8,
        "rule_choice": "alt:P0160", "chosen_choice": "alt:P0160"}
    decision = next(span for span in tracer.spans if span.name == "offer.decision")
    assert decision.tags == {
        "offer.route": "RULE_DEFAULT", "offer.reason": "low_confidence", "jev.choice": "notify_me",
        "jev.confidence": 0.76, "offer.min_confidence": 0.8, "rule.choice": "alt:P0160",
        "offer.chosen": "alt:P0160"}
    assert "secret-key" not in json.dumps(offer) and "sh1" not in json.dumps(offer)


def test_jev_context_with_dated_restock_offers_notify_me_with_a_concrete_wait(catalogue, redis_client):
    redis_client.set("restock:eta:P0042", str(int(NOW * 1000) + 3_600_000))  # 1 real hour; demo clock 60x = 2.5 days, shown as the shop rounds it
    redis_client.hset("demo:config", "time_compression", "60")
    redis_client.hset("cart:S1:C1", mapping={"P0042": 1, "P0001": 2})
    jev = FakeJev()
    w, _, _ = build(make_cfg(), catalogue, redis_client, jev)

    assert w.handle(risk()) == "published"

    state, instructions, criteria = jev.calls[0]
    assert "Trailrunner GTX" in state and "Restock: expected in about 3 days" in state
    value = catalogue["P0042"]["price_eur"] + 2 * catalogue["P0001"]["price_eur"]
    assert f"Cart: 3 items, EUR {value:.2f}" in state and "returning shopper" in state
    assert "notify-me only if the restock is within 7 days" in instructions
    assert "expected in about 3 days (within the 7-day limit)" in criteria["notify_me"]
    assert any(choice.startswith("alt:") for choice in criteria)


def test_undated_restock_leaves_notify_me_out_of_jev_options(catalogue, redis_client):
    jev = FakeJev()
    w, _, _ = build(make_cfg(), catalogue, redis_client, jev)

    assert w.handle(risk()) == "published"

    state, instructions, criteria = jev.calls[0]
    assert "notify_me" not in criteria and len(criteria) == 2
    assert "Restock: no date (no open purchase order)" in state and "notify-me" not in instructions


def test_jev_choosing_notify_me_when_it_was_not_offered_is_invalid(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client, FakeJev(choice="notify_me"))
    w.handle(risk())
    o = only(out)
    assert (o["decision_route"], o["decision_reason"], o["offer_type"]) == ("RULE_DEFAULT", "invalid_choice", "ALTERNATIVE_PRODUCT")


def test_one_alternative_and_no_restock_date_is_no_choice_without_a_jev_call(catalogue, redis_client):
    original = catalogue["P0042"]
    keep = None
    for pid, p in catalogue.items():
        same = p["category"] == original["category"] and p["size"] == original["size"] and pid != "P0042"
        if same and keep is None and 0.8 * original["price_eur"] <= p["price_eur"] <= 1.2 * original["price_eur"]:
            keep = pid
        elif pid != keep:
            redis_client.hset(f"sellable:{pid}", "sellable", 0)
    jev = FakeJev()
    w, out, m = build(make_cfg(), catalogue, redis_client, jev)
    w.handle(risk())
    o = only(out)
    assert jev.calls == [] and (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", "no_choice")
    assert o["offer_type"] == "ALTERNATIVE_PRODUCT" and o["product_id"] == keep and o["rule_choice"] == f"alt:{keep}"


def test_cart_without_readable_contents_gives_lower_bounds_not_the_random_signal(catalogue, redis_client):
    jev = FakeJev()
    w, _, _ = build(make_cfg(), catalogue, redis_client, jev)
    w.handle(risk())  # risk carries cart_value_eur 189.50, item_count 3: synthetic and independent of the contents
    state = jev.calls[0][0]
    assert "Cart: at least 1 item, at least EUR 219.90 (contents not available)" in state
    assert "189.50" not in state and "3 items" not in state


@pytest.mark.parametrize(("choice", "confidence", "route", "reason", "logged_choice"), [
    ("alt:P0160", 0.95, "JEV", "accepted", "alt:P0160"),
    ("alt:P0160", 0.5, "RULE_DEFAULT", "low_confidence", "alt:P0160"),
    ("alt:P9999", 0.95, "RULE_DEFAULT", "invalid_choice", "alt:P9999"),
    ("secret-key", 0.95, "RULE_DEFAULT", "invalid_choice", "unrecognized"),
])
def test_offer_published_log_includes_jev_response_diagnostics_without_request_secrets(catalogue, redis_client, caplog,
                                                                                         choice, confidence, route, reason, logged_choice):
    w, _, _ = build(make_cfg(JEV_API_KEY="secret-key"), catalogue, redis_client,
                     FakeJev(choice=choice, confidence=confidence))
    caplog.set_level("INFO", logger="offer_worker")

    assert w.handle(risk()) == "published"

    record = next(r for r in caplog.records if r.message == "offer published")
    structured = json.loads(JsonFormatter().format(record))
    assert structured["decision"] == f"{route}/{reason}"
    assert structured["jev_confidence"] == confidence
    assert 0.0 <= structured["jev_confidence"] <= 1.0
    assert structured["jev_choice"] == logged_choice
    assert "secret-key" not in json.dumps(structured) and "sh1" not in json.dumps(structured)


def test_json_log_trace_correlation_uses_datadog_compatible_low_64_bits(monkeypatch):
    class ActiveSpan:
        trace_id = (0x1234 << 64) | 42
        span_id = 99

    monkeypatch.setattr("ddtrace.tracer.current_span", lambda: ActiveSpan())
    record = logging.LogRecord("offer_worker", logging.INFO, __file__, 1, "correlated", (), None)

    structured = json.loads(JsonFormatter().format(record))

    assert structured["dd.trace_id"] == "42"
    assert structured["dd.span_id"] == "99"


def test_jev_can_choose_notify_me(catalogue, redis_client):
    redis_client.set("restock:eta:P0042", str(int(NOW * 1000) + 3_600_000))
    w, out, _ = build(make_cfg(), catalogue, redis_client, FakeJev(choice="notify_me"))
    w.handle(risk())
    o = only(out)
    assert o["offer_type"] == "NOTIFY_ME" and o["discount_pct"] == 0 and o["product_id"] is None and o["decision_route"] == "JEV"


@pytest.mark.parametrize("jev,reason", [
    (FakeJev(confidence=0.5), "low_confidence"),
    (FakeJev(choice="alt:P9999"), "invalid_choice"),
    (FakeJev(exc=JevTimeout("t")), "timeout"),
    (FakeJev(exc=JevRateLimited("r")), "rate_limited"),
    (FakeJev(exc=JevError("e")), "error"),
])
def test_everything_else_is_rule_default(catalogue, redis_client, jev, reason):
    w, out, m = build(make_cfg(), catalogue, redis_client, jev)
    w.handle(risk())
    o = only(out)
    assert (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", reason)
    assert o["offer_type"] == "ALTERNATIVE_PRODUCT"  # policy's own pick
    assert ("decision", "RULE_DEFAULT", reason) in m.calls


def test_confidence_threshold_is_configurable(catalogue, redis_client):
    w, out, _ = build(make_cfg(JEV_MIN_CONFIDENCE="0.99"), catalogue, redis_client, FakeJev(confidence=0.95))
    w.handle(risk())
    assert only(out)["decision_reason"] == "low_confidence"


def test_kill_switch_env_makes_no_jev_call(catalogue, redis_client):
    jev = FakeJev()
    w, out, _ = build(make_cfg(OFFERS_KILL_SWITCH="true"), catalogue, redis_client, jev)
    w.handle(risk())
    assert jev.calls == [] and only(out)["decision_reason"] == "kill_switch"


def test_kill_switch_redis_key_is_live(catalogue, redis_client):
    jev = FakeJev()
    w, out, _ = build(make_cfg(), catalogue, redis_client, jev)
    redis_client.set("offers:kill_switch", "1")
    w.handle(risk("S1|C1|P0042|1"))
    redis_client.set("offers:kill_switch", "0")
    w.handle(risk("S1|C1|P0042|2"))
    assert [o["decision_reason"] for _, o in out] == ["kill_switch", "accepted"]


def test_no_jev_configured_is_rule_default_disabled(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client, None)
    w.handle(risk())
    assert only(out)["decision_reason"] == "disabled"


def test_dedup_and_tombstone_and_stale(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client)
    assert w.handle(risk()) == "published"
    assert w.handle(risk()) == "duplicate"
    assert w.handle(None) == "tombstone"
    assert w.handle(risk("other", detected=NOW - 301)) == "stale"
    assert len(out) == 1


def test_prior_scenario_risk_is_expired_before_dedup_or_jev(catalogue, redis_client):
    redis_client.set("scenario:current", "S2")
    jev = FakeJev()
    w, out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)

    old_risk = {**risk(rid="S1|C1|P0042|old"), "scenario_id": "S1"}
    current_risk = {**risk(rid="S2|C1|P0042|current"), "scenario_id": "S2"}

    assert w.handle(old_risk) == "expired"
    assert w.handle(current_risk) == "published"
    assert len(out) == len(jev.calls) == 1
    assert only(out)["scenario_id"] == "S2"


class SellsOutBetween(FakeJev):
    """Jev picks the first alternative; `gone` alternatives sell out after the candidates were built."""
    def __init__(self, redis_client, gone="chosen"):
        super().__init__()
        self.r, self.gone = redis_client, gone

    def choose(self, state, instructions, criteria):
        alts = [k for k in criteria if k.startswith("alt:")]
        for a in (alts if self.gone == "all" else alts[:1]):
            self.r.hset(f"sellable:{a[4:]}", "sellable", 0)
        return JevChoice(alts[0], 0.99)


def test_revalidation_failure_falls_back_to_the_next_valid_alternative(catalogue, redis_client):
    jev = SellsOutBetween(redis_client)
    w, out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    w.handle(risk())
    o = only(out)
    assert (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", "invalid_choice")
    assert o["offer_type"] == "ALTERNATIVE_PRODUCT" and o["chosen_choice"] == f"alt:{o['product_id']}"
    assert o["product_id"] != o["jev_choice"][4:]


def test_revalidation_failure_with_nothing_left_and_no_restock_date_is_no_offer(catalogue, redis_client):
    w, out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, SellsOutBetween(redis_client, gone="all"))
    w.handle(risk())
    o = only(out)
    assert (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", "invalid_choice")
    assert o["chosen_choice"] is None and o["product_id"] is None and o["discount_pct"] == 0  # never notify-me undated


def test_revalidation_failure_with_nothing_left_falls_back_to_a_near_restock_notice(catalogue, redis_client):
    redis_client.set("restock:eta:P0042", str(int(NOW * 1000) + 3_600_000))  # 60 business hours with the demo clock
    w, out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, SellsOutBetween(redis_client, gone="all"))
    w.handle(risk())
    o = only(out)
    assert o["offer_type"] == "NOTIFY_ME" and o["chosen_choice"] == "notify_me" and o["decision_reason"] == "invalid_choice"


def test_no_stock_anywhere_and_no_restock_date_is_no_offer(catalogue, redis_client):
    for pid in catalogue:
        redis_client.hset(f"sellable:{pid}", "sellable", 0)
    jev = FakeJev()
    w, out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    w.handle(risk())
    o = only(out)
    assert jev.calls == [] and o["chosen_choice"] is None and o["decision_reason"] == "no_alternative"


# P0092 Pathfinder Lite, EU 42, EUR 119.90: no other EU 42 footwear lies within EUR 95.92-143.88, so it never has an
# eligible alternative, whatever the stock (the Lab 5.1 "nothing comparable" product).
NO_ALT = "P0092"


def test_p0092_has_no_comparable_product_in_the_catalogue(catalogue):
    assert build_candidates(catalogue[NO_ALT], catalogue, lambda pid: 5, 10, restock_notice=False) == []


def test_no_eligible_alternative_and_no_restock_date_publishes_no_offer_without_a_jev_call(catalogue, redis_client):
    jev = FakeJev()
    w, out, m = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    assert w.handle(risk(rid=f"S1|C1|{NO_ALT}|1", product=NO_ALT)) == "published"
    o = only(out)
    assert jev.calls == []
    assert (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", "no_alternative")
    assert o["chosen_choice"] is None and o["rule_choice"] is None and o["jev_choice"] is None
    assert o["product_id"] is None and o["discount_pct"] == 0 and o["offer_type"] == "NOTIFY_ME"  # required enum placeholder
    assert o["headline"] == "Pathfinder Lite just sold out"
    assert o["body"] == "Sold out everywhere, and nothing comparable is in stock right now."
    assert (o["text_route"], o["text_reason"]) == ("TEMPLATE", "no_offer")
    assert ("completed", "NONE") in m.calls and ("decision", "RULE_DEFAULT", "no_alternative") in m.calls
    fastavro.validation.validate(o, fastavro.parse_schema(json.loads((CONTRACT_AVRO / "offer.avsc").read_text())))


def test_no_eligible_alternative_with_a_near_purchase_order_is_a_restock_notice(catalogue, redis_client):
    redis_client.set(f"restock:eta:{NO_ALT}", str(int(NOW * 1000) + 3_600_000))  # 1 real hour = 60 business hours
    jev = FakeJev()
    w, out, m = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    w.handle(risk(rid=f"S1|C1|{NO_ALT}|1", product=NO_ALT))
    o = only(out)
    assert jev.calls == [] and (o["decision_route"], o["decision_reason"]) == ("RULE_DEFAULT", "no_alternative")
    assert o["offer_type"] == "NOTIFY_ME" and o["chosen_choice"] == o["rule_choice"] == "notify_me"
    assert o["body"] == "Back in about 3 days. We will let you know as soon as it is back in stock."
    assert ("completed", "NOTIFY_ME") in m.calls


def test_no_eligible_alternative_with_a_far_purchase_order_is_no_offer(catalogue, redis_client):
    redis_client.set(f"restock:eta:{NO_ALT}", str(int(NOW * 1000) + 10 * 24 * 60_000))  # 10 business days
    w, out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, FakeJev())
    w.handle(risk(rid=f"S1|C1|{NO_ALT}|1", product=NO_ALT))
    assert only(out)["chosen_choice"] is None


def test_restock_beyond_the_near_limit_is_not_a_jev_option(catalogue, redis_client):
    redis_client.set("restock:eta:P0042", str(int(NOW * 1000) + 10 * 24 * 60_000))
    jev = FakeJev()
    w, _, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    w.handle(risk())
    assert "notify_me" not in jev.calls[0][2]


def test_same_model_in_another_colour_names_the_difference(catalogue, redis_client):
    """P0048 Dolomia Evo Charcoal: the Safe rule's pick is P0059, the same boot in Glacier blue."""
    w, out, _ = build(make_cfg(), catalogue, redis_client)
    w.handle(risk(rid="S1|C1|P0048|1", product="P0048"))
    o = only(out)
    assert o["product_id"] == "P0059"
    assert o["body"] == "The same Dolomia Evo in Glacier blue is in stock. Take 10% off if you switch."


def test_other_model_in_another_colour_names_the_colour(catalogue):
    from offer_worker.policy import template_text
    _, body = template_text("ALTERNATIVE_PRODUCT", catalogue["P0042"], catalogue["P0061"], 10)
    assert body == "Pathfinder Air by Alpenpace, Ember red, is in stock and similar. Take 10% off if you switch."
    same_colour = {**catalogue["P0061"], "colour": catalogue["P0042"]["colour"]}
    _, body = template_text("ALTERNATIVE_PRODUCT", catalogue["P0042"], same_colour, 10)
    assert body == "Pathfinder Air by Alpenpace is in stock and similar. Take 10% off if you switch."


def test_alternative_is_same_category_and_in_stock(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client)
    w.handle(risk())
    alt = catalogue[only(out)["product_id"]]
    assert alt["category"] == catalogue["P0042"]["category"]


def test_jev_and_rule_default_choose_from_the_same_eligible_candidates(catalogue, redis_client):
    original = catalogue["P0042"]
    candidates = build_candidates(original, catalogue,
                                  lambda product_id: int(redis_client.hget(f"sellable:{product_id}", "sellable")), 10)
    default = rule_default(candidates)
    jev = FakeJev(choice=default.id)

    jev_worker, jev_out, _ = build(make_cfg(JEV_API_KEY="k"), catalogue, redis_client, jev)
    default_worker, default_out, _ = build(make_cfg(), catalogue, redis_client)

    jev_worker.handle(risk(rid="S1|C1|P0042|jev"))
    default_worker.handle(risk(rid="S1|C1|P0042|rule"))

    assert set(jev.calls[0][2]) == {candidate.id for candidate in candidates} - {"notify_me"}  # no restock date
    assert only(jev_out)["product_id"] == default.product_id == only(default_out)["product_id"]


def test_bedrock_accepted_text(catalogue, redis_client):
    t = FakeText("A light, similar trail shoe is ready for your next run.")
    w, out, m = build(make_cfg(), catalogue, redis_client, None, t)
    w.handle(risk())
    o = only(out)
    assert (o["text_route"], o["text_reason"]) == ("BEDROCK", "accepted") and o["body"].startswith("A light")
    assert "10" not in t.calls[0]  # prompt carries no terms


@pytest.mark.parametrize("bad", ["Now only EUR 99 for you", "Get 25 percent more comfort", "Save 10% today!", "Two\nlines", "x" * 300,
                                 "A shoe with 3 layers", "Free shipping included"])
def test_bedrock_text_with_invented_terms_is_rejected(catalogue, redis_client, bad):
    w, out, m = build(make_cfg(), catalogue, redis_client, None, FakeText(bad))
    w.handle(risk())
    o = only(out)
    assert (o["text_route"], o["text_reason"]) == ("TEMPLATE", "invalid_text")
    assert o["body"] != bad and ("text", "TEMPLATE", "invalid_text") in m.calls


@pytest.mark.parametrize("exc,reason", [(TextTimeout("t"), "timeout"), (TextError("e"), "error")])
def test_bedrock_failure_uses_template(catalogue, redis_client, exc, reason):
    w, out, _ = build(make_cfg(), catalogue, redis_client, None, FakeText(exc=exc))
    w.handle(risk())
    assert (only(out)["text_route"], only(out)["text_reason"]) == ("TEMPLATE", reason)


def test_bedrock_disabled_is_template(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client)
    w.handle(risk())
    assert (only(out)["text_route"], only(out)["text_reason"]) == ("TEMPLATE", "disabled")


def test_text_may_repeat_numbers_from_product_facts():
    assert validate_text("Another EU 42 option.", allowed_numbers("Trailrunner", "EU 42")) is None


def test_offer_matches_avro_contract_and_storefront_fields(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client)
    w.handle(risk())
    schema = fastavro.parse_schema(json.loads((CONTRACT_AVRO / "offer.avsc").read_text()))
    fastavro.validation.validate(only(out), schema)
    from app_compat import STOREFRONT_FIELDS  # noqa: F401  (see tests/app_compat.py)
    assert STOREFRONT_FIELDS <= set(only(out))


def test_cart_at_risk_schema_has_contract_fields():
    s = json.loads((CONTRACT_AVRO / "cart_at_risk.avsc").read_text())
    names = [f["name"] for f in s["fields"]]
    assert "sellable_changed_at_ms" in names and "stock_revision" not in names and "store_id" not in names
    defaults = {f["name"]: f.get("default") for f in s["fields"]}
    assert {"cart_value_eur": 0.0, "returning_shopper": False, "item_count": 1}.items() <= defaults.items()
    fastavro.parse_schema(s)


def test_config_fails_loudly():
    with pytest.raises(SystemExit, match="KAFKA_BOOTSTRAP"):
        Config.from_env({})
    base = {"KAFKA_BOOTSTRAP": "k", "SR_URL": "s", "REDIS_URL": "r", "DD_AGENT_HOST": "a", "DD_ENV": "e", "DD_SERVICE": "s",
            "DD_VERSION": "v", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT", "STORE_HOSTS": "S01=store-s01,S02=store-s02"}
    with pytest.raises(SystemExit, match="STORE_HOSTS"):
        Config.from_env({k: v for k, v in base.items() if k != "STORE_HOSTS"})
    for bad in ("S01", "S01=", "X1=a", "S01=a,S01=b"):
        with pytest.raises(SystemExit, match="STORE_HOSTS"):
            Config.from_env({**base, "STORE_HOSTS": bad})
    assert Config.from_env(base).store_ids == ("S01", "S02")
    with pytest.raises(SystemExit, match="BEDROCK_MODEL_ID"):
        Config.from_env({**base, "BEDROCK_ENABLED": "true", "AWS_REGION": "eu-west-1"})
    with pytest.raises(SystemExit, match="OFFER_DISCOUNT_PCT"):
        Config.from_env({**base, "OFFER_DISCOUNT_PCT": "90"})
    with pytest.raises(SystemExit, match="KAFKA_API_KEY"):
        Config.from_env({k: v for k, v in base.items() if k != "KAFKA_SECURITY_PROTOCOL"})
    assert Config.from_env(base).jev_timeout_ms == 800 and Config.from_env(base).jev_min_confidence == 0.8


# --- live config from Redis hash demo:config (contracts section 12) ---------------------------------------------
def test_live_min_confidence_overrides_env(catalogue, redis_client):
    redis_client.hset("demo:config", "jev_min_confidence", "0.95")
    w, out, _ = build(make_cfg(), catalogue, redis_client, FakeJev(confidence=0.9))
    w.handle(risk())
    assert only(out)["decision_reason"] == "low_confidence"
    redis_client.hset("demo:config", "jev_min_confidence", "0.5")
    w.handle(risk(rid="S1|C1|P0042|2000"))
    assert out[-1][1]["decision_reason"] == "accepted"


def test_live_max_risk_age_overrides_env(catalogue, redis_client):
    w, out, _ = build(make_cfg(), catalogue, redis_client)
    old = risk(detected=NOW - 100)
    assert w.handle(old) == "published"
    redis_client.hset("demo:config", "max_risk_age_s", "10")
    assert w.handle(risk(rid="other", detected=NOW - 100)) == "stale"


def test_live_jev_timeout_reaches_the_client(catalogue, redis_client):
    jev = FakeJev()
    jev.timeout_ms = 800
    redis_client.hset("demo:config", "jev_timeout_ms", "2500")
    w, _, _ = build(make_cfg(), catalogue, redis_client, jev)
    w.handle(risk())
    assert jev.timeout_ms == 2500


def test_absent_or_out_of_range_live_values_use_defaults_and_log_once(catalogue, redis_client, caplog):
    redis_client.hset("demo:config", "jev_min_confidence", "7")  # outside 0..1
    w, out, _ = build(make_cfg(), catalogue, redis_client, FakeJev(confidence=0.85))
    w.handle(risk())
    w.handle(risk(rid="S1|C1|P0042|2000"))
    assert [o["decision_reason"] for _, o in out] == ["accepted", "accepted"]  # env default 0.8
    assert len([r for r in caplog.records if "live config" in r.message and r.ctx["param"] == "jev_min_confidence"]) == 1


def test_kill_switch_hash_field_is_not_the_switch(catalogue, redis_client):
    # demo-control mirrors offers_kill_switch to offers:kill_switch; the worker keeps reading that key only.
    redis_client.set("offers:kill_switch", "1")
    jev = FakeJev()
    w, out, _ = build(make_cfg(), catalogue, redis_client, jev)
    w.handle(risk())
    assert jev.calls == [] and only(out)["decision_reason"] == "kill_switch"
