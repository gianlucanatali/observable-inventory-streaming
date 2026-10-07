"""Offer decision pipeline for one deduplicated cart-at-risk event. No Kafka/Jev/Bedrock imports: all injected."""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from ddtrace import tracer

from .bedrock import TextError, TextWriter, allowed_numbers, validate_text
from .config import MAX_DISCOUNT_PCT, Config
from .jev import JevClient, JevError
from .live import LiveConfig
from .metrics import Metrics
from .policy import Candidate, build_candidates, rule_default, template_text
from .stock import ConfirmedStock, MalformedStock

log = logging.getLogger("offer_worker")


def _jev_choice_for_log(choice: str) -> str:
    """Keep Jev diagnostics to bounded choice identifiers, never raw response content."""
    if choice == "notify_me" or re.fullmatch(r"alt:P[0-9]{1,16}", choice):
        return choice
    return "unrecognized"


class LruSet:
    """Bounded set of seen ids (oldest evicted first)."""

    def __init__(self, size: int):
        self._d: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def add_if_new(self, key: str) -> bool:
        if key in self._d:
            self._d.move_to_end(key)
            return False
        self._d[key] = None
        while len(self._d) > self._size:
            self._d.popitem(last=False)
        return True

    def discard(self, key: str) -> None:
        self._d.pop(key, None)


def _ms(v: Any) -> int:
    return int(v.timestamp() * 1000) if isinstance(v, datetime) else int(v)


