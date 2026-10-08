"""UrbanStreet storefront backend. See overlay/contracts/README.md (§5 API, §6 ingress, §7 telemetry)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable

from werkzeug.exceptions import HTTPException, NotFound
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from .alternatives import MAX_ALTERNATIVES, comparable_pool, display
from .beacon import BeaconRejected, display_delay_seconds
from .catalogue import get_product, load_products, model_variants, product_image_path
from .cart import CART_ID_RE, ValidationError, apply_cart_event, build_cart_event, cart_key, read_cart
from .config import Config
from .illustrations import render_svg
from .offers import OfferConsumer, OfferStore, offer_to_json

log = logging.getLogger(__name__)

SCENARIO_KEY = "scenario:current"
DEFAULT_TIME_COMPRESSION = 60  # used (logged once) when demo:config has no valid time_compression
CONFIG_KEY = "demo:config"  # hash written by demo-control (contracts section 12)


@dataclass
class Deps:
    """Everything with I/O, injectable so tests can use fakes."""
    publisher: Any            # .publish(key: dict, value: dict)
    redis: Any                # .get(key) / .ping()
    statsd: Any               # .distribution(name, value, tags=) / .increment(name, tags=)
    offer_consumer: Any | None  # OfferConsumer or None when offers are disabled
    offer_store: OfferStore
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # backend receive time for the beacon


def _error(status: int, code: str, message: str):
    return jsonify({"error": code, "message": message}), status


def _cart_json(cart_id: str, scenario_id: str, quantities: dict[str, int]) -> dict:
    """Cart contents for the shop's cart drawer, with display facts from the catalogue."""
    items = []
    for product_id, quantity in quantities.items():
        product = get_product(product_id) or {}
        items.append({"product_id": product_id, "quantity": quantity, "name": product.get("name"),
                      "brand": product.get("brand"), "size": product.get("size"),
                      "colour": (product.get("colour") or {}).get("name"), "price_eur": product.get("price_eur")})
    return {"cart_id": cart_id, "scenario_id": scenario_id, "items": items,
            "count": sum(quantities.values())}


def _jev_choice_label(choice: str | None, original_product_id: str) -> str | None:
    """Jev's choice as the shopper reads it: the product name ("Pathfinder Air"), plus its colour when it is the
    same model as the sold-out item ("Dolomia Evo in Glacier blue"); "no substitute" for Jev's "none", "a restock
    notice" in older records. None when Jev returned no choice or the id is not in the catalogue (the card then shows
    the id)."""
    if choice == "none":
        return "no substitute"
    if choice == "notify_me":
        return "a restock notice"
    if not choice or not choice.startswith("alt:"):
        return None
    product = get_product(choice[4:])
    if not product:
        return None
    original = get_product(original_product_id) or {}
    if product.get("name") == original.get("name"):
        return f"{product['name']} in {(product.get('colour') or {}).get('name')}"
    return product["name"]


def _offer_json(offer: dict) -> dict:
    """Offer as served, plus the offered alternative's display facts from the catalogue (null without one),
    Jev's choice by name, `restock_notice` and `no_offer`. The Offer has two independent parts: the alternative
    (`product_id`) and the Restock notice (`restock_eta`; an older record marks it with chosen_choice "notify_me").
    Neither: no Offer (offer-worker README, "Card states")."""
    body = offer_to_json(offer)
    alt = get_product(body["product_id"]) if body.get("product_id") else None
    body["alternative"] = display(alt) if alt else None
    body["jev_choice_label"] = _jev_choice_label(body.get("jev_choice"), body["original_product_id"])
    body["restock_notice"] = body["restock_eta"] is not None or body.get("chosen_choice") == "notify_me"
    body["no_offer"] = body.get("product_id") is None and not body["restock_notice"]
    return body


