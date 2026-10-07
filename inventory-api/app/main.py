"""Stock lookup API. Behaviour of product enrichment is selected by CATALOGUE_MODE."""
import logging
import re
import time

import redis
from datadog import DogStatsd
from ddtrace import tracer
from flask import Flask, jsonify

from app.catalogue import Catalogue
from app.config import Config

log = logging.getLogger("inventory-api")

STATUS_TAG_UNKNOWN = "none"
PRODUCT_RE = re.compile(r"^P\d{4}$")


def create_app(config=None, redis_client=None, statsd=None, catalogue=None):
    cfg = config or Config()
    app = Flask(__name__)
    app.json.sort_keys = False
    r = redis_client or redis.Redis.from_url(
        cfg.redis_url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0)
    sd = statsd or DogStatsd(host=cfg.dogstatsd_host, port=cfg.dogstatsd_port,
                             constant_tags=[f"env:{_e('DD_ENV')}", "service:inventory-api", f"version:{cfg.release}"])
    cat = catalogue or Catalogue(cfg.catalogue_path, cfg.mode)
    app.extensions["catalogue"] = cat
    app.extensions["statsd"] = sd
    app.config["RELEASE"] = cfg.release
    app.config["READY"] = cfg.mode != "startup"
    app.config["CATALOGUE_SHA256"] = None

    def prepare():
        """Startup preparation; called once per worker (gunicorn post_fork) in mode startup."""
        if cfg.mode == "startup":
            cat.prepare_startup()
            app.config["CATALOGUE_SHA256"] = cat.sha256
            app.config["READY"] = True
            log.info("catalogue prepared at startup products=%d sha256=%s", len(cat.index["by_id"]), cat.sha256)

    app.extensions["prepare"] = prepare

    def ms_to_iso(ms):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}Z"

    def store_feed(h, now_ms):
        """feed:status:{store} hash -> ok|stale|unknown (missing, malformed or older than feed_max_age_s = unknown)."""
        if not h:
            return "unknown"
        try:
            age_ms = now_ms - int(h["checked_at_ms"])
        except (KeyError, ValueError):
            log.error("feed:status:* malformed: %r", h)
            return "unknown"
        if age_ms > cfg.feed_max_age_s * 1000:
            return "unknown"
        return h["state"] if h.get("state") in ("ok", "stale", "unknown") else "unknown"

    def read_stock(product_id):
        """Redis reads inside the stock.read span. Returns (ns_ready, sellable_hash, positions, feeds, restock_eta_ms).
        Round trip 1 is a pipeline (active ns, sellable, every store feed); the namespaced keys cannot be named
        before the namespace is known, so round trip 2 is a pipeline (meta, every store position)."""
        with tracer.trace("stock.read") as span:
            span.set_tag("product_id", product_id)
            pipe = r.pipeline(transaction=False)
            pipe.get("stock:active_ns")
            pipe.hgetall(f"sellable:{product_id}")
            for s in cfg.stores:
                pipe.hgetall(f"feed:status:{s}")
            pipe.get(f"restock:eta:{product_id}")
            res = pipe.execute()
            ns, sellable, feeds, eta = res[0], res[1], res[2:-1], res[-1]
            if not ns:
                return False, sellable, {}, feeds, eta
            pipe = r.pipeline(transaction=False)
            pipe.hget(f"stock:{ns}:meta", "ready")
            for s in cfg.stores:
                pipe.hgetall(f"stock:{ns}:{s}:{product_id}")
            res = pipe.execute()
            return res[0] == "1", sellable, dict(zip(cfg.stores, res[1:])), feeds, eta

    def parse_eta(product_id, raw):
        """restock:eta:{product_id} = epoch ms string -> ISO UTC; absent -> None; malformed -> logged, None
        (the ETA is advisory; it never changes the availability status)."""
        if raw is None:
            return None
        try:
            return ms_to_iso(int(raw))
        except (ValueError, OverflowError, OSError) as e:
            log.error("malformed restock:eta product=%s: %r (%s)", product_id, raw, e)
            return None

    def parse_position(store_id, product_id, pos):
        try:
            return int(pos["quantity"]), int(pos["revision"]), pos.get("deleted", "0") == "1"
        except (KeyError, ValueError) as e:
            log.error("malformed position store=%s product=%s: %r (%s)", store_id, product_id, pos, e)
            raise  # a malformed serving view is an API failure: 500, loudly

    @app.get("/api/availability/<product_id>")
    def availability(product_id):
        if not PRODUCT_RE.match(product_id):
            return jsonify(error="not_found", message="product_id must look like P0042"), 404
        ready, sellable_h, positions, feeds, eta_raw = False, {}, {}, [{}] * len(cfg.stores), None
        reason = None
        try:
            ready, sellable_h, positions, feeds, eta_raw = read_stock(product_id)
        except redis.RedisError as e:
            reason = "redis_error"
            sellable_h, positions, feeds = {}, {}, [{}] * len(cfg.stores)
            log.error("redis failure on availability lookup product=%s: %s: %s",
                      product_id, type(e).__name__, e, exc_info=True)
            span = tracer.current_span()
            if span is not None:
                span.set_exc_info(type(e), e, e.__traceback__)

        now_ms = int(time.time() * 1000)
        # A store is live when the namespace is ready, its feed is ok and its position is present. Only live
        # stores count towards confirmed_min; a not-live store keeps its last seen quantity as detail.
        stores, any_unknown, live_sum = [], False, 0
        for sid, fh in zip(cfg.stores, feeds):
            feed = store_feed(fh, now_ms)
            entry = {"store_id": sid, "status": "unknown", "quantity": None, "revision": None, "feed": feed,
                     "live": False}
            pos = positions.get(sid)
            if pos:
                q, rev, deleted = parse_position(sid, product_id, pos)
                entry["revision"] = rev
                if deleted:
                    entry["status"] = "not_stocked"
                else:
                    entry["quantity"] = q
                    entry["status"] = "available" if q > 0 else "out_of_stock"
            entry["live"] = entry["status"] != "unknown" and feed == "ok" and ready
            if entry["live"]:
                live_sum += entry["quantity"] or 0
            else:
                any_unknown = True
            stores.append(entry)
        feed_order = {"ok": 0, "stale": 1, "unknown": 2}
        worst = max((e["feed"] for e in stores), key=feed_order.__getitem__)

        sellable, last_changed = None, None
        if reason is None and not ready:
            reason = "not_ready"
        elif reason is None and not sellable_h:
            reason = "not_found"
        elif reason is None:
            try:
                sellable = int(sellable_h["sellable"])
                last_changed = ms_to_iso(int(sellable_h["last_changed_at_ms"]))
            except (KeyError, ValueError) as e:
                log.error("malformed sellable product=%s: %r (%s)", product_id, sellable_h, e)
                raise  # a malformed serving view is an API failure: 500, loudly
        # confirmed_min is what the shop may promise: the Flink total when every store is live, otherwise the
        # live stores' sum, capped by the Flink total so it never exceeds what both paths agree on.
        confirmed_min = None
        if reason is not None:
            status, sellable, last_changed = "unknown", None, None
        else:
            confirmed_min = min(live_sum, sellable) if any_unknown else sellable
            if confirmed_min > 0:
                status = "available"
            elif any_unknown:
                status, reason = "unknown", "stores_unknown"
            else:
                status = "out_of_stock"

        body = {"product_id": product_id, "status": status, "sellable": sellable, "confirmed_min": confirmed_min,
                "at_least": any_unknown,
                "last_changed_at": last_changed, "restock_eta": parse_eta(product_id, eta_raw), "unknown_reason": reason, "feed": worst, "stores": stores,
                "product": cat.product(product_id), "release": cfg.release}
        sd.increment("stock.lookup.result", tags=[f"status:{status}",
                                                  f"unknown_reason:{reason or STATUS_TAG_UNKNOWN}",
                                                  f"at_least:{str(any_unknown).lower()}"])
        resp = jsonify(body)
        resp.headers["X-Release"] = cfg.release
        return resp

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/readyz")
    def readyz():
        if not app.config["READY"]:
            return jsonify(status="not_ready", reason="catalogue preparation pending", release=cfg.release), 503
        try:
            r.ping()
        except redis.RedisError as e:
            log.error("readyz: redis ping failed: %s: %s", type(e).__name__, e)
            return jsonify(status="not_ready", reason=f"redis ping failed: {type(e).__name__}", release=cfg.release), 503
        return jsonify(status="ready", release=cfg.release, catalogue_mode=cfg.mode,
                       catalogue_sha256=app.config["CATALOGUE_SHA256"])

    return app


def _e(name):
    import os
    return os.environ.get(name, "")