class OfferWorker:
    def __init__(self, cfg: Config, redis_client, catalogue: dict[str, dict], publish: Callable[[dict, dict], None],
                 metrics: Metrics, jev: JevClient | None, text_writer: TextWriter | None,
                 clock: Callable[[], float] = time.time):
        self._cfg, self._r, self._cat, self._publish = cfg, redis_client, catalogue, publish
        self._m, self._jev, self._text, self._clock = metrics, jev, text_writer, clock
        self._seen = LruSet(cfg.dedup_size)
        self._live = LiveConfig(redis_client)
        self._stock = ConfirmedStock(redis_client, cfg.store_ids, clock)

    # --- helpers -------------------------------------------------------------------------------------------------
    def _confirmed_min(self, product_id: str) -> int | None:
        """Confirmed minimum with the API's rule: only live stores count, so a quiet store's last known
        stock never makes an alternative eligible. None when it cannot be computed (namespace not ready, Flink total
        missing, Redis error, malformed hash): logged and counted, never treated as available."""
        ctx = {"product_id": product_id}
        try:
            a = self._stock.read(product_id)
        except MalformedStock as e:
            log.error("stock hash malformed; alternative not eligible", extra={"ctx": {**ctx, "error": str(e)}})
            self._m.stock_unconfirmed("malformed")
            return None
        except Exception as e:  # noqa: BLE001 - logged with traceback and counted; the caller treats None as not eligible
            log.exception("stock lookup failed; alternative not eligible",
                          extra={"ctx": {**ctx, "error": f"{type(e).__name__}: {e}"}})
            self._m.stock_unconfirmed("redis_error")
            return None
        if a.unknown_reason is not None:
            level = logging.WARNING if a.unknown_reason == "not_ready" else logging.INFO
            log.log(level, "stock not confirmed; alternative not eligible",
                    extra={"ctx": {**ctx, "reason": a.unknown_reason, "sellable": a.sellable,
                                   "confirmed_min": a.confirmed_min}})
            self._m.stock_unconfirmed(a.unknown_reason)
        return a.confirmed_min

    def _kill_switch(self) -> bool:
        if self._cfg.kill_switch:
            return True  # env override; the demo-control switch is the Redis key below
        try:
            return self._r.get(self._cfg.kill_switch_key) not in (None, b"", b"0", "", "0")
        except Exception:  # noqa: BLE001 - fail safe: engaged means rule default, no external call
            log.exception("kill switch key unreadable; treating the switch as engaged")
            return True

    def _is_current_scenario(self, scenario_id: str) -> bool:
        """Fail closed when reset state cannot prove this risk belongs to the current scenario."""
        try:
            current = self._r.get("scenario:current")
        except Exception:  # noqa: BLE001 - do not make an external call for an unverified scenario
            log.exception("current scenario unreadable; expiring risk", extra={"ctx": {"scenario_id": scenario_id}})
            return False
        if isinstance(current, bytes):
            current = current.decode()
        return current == scenario_id

    def _restock_eta(self, product_id: str) -> str:
        """Return the advisory restock ETA for Jev context without changing the offer's safe policy path."""
        try:
            raw = self._r.get(f"restock:eta:{product_id}")
        except Exception:  # noqa: BLE001 - the unavailable advisory is recorded, offer policy continues safely
            log.exception("restock ETA lookup failed", extra={"ctx": {"product_id": product_id}})
            return "unknown (restock ETA unavailable)"
        if raw is None:
            return "unknown (no open purchase order)"
        try:
            eta_ms = int(raw)
            return datetime.fromtimestamp(eta_ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError, OSError) as e:
            log.error("malformed restock ETA; excluding it from Jev context",
                      extra={"ctx": {"product_id": product_id, "value": raw, "error": f"{type(e).__name__}: {e}"}})
            return "unknown (invalid restock ETA)"

    @staticmethod
    def _shopper_signals(risk: dict) -> tuple[float, bool, int]:
        """Validate synthetic signals; old compatible records use their Avro defaults."""
        value = risk.get("cart_value_eur", 0.0)
        returning = risk.get("returning_shopper", False)
        count = risk.get("item_count", 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"carts.at-risk cart_value_eur must be a non-negative number, got {value!r}")
        if not isinstance(returning, bool):
            raise ValueError(f"carts.at-risk returning_shopper must be boolean, got {returning!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"carts.at-risk item_count must be an integer >= 1, got {count!r}")
        return float(value), returning, count

    # --- decision lane -------------------------------------------------------------------------------------------
    def _decide(self, risk: dict, original: dict, cands: list[Candidate], min_confidence: float) -> tuple[Candidate, str, str, float | None, str | None]:
        default = rule_default(cands)
        if self._jev is None:
            return default, "RULE_DEFAULT", "disabled", None, None          # no JEV_API_KEY
        if self._kill_switch():
            return default, "RULE_DEFAULT", "kill_switch", None, None
        if len(cands) < 2:
            return default, "RULE_DEFAULT", "no_choice", None, None          # nothing to choose between
        cart_value, returning, item_count = self._shopper_signals(risk)
        state = (f"A shopper's cart holds a product that just sold out everywhere: {original['name']} by {original['brand']}, "
                 f"{original['category']}, size {original['size']}, {original['colour']['name']}, EUR {original['price_eur']:.2f}. "
                 f"Restock ETA: {self._restock_eta(risk['product_id'])}. Synthetic shopper signals: cart value EUR {cart_value:.2f}, "
                 f"{'returning shopper' if returning else 'new shopper'}, {item_count} items.")
        if hasattr(self._jev, "timeout_ms"):  # live Jev timeout (demo:config)
            self._jev.timeout_ms = int(self._live.get("jev_timeout_ms", self._cfg.jev_timeout_ms, 100, 10_000))
        try:
            got = self._jev.choose(state,
                                   "Choose only among the supplied eligible alternatives and notify-me. Eligible alternatives are in stock; "
                                   "notify-me is the option when waiting for the stated restock ETA is a better fit.",
                                   {c.id: c.description for c in cands})
        except JevError as e:
            log.warning("Jev unavailable; rule default", extra={"ctx": {"reason": e.reason, "error": str(e)}})
            return default, "RULE_DEFAULT", e.reason, None, None
        jev_choice = _jev_choice_for_log(got.choice)
        by_id = {c.id: c for c in cands}
        if got.choice not in by_id:
            log.warning("Jev chose an id that is not allowed; rule default", extra={"ctx": {"choice": jev_choice}})
            return default, "RULE_DEFAULT", "invalid_choice", got.confidence, jev_choice
        if got.confidence < min_confidence:
            return default, "RULE_DEFAULT", "low_confidence", got.confidence, jev_choice
        return by_id[got.choice], "JEV", "accepted", got.confidence, jev_choice

    # --- text lane -----------------------------------------------------------------------------------------------
    def _write_text(self, original: dict, alt: dict | None, offer_type: str) -> tuple[str, str, str, str]:
        headline, body = template_text(offer_type, original, alt, self._cfg.discount_pct if alt else 0)
        if self._text is None:
            return headline, body, "TEMPLATE", "disabled"
        facts = f"Original product (sold out): {original['name']} by {original['brand']}, {original['category']}. "
        facts += (f"Replacement offered: {alt['name']} by {alt['brand']}, {alt['category']}, {alt['colour']['name']}."
                  if alt else "No replacement; the shopper will be notified when the original is back.")
        allowed = allowed_numbers(original["name"], original["size"], *( [alt["name"], alt["size"]] if alt else []))
        try:
            text = self._text.write(facts)
        except TextError as e:
            log.warning("Bedrock text failed; template", extra={"ctx": {"reason": e.reason, "error": str(e)}})
            return headline, body, "TEMPLATE", e.reason
        cause = validate_text(text, allowed)
        if cause:
            log.warning("Bedrock text rejected; template", extra={"ctx": {"cause": cause, "text": text}})
            return headline, body, "TEMPLATE", "invalid_text"
        return headline, text.strip(), "BEDROCK", "accepted"

    # --- main ----------------------------------------------------------------------------------------------------
    def handle(self, risk: dict | None) -> str:
        """Returns an outcome label: published | tombstone | duplicate | stale | expired | unknown_product."""
        if risk is None:
            log.info("risk ended (tombstone); nothing to do")
            return "tombstone"
        rid = risk["risk_id"]
        ctx = {"risk_id": rid, "cart_id": risk["cart_id"], "scenario_id": risk["scenario_id"], "product_id": risk["product_id"]}
        with tracer.trace("offer.process", service=self._cfg.dd_service, resource="carts.at-risk") as span:
            span.set_tag("risk_id", rid)
            span.set_tag("cart_id", risk["cart_id"])
            age_s = self._clock() - _ms(risk["detected_at"]) / 1000.0
            if age_s > self._live.get("max_risk_age_s", self._cfg.max_risk_age_s, 10, 3600):
                log.info("stale risk dropped", extra={"ctx": {**ctx, "age_s": round(age_s, 1)}})
                return "stale"
            if not self._is_current_scenario(risk["scenario_id"]):
                log.info("prior scenario risk expired", extra={"ctx": ctx})
                return "expired"
            if not self._seen.add_if_new(rid):
                log.info("duplicate risk skipped", extra={"ctx": ctx})
                return "duplicate"
            original = self._cat.get(risk["product_id"])
            if original is None:
                self._seen.discard(rid)
                log.error("product is not in the catalogue; no offer", extra={"ctx": ctx})
                return "unknown_product"

            cands = build_candidates(original, self._cat, self._confirmed_min, self._cfg.discount_pct)
            min_confidence = self._live.get("jev_min_confidence", self._cfg.jev_min_confidence, 0.0, 1.0)
            rule_choice = rule_default(cands).id
            chosen, route, reason, jev_confidence, jev_choice = self._decide(risk, original, cands, min_confidence)
            # Revalidate right before publishing, with the same confirmed-minimum rule: stock may have been taken, or its
            # only store may have gone quiet, since the candidates were built. Jev and the rule default both pass here.
            if chosen.product_id is not None and not ((self._confirmed_min(chosen.product_id) or 0) > 0):
                log.warning("alternative no longer available; notify-me", extra={"ctx": {**ctx, "alt": chosen.product_id}})
                chosen = cands[-1]
                route, reason = "RULE_DEFAULT", "invalid_choice"
            self._m.decision(route, reason)
            assert chosen.discount_pct <= MAX_DISCOUNT_PCT  # policy bound, cannot come from Jev/Bedrock

            alt = self._cat[chosen.product_id] if chosen.product_id else None
            headline, body, text_route, text_reason = self._write_text(original, alt, chosen.offer_type)
            self._m.text(text_route, text_reason)

            offer = {
                "offer_id": f"offer|{rid}", "risk_id": rid, "scenario_id": risk["scenario_id"], "cart_id": risk["cart_id"],
                "original_store_id": "ONLINE", "original_product_id": risk["product_id"],
                "offer_type": chosen.offer_type, "store_id": None, "product_id": chosen.product_id,
                "discount_pct": chosen.discount_pct, "headline": headline, "body": body,
                "decision_route": route, "decision_reason": reason, "text_route": text_route, "text_reason": text_reason,
                "jev_choice": jev_choice, "jev_confidence": jev_confidence, "min_confidence": min_confidence,
                "rule_choice": rule_choice, "chosen_choice": chosen.id,
                "created_at": int(self._clock() * 1000),
            }
            self._publish({"offer_id": offer["offer_id"]}, offer)
            self._m.completed(chosen.offer_type)
            span.set_tag("offer.type", chosen.offer_type)
            with tracer.trace("offer.decision", service=self._cfg.dd_service, resource="offer") as decision_span:
                decision_span.set_tag("offer.route", route)
                decision_span.set_tag("offer.reason", reason)
                decision_span.set_tag("jev.choice", jev_choice or "not_returned")
                decision_span.set_tag("jev.confidence", jev_confidence if jev_confidence is not None else "not_returned")
                decision_span.set_tag("offer.min_confidence", min_confidence)
                decision_span.set_tag("rule.choice", rule_choice)
                decision_span.set_tag("offer.chosen", chosen.id)
            log_ctx = {**ctx, "offer_id": offer["offer_id"], "offer_type": chosen.offer_type,
                       "decision": f"{route}/{reason}", "text": f"{text_route}/{text_reason}"}
            if jev_confidence is not None:
                log_ctx["jev_confidence"] = min(1.0, max(0.0, jev_confidence))
                log_ctx["jev_choice"] = jev_choice
            log.info("offer published", extra={"ctx": log_ctx})
            return "published"