def create_app(cfg: Config, deps: Deps) -> Flask:
    app = Flask(__name__, static_folder=None)

    def current_scenario() -> str:
        scenario = deps.redis.get(SCENARIO_KEY)
        if not scenario:
            raise LookupError(f"Redis key {SCENARIO_KEY} is absent; run the scenario reset before using the storefront")
        return scenario

    @app.errorhandler(Exception)
    def unhandled(exc):  # JSON for every failure, with where/what/why
        if isinstance(exc, HTTPException) and (exc.code or 500) < 500:
            return _error(exc.code, "http_error", exc.description)
        log.exception("unhandled error in %s %s", request.method, request.path)
        return _error(500, "internal_error", f"{request.method} {request.path} failed: {type(exc).__name__}: {exc}")

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readyz():
        problems = {}
        try:
            deps.redis.ping()
        except Exception as exc:  # noqa: BLE001 - reported in the response body
            problems["redis"] = f"{type(exc).__name__}: {exc}"
        body: dict = {}
        if deps.offer_consumer is not None:
            st = deps.offer_consumer.status()
            body["offers_consumer"] = st
            if st["state"] != "running":
                problems["offers_consumer"] = st["error"]
        else:
            body["offers_consumer"] = {"state": "disabled"}
        body["status"] = "not_ready" if problems else "ready"
        body["problems"] = problems
        return jsonify(body), (503 if problems else 200)

    warned: set[str] = set()

    def live_int(name: str, default: int, lo: int, hi: int) -> int:
        """Live value from demo:config; the env default (logged once per name) when absent, unreadable or out of range."""
        try:
            raw = deps.redis.hget(CONFIG_KEY, name)
            if raw is None:
                why = "field absent"
            else:
                value = int(float(raw.decode() if isinstance(raw, bytes) else raw))
                if lo <= value <= hi:
                    warned.discard(name)
                    return value
                why = f"{value} outside {lo}..{hi}"
        except Exception as exc:  # noqa: BLE001 - /config must keep answering; logged once
            why = f"unreadable: {type(exc).__name__}: {exc}"
        if name not in warned:
            warned.add(name)
            log.warning("live config %s unavailable (%s); using default %s", name, why, default)
        return default

    @app.get("/config")
    def config():
        # Offer rendering shares this poll. Keeping its live ceiling at 2s is
        # part of the configured sell-out-to-rendered-offer budget.
        poll_ms = live_int("poll_ms", cfg.poll_ms, 250, 2_000)
        budget_ms = live_int("exposure_budget_ms", cfg.exposure_budget_ms, 50, 10_000)
        # Demo clock (contracts 13b): business duration = real duration x C. The shop converts ETAs with it.
        compression = live_int("time_compression", DEFAULT_TIME_COMPRESSION, 1, 3600)
        body = {"poll_ms": poll_ms, "offers_enabled": cfg.offers_enabled, "release": cfg.dd_version,
                "time_compression": compression}
        if cfg.rum_application_id and cfg.rum_client_token:
            # The client token is public by design (it ships to every browser); it is not an API key.
            body["rum"] = {"application_id": cfg.rum_application_id, "client_token": cfg.rum_client_token,
                           "site": cfg.dd_site, "service": "storefront-web", "env": cfg.dd_env,
                           "version": cfg.dd_version, "stack": cfg.stack,
                           "exposure_budget_ms": budget_ms}
        return jsonify(body)

    @app.post("/api/cart")
    def cart():
        try:
            scenario = current_scenario()
        except LookupError as exc:
            return _error(503, "scenario_missing", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("redis read of %s failed", SCENARIO_KEY)
            return _error(503, "redis_error", f"cannot read {SCENARIO_KEY}: {type(exc).__name__}: {exc}")
        try:
            key, value = build_cart_event(request.get_json(silent=True), scenario)
        except ValidationError as exc:
            return _error(400, "invalid_request", str(exc))
        try:
            deps.publisher.publish(key, value)
        except Exception as exc:  # noqa: BLE001
            log.exception("cart event publish failed", extra={"cart_id": value["cart_id"]})
            return _error(502, "publish_failed", f"could not publish cart event: {type(exc).__name__}: {exc}")
        log.info("cart event published", extra={k: value[k] for k in ("event_id", "cart_id", "event_type", "store_id", "product_id", "scenario_id")})
        try:
            quantities = apply_cart_event(deps.redis, value)
        except Exception as exc:  # noqa: BLE001
            key = cart_key(scenario, value["cart_id"])
            log.exception("cart contents update failed after publish", extra={"cart_id": value["cart_id"]})
            return _error(503, "redis_error", f"cart event {value['event_id']} was published but the cart contents "
                          f"in Redis key {key} were not updated: {type(exc).__name__}: {exc}")
        return jsonify({**_cart_json(value["cart_id"], scenario, quantities), "event_id": value["event_id"],
                        "event_type": value["event_type"]}), 201

    @app.get("/api/cart/<cart_id>")
    def cart_contents(cart_id: str):
        if not CART_ID_RE.match(cart_id):
            return _error(400, "invalid_request", "cart_id must be a value previously returned by this API")
        try:
            scenario = current_scenario()
        except LookupError as exc:
            return _error(503, "scenario_missing", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("redis read of %s failed", SCENARIO_KEY)
            return _error(503, "redis_error", f"cannot read {SCENARIO_KEY}: {type(exc).__name__}: {exc}")
        try:
            quantities = read_cart(deps.redis, scenario, cart_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("cart contents read failed", extra={"cart_id": cart_id})
            return _error(503, "redis_error", f"cannot read {cart_key(scenario, cart_id)}: {type(exc).__name__}: {exc}")
        if not quantities:
            # Expired, emptied, or started before the current scenario (a reset): the shop starts a new cart.
            return _error(404, "cart_not_found", f"no cart {cart_id} in scenario {scenario}")
        return jsonify(_cart_json(cart_id, scenario, quantities))

    @app.get("/api/offers")
    def offers():
        if not cfg.offers_enabled:
            return _error(404, "offers_disabled", "the offer branch is disabled (OFFERS_ENABLED=false)")
        cart_id = request.args.get("cart_id", "")
        if not cart_id:
            return _error(400, "invalid_request", "cart_id query parameter is required")
        try:
            scenario = current_scenario()
        except LookupError as exc:
            return _error(503, "scenario_missing", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("redis read of %s failed", SCENARIO_KEY)
            return _error(503, "redis_error", f"cannot read {SCENARIO_KEY}: {type(exc).__name__}: {exc}")
        # `offers`: one per sold-out cart item (oldest first). `offer`: the most recent one, kept for older readers.
        offers = [_offer_json(o) for o in deps.offer_store.get_all(scenario, cart_id)]
        latest = deps.offer_store.get(scenario, cart_id)
        return jsonify({"offer": _offer_json(latest) if latest else None, "offers": offers})

    @app.post("/api/beacon/display")
    def beacon():
        try:
            delay = display_delay_seconds(request.get_json(silent=True), cfg.max_display_delay_s, deps.clock())
        except BeaconRejected as exc:
            deps.statsd.increment("stock.display.beacon_rejected", tags=[f"reason:{exc.reason}"])
            log.warning("display beacon rejected", extra={"reason": exc.reason, "detail": str(exc)})
            return _error(422, "beacon_rejected", f"{exc.reason}: {exc}")
        deps.statsd.distribution("stock.display.delay", delay)
        return "", 204

    @app.get("/api/products")
    def products():
        return jsonify(load_products())

    @app.get("/api/products/<product_id>")
    def product_detail(product_id: str):
        product = get_product(product_id)
        if product is None:
            return _error(404, "not_found", f"unknown product {product_id}")
        return jsonify({**product, "variants": model_variants(product)})

    @app.get("/api/products/<product_id>/alternatives")
    def product_alternatives(product_id: str):
        """Comparable products in the offer-worker's order, without stock: the shop keeps the first
        `max_alternatives` that the availability API confirms in stock (the worker's rule)."""
        product = get_product(product_id)
        if product is None:
            return _error(404, "not_found", f"unknown product {product_id}")
        return jsonify({"product_id": product_id, "max_alternatives": MAX_ALTERNATIVES,
                        "candidates": [display(p) for p in comparable_pool(product, load_products())]})

    @app.get("/img/<product_id>.svg")
    def product_image(product_id: str):
        product = get_product(product_id) if re.match(r"^P\d{4}$", product_id) else None
        if product is None:
            return _error(404, "not_found", "unknown product image")
        return Response(render_svg(product), mimetype="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/img/<product_id>.jpg")
    def product_photo(product_id: str):
        image_path = product_image_path(product_id) if re.match(r"^P\d{4}$", product_id) else None
        if image_path is None:
            return _error(404, "not_found", "unknown product image or no approved photo mapping")
        response = send_file(image_path, mimetype="image/jpeg")
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def static_files(path: str):
        if path.startswith("api/"):
            return _error(404, "not_found", f"no such endpoint: /{path}")
        full = path or "index.html"
        try:
            return send_from_directory(cfg.static_dir, full)
        except NotFound:
            if path and "." not in path.rsplit("/", 1)[-1]:
                return send_from_directory(cfg.static_dir, "index.html")
            return _error(404, "not_found", f"static file {full} not found in {cfg.static_dir}")

    return app


def build_deps(cfg: Config) -> Deps:
    """Wire the real adapters. Starts the offers consumer thread when offers are enabled."""
    from . import kafka_io

    sr = kafka_io.schema_registry(cfg)
    publisher = kafka_io.CartPublisher(cfg, sr)
    redis_client = kafka_io.make_redis(cfg)
    statsd = kafka_io.make_statsd(cfg)
    store = OfferStore()
    consumer = None
    if cfg.offers_enabled:
        consumer = OfferConsumer(
            kafka_io.offers_consumer(cfg, sr), store,
            on_record=lambda: statsd.increment("storefront.offers.received"),
            partition_eof=kafka_io.PARTITION_EOF,
        )
        consumer.start()
    return Deps(publisher=publisher, redis=redis_client, statsd=statsd, offer_consumer=consumer, offer_store=store)
