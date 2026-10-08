"""Offer decision pipeline for one deduplicated cart-at-risk event. No Kafka/Jev/Bedrock imports: all injected."""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable

from ddtrace import tracer

from .bedrock import TextError, TextWriter, allowed_numbers, validate_text
from .config import MAX_DISCOUNT_PCT, Config
from .jev import JevClient, JevError
from .live import LiveConfig
from .metrics import Metrics
from . import prompt
from .policy import NONE_ID, Candidate, Restock, build_candidates, restock_notice_eligible, rule_default, template_text
from .prompt import Cart
from .stock import ConfirmedStock, MalformedStock

log = logging.getLogger("offer_worker")
MAX_PLAUSIBLE_DELAY_S = 600  # a larger Offer delay is clock skew or a bad timestamp, not a measurement
DEFAULT_TIME_COMPRESSION = 60  # contracts/demo-params.json time_compression default


def _jev_choice_for_log(choice: str) -> str:
    """Keep Jev diagnostics to bounded choice identifiers, never raw response content."""
    if choice == NONE_ID or re.fullmatch(r"alt:P[0-9]{1,16}", choice):
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

    def _restock(self, product_id: str) -> Restock:
        """Restock date as a business-time wait (demo clock `time_compression`). It decides whether the Offer
        includes the Restock notice (policy.restock_notice_eligible); it is never part of the AI question."""
        try:
            raw = self._r.get(f"restock:eta:{product_id}")
        except Exception:  # noqa: BLE001 - the unavailable advisory is recorded, offer policy continues safely
            log.exception("restock ETA lookup failed", extra={"ctx": {"product_id": product_id}})
            return Restock(None, "restock ETA unavailable")
        if raw is None:
            return Restock(None, "no open purchase order")
        try:
            eta_ms = int(raw)
        except (TypeError, ValueError) as e:
            log.error("malformed restock ETA; no Restock notice",
                      extra={"ctx": {"product_id": product_id, "value": raw, "error": f"{type(e).__name__}: {e}"}})
            return Restock(None, "invalid restock ETA")
        compression = self._live.get("time_compression", DEFAULT_TIME_COMPRESSION, 1, 3600)
        return Restock((eta_ms / 1000.0 - self._clock()) * compression, eta_ms)

    def _cart(self, risk: dict, original: dict) -> Cart:
        """Cart totals from the shop's cart contents (contracts section 4, `cart:{scenario_id}:{cart_id}`) priced with
        the catalogue. When they cannot be read (jr carts have none, expired, Redis error, unknown product) the totals
        are lower bounds: the sold-out item alone. Never the random synthetic value, which ignores the contents."""
        floor = Cart(float(original["price_eur"]), 1, False)
        key = f"cart:{risk['scenario_id']}:{risk['cart_id']}"
        try:
            raw = self._r.hgetall(key)
        except Exception:  # noqa: BLE001 - context only; logged with traceback, lower bounds used
            log.exception("cart contents unreadable; Jev gets lower bounds", extra={"ctx": {"cart_id": risk["cart_id"]}})
            return floor
        lines = {(k.decode() if isinstance(k, bytes) else k): v for k, v in (raw or {}).items()}
        if risk["product_id"] not in lines:
            log.info("cart contents not found; Jev gets lower bounds",
                     extra={"ctx": {"cart_id": risk["cart_id"], "lines": len(lines)}})
            return floor
        try:
            qty = {pid: int(v) for pid, v in lines.items()}
            value = sum(self._cat[pid]["price_eur"] * n for pid, n in qty.items())
        except (KeyError, ValueError) as e:
            log.error("cart contents malformed; Jev gets lower bounds",
                      extra={"ctx": {"cart_id": risk["cart_id"], "error": f"{type(e).__name__}: {e}"}})
            return floor
        items = sum(qty.values())
        if items < 1:
            return floor
        return Cart(round(value, 2), items, True)

    @staticmethod
    def _returning_shopper(risk: dict) -> bool:
        """Synthetic signal from carts.events (stable per cart id); old compatible records use the Avro default."""
        returning = risk.get("returning_shopper", False)
        if not isinstance(returning, bool):
            raise ValueError(f"carts.at-risk returning_shopper must be boolean, got {returning!r}")
        return returning

    # --- decision lane (the alternative part only; the Restock notice is a fact, decided elsewhere) ---------------
    def _decide(self, risk: dict, original: dict, alts: list[Candidate],
                min_confidence: float) -> tuple[Candidate | None, str, str, float | None, str | None]:
        """(alternative or None, route, reason, jev confidence, jev choice). Jev answers which eligible alternative is a
        good substitute, or none. "none" stands at any confidence (JEV/no_good_substitute): the Safe rule must not offer
        an alternative the AI ranked below "none"; the low confidence stays visible as jev_confidence < min_confidence.
        A confident alternative stands (JEV/accepted); everything else is the Safe rule's pick (RULE_DEFAULT)."""
        default = rule_default(alts)
        if not alts:
            return None, "RULE_DEFAULT", "no_alternative", None, None  # nothing to judge: no Jev call
        if self._jev is None:
            return default, "RULE_DEFAULT", "disabled", None, None          # no JEV_API_KEY
        if self._kill_switch():
            return default, "RULE_DEFAULT", "kill_switch", None, None
        state, instructions, criteria = prompt.build(original, self._cat, alts, self._cart(risk, original),
                                                     self._returning_shopper(risk))
        if hasattr(self._jev, "timeout_ms"):  # live Jev timeout (demo:config)
            self._jev.timeout_ms = int(self._live.get("jev_timeout_ms", self._cfg.jev_timeout_ms, 100, 10_000))
        try:
            got = self._jev.choose(state, instructions, criteria)
        except JevError as e:
            log.warning("Jev unavailable; rule default", extra={"ctx": {"reason": e.reason, "error": str(e)}})
            return default, "RULE_DEFAULT", e.reason, None, None
        jev_choice = _jev_choice_for_log(got.choice)
        by_id = {c.id: c for c in alts}
        if got.choice != NONE_ID and got.choice not in by_id:
            log.warning("Jev chose an id that is not allowed; rule default", extra={"ctx": {"choice": jev_choice}})
            return default, "RULE_DEFAULT", "invalid_choice", got.confidence, jev_choice
        if got.choice == NONE_ID:
            return None, "JEV", "no_good_substitute", got.confidence, jev_choice
        if got.confidence < min_confidence:
            return default, "RULE_DEFAULT", "low_confidence", got.confidence, jev_choice
        return by_id[got.choice], "JEV", "accepted", got.confidence, jev_choice

    # --- text lane -----------------------------------------------------------------------------------------------
    def _write_text(self, original: dict, alt: dict | None, restock_wait: str | None,
                    no_good_substitute: bool = False) -> tuple[str, str, str, str]:
        headline, body = template_text(original, alt, self._cfg.discount_pct if alt else 0, restock_wait,
                                       no_good_substitute)
        if alt is None and restock_wait is None:
            return headline, body, "TEMPLATE", "no_offer"  # nothing to write about: the honest fixed wording
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

    def _emit_delay(self, risk: dict, received_at: float, published_at: float, route: str, offer_type: str) -> None:
        """Offer delay = publish time - sell-out detected_at; upstream = receive time - detected_at; worker = the rest.
        A missing, negative or absurd value (clock skew, bad timestamp) is logged with the risk_id and not emitted."""
        detected_s = _ms(risk["detected_at"]) / 1000.0 if risk.get("detected_at") else None
        total = published_at - detected_s if detected_s is not None else None
        if total is None or not (0 <= total <= MAX_PLAUSIBLE_DELAY_S):
            log.warning("offer delay not emitted: detected_at missing or delay implausible",
                        extra={"ctx": {"risk_id": risk["risk_id"], "detected_at": risk.get("detected_at"),
                                       "delay_s": None if total is None else round(total, 3)}})
            return
        tags = {"product_id": risk["product_id"], "decided_by": "ai" if route == "JEV" else "rule", "route": route,
                "offer_type": offer_type}
        self._m.delay(tags, total, received_at - detected_s, published_at - received_at)

    # --- main ----------------------------------------------------------------------------------------------------
    def handle(self, risk: dict | None) -> str:
        """Returns an outcome label: published | tombstone | duplicate | stale | expired | unknown_product."""
        if risk is None:
            log.info("risk ended (tombstone); nothing to do")
            return "tombstone"
        received_at = self._clock()
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

            restock = self._restock(risk["product_id"])
            near_days = self._live.get("notify_me_near_days", self._cfg.notify_me_near_days, 0, 60)
            # The Restock notice is a fact: included whenever an open purchase order is due within the near-restock
            # limit, whatever the AI says about alternatives.
            restock_included = restock_notice_eligible(restock.business_s, near_days)
            alts = build_candidates(original, self._cat, self._confirmed_min, self._cfg.discount_pct)
            min_confidence = self._live.get("jev_min_confidence", self._cfg.jev_min_confidence, 0.0, 1.0)
            rule = rule_default(alts)
            rule_choice = rule.id if rule else None
            chosen, route, reason, jev_confidence, jev_choice = self._decide(risk, original, alts, min_confidence)
            # Revalidate right before publishing, with the same confirmed-minimum rule: stock may have been taken, or its
            # only store may have gone quiet, since the candidates were built. Jev and the rule default both pass here.
            # The Safe rule then takes the next alternative still valid, else none.
            if chosen is not None and not ((self._confirmed_min(chosen.product_id) or 0) > 0):
                failed = chosen
                chosen = next((c for c in alts if c is not failed and (self._confirmed_min(c.product_id) or 0) > 0), None)
                log.warning("alternative no longer available; safe rule",
                            extra={"ctx": {**ctx, "alt": failed.product_id, "now": chosen.id if chosen else "none"}})
                route, reason = "RULE_DEFAULT", "invalid_choice"
            self._m.decision(route, reason)
            assert chosen is None or chosen.discount_pct <= MAX_DISCOUNT_PCT  # policy bound, cannot come from Jev/Bedrock

            alt = self._cat[chosen.product_id] if chosen else None
            restock_wait = prompt.business_duration(restock.business_s) if restock_included else None
            headline, body, text_route, text_reason = self._write_text(
                original, alt, restock_wait, no_good_substitute=(reason == "no_good_substitute"))
            self._m.text(text_route, text_reason)

            # offer_type stays required for older readers: ALTERNATIVE_PRODUCT with an alternative, else NOTIFY_ME
            # (also when there is no Offer at all). Readers decide on product_id (the alternative) and restock_eta
            # (the Restock notice).
            offer_type = "ALTERNATIVE_PRODUCT" if chosen else "NOTIFY_ME"
            offer = {
                "offer_id": f"offer|{rid}", "risk_id": rid, "scenario_id": risk["scenario_id"], "cart_id": risk["cart_id"],
                "original_store_id": "ONLINE", "original_product_id": risk["product_id"],
                "offer_type": offer_type, "store_id": None,
                "product_id": chosen.product_id if chosen else None,
                "discount_pct": chosen.discount_pct if chosen else 0, "headline": headline, "body": body,
                "decision_route": route, "decision_reason": reason, "text_route": text_route, "text_reason": text_reason,
                "jev_choice": jev_choice, "jev_confidence": jev_confidence, "min_confidence": min_confidence,
                "rule_choice": rule_choice, "chosen_choice": chosen.id if chosen else None,
                "restock_eta": restock.eta_ms if restock_included else None,
                "created_at": int(self._clock() * 1000),
            }
            self._publish({"offer_id": offer["offer_id"]}, offer)
            type_tag = ("ALTERNATIVE_PRODUCT" if chosen else "NOTIFY_ME") if (chosen or restock_included) else "NONE"
            self._m.completed(type_tag, restock_included)
            self._emit_delay(risk, received_at, self._clock(), route, type_tag)
            span.set_tag("offer.type", type_tag)
            span.set_tag("offer.restock_included", restock_included)
            with tracer.trace("offer.decision", service=self._cfg.dd_service, resource="offer") as decision_span:
                decision_span.set_tag("offer.route", route)
                decision_span.set_tag("offer.reason", reason)
                decision_span.set_tag("jev.choice", jev_choice or "not_returned")
                decision_span.set_tag("jev.confidence", jev_confidence if jev_confidence is not None else "not_returned")
                decision_span.set_tag("offer.min_confidence", min_confidence)
                decision_span.set_tag("rule.choice", rule_choice or "none")
                decision_span.set_tag("offer.chosen", chosen.id if chosen else "none")
                decision_span.set_tag("offer.restock_included", restock_included)
            log_ctx = {**ctx, "offer_id": offer["offer_id"], "offer_type": type_tag,
                       "decision": f"{route}/{reason}", "text": f"{text_route}/{text_reason}",
                       "restock_included": restock_included}
            if jev_confidence is not None:
                log_ctx["jev_confidence"] = min(1.0, max(0.0, jev_confidence))
                log_ctx["jev_choice"] = jev_choice
            log.info("offer published", extra={"ctx": log_ctx})
            return "published"
