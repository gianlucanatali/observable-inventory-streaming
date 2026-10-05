"""UrbanStreet storefront backend. See overlay/contracts/README.md (§5 API, §6 ingress, §7 telemetry)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable

from werkzeug.exceptions import HTTPException, NotFound
from flask import Flask, Response, jsonify, request, send_from_directory

from .beacon import BeaconRejected, display_delay_seconds
from .catalogue import get_product, load_products
from .cart import ValidationError, build_cart_event
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
        return jsonify({"cart_id": value["cart_id"], "event_id": value["event_id"],
                        "event_type": value["event_type"], "scenario_id": scenario}), 201

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
        offer = deps.offer_store.get(scenario, cart_id)
        return jsonify({"offer": offer_to_json(offer) if offer else None})

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
        return jsonify(product)

    @app.get("/img/<product_id>.svg")
    def product_image(product_id: str):
        product = get_product(product_id) if re.match(r"^P\d{4}$", product_id) else None
        if product is None:
            return _error(404, "not_found", "unknown product image")
        return Response(render_svg(product), mimetype="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

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
