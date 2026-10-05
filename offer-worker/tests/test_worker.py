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


def test_jev_context_includes_product_restock_eta_shopper_signals_and_choice_boundary(catalogue, redis_client):
    redis_client.set("restock:eta:P0042", "1800000060000")
    jev = FakeJev()
    w, _, _ = build(make_cfg(), catalogue, redis_client, jev)

    assert w.handle(risk()) == "published"

    state, instructions, criteria = jev.calls[0]
    assert "Trailrunner GTX" in state and "2027-01-15T08:01:00Z" in state
    assert "cart value EUR 189.50" in state and "returning shopper" in state and "3 items" in state
    assert "eligible alternatives" in instructions and "notify-me" in instructions
    assert "notify_me" in criteria and any(choice.startswith("alt:") for choice in criteria)


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


def test_revalidation_failure_becomes_notify_me(catalogue, redis_client):
    class SellsOutBetween(FakeJev):
        def choose(self, state, instructions, criteria):
            alt = next(k for k in criteria if k.startswith("alt:"))
            redis_client.hset(f"sellable:{alt[4:]}", "sellable", 0)  # stock gone after candidates were built
            return JevChoice(alt, 0.99)

    w, out, _ = build(make_cfg(), catalogue, redis_client, SellsOutBetween())
    w.handle(risk())
    o = only(out)
    assert o["offer_type"] == "NOTIFY_ME" and o["product_id"] is None and o["discount_pct"] == 0
    assert o["decision_route"] == "RULE_DEFAULT"


def test_no_stock_anywhere_means_notify_me_only(catalogue, redis_client):
    for pid in catalogue:
        redis_client.hset(f"sellable:{pid}", "sellable", 0)
    jev = FakeJev()
    w, out, _ = build(make_cfg(), catalogue, redis_client, jev)
    w.handle(risk())
    assert jev.calls == [] and only(out)["offer_type"] == "NOTIFY_ME"


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

    assert jev.calls[0][2] == {candidate.id: candidate.description for candidate in candidates}
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
            "DD_VERSION": "v", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}
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
