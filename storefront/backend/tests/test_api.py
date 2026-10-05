from datetime import datetime, timedelta, timezone

import pytest

from app.offers import OfferConsumer
from app.config import Config, ConfigError
from tests.conftest import ENV


def cart_body(**kw):
    return {"product_id": "P0042", "event_type": "ADD", **kw}


def test_config_names_missing_variable():
    env = dict(ENV)
    del env["REDIS_URL"]
    with pytest.raises(ConfigError, match="REDIS_URL"):
        Config.from_env(env)


def test_config_endpoint(make):
    client, _ = make(offers_enabled=False, POLL_MS="500")
    assert client.get("/config").get_json() == {"poll_ms": 500, "offers_enabled": False, "release": "1.0.0", "time_compression": 60}


def test_add_publishes_event_with_scenario(make):
    client, deps = make()
    r = client.post("/api/cart", json=cart_body())
    assert r.status_code == 201
    key, value = deps.publisher.sent[0]
    assert key == {"cart_id": r.get_json()["cart_id"]}
    assert value["scenario_id"] == "sc-1" and value["event_type"] == "ADD"
    assert value["shopper_id"].startswith("shopper-")


def test_cart_event_carries_deterministic_synthetic_shopper_signals(make):
    client, deps = make()
    cart_id = client.post("/api/cart", json=cart_body()).get_json()["cart_id"]
    client.post("/api/cart", json=cart_body(cart_id=cart_id, event_type="ABANDON"))
    add, abandon = [value for _, value in deps.publisher.sent]
    for field in ("cart_value_eur", "returning_shopper", "item_count"):
        assert field in add
    assert isinstance(add["cart_value_eur"], float) and add["cart_value_eur"] > 0
    assert isinstance(add["returning_shopper"], bool)
    assert isinstance(add["item_count"], int) and add["item_count"] >= 1
    assert {field: abandon[field] for field in ("cart_value_eur", "returning_shopper", "item_count")} == {
        field: add[field] for field in ("cart_value_eur", "returning_shopper", "item_count")}


def test_cart_events_are_always_online(make):
    client, deps = make()
    assert client.post("/api/cart", json=cart_body()).status_code == 201
    assert client.post("/api/cart", json=cart_body(store_id="ONLINE")).status_code == 201
    assert [v["store_id"] for _, v in deps.publisher.sent] == ["ONLINE", "ONLINE"]


def test_abandon_requires_cart_id(make):
    client, deps = make()
    assert client.post("/api/cart", json=cart_body(event_type="ABANDON")).status_code == 400
    assert deps.publisher.sent == []


def test_abandon_reuses_cart(make):
    client, deps = make()
    cart_id = client.post("/api/cart", json=cart_body()).get_json()["cart_id"]
    assert client.post("/api/cart", json=cart_body(cart_id=cart_id, event_type="ABANDON")).status_code == 201
    assert deps.publisher.sent[1][1]["cart_id"] == cart_id


@pytest.mark.parametrize("bad", [{"store_id": "S03"}, {"product_id": "42"}, {"event_type": "BUY"}, {"cart_id": "nope"}])
def test_cart_validation(make, bad):
    client, deps = make()
    assert client.post("/api/cart", json=cart_body(**bad)).status_code == 400
    assert deps.publisher.sent == []


def test_cart_missing_scenario_is_503(make):
    client, deps = make(scenario=None)
    r = client.post("/api/cart", json=cart_body())
    assert r.status_code == 503 and "scenario:current" in r.get_json()["message"]
    assert deps.publisher.sent == []


def test_cart_publish_failure_is_502(make):
    client, deps = make()
    deps.publisher.fail = RuntimeError("broker down")
    r = client.post("/api/cart", json=cart_body())
    assert r.status_code == 502 and "broker down" in r.get_json()["message"]


def test_offers_latest_wins_and_ignores_other_scenario(make):
    client, deps = make()
    base = dict(offer_id="o", cart_id="cart-1", scenario_id="sc-1", offer_type="NOTIFY_ME",
                original_store_id="S03", original_product_id="P0042", discount_pct=0, headline="h1",
                body="b", decision_route="RULE_DEFAULT", text_route="TEMPLATE")
    deps.offer_store.put(base)
    deps.offer_store.put({**base, "headline": "h2"})
    deps.offer_store.put({**base, "scenario_id": "old", "headline": "stale"})
    r = client.get("/api/offers?cart_id=cart-1")
    assert r.get_json()["offer"]["headline"] == "h2"
    assert client.get("/api/offers?cart_id=other").get_json() == {"offer": None}


def test_offers_disabled(make):
    client, _ = make(offers_enabled=False)
    assert client.get("/api/offers?cart_id=x").status_code == 404


def test_offers_requires_cart_id(make):
    client, _ = make()
    assert client.get("/api/offers").status_code == 400


def _at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_beacon_records_distribution_from_backend_receive_time(make):
    client, deps = make(now=_at("2026-10-09T11:02:01.500Z"))
    r = client.post("/api/beacon/display", json={
        "product_id": "P0042", "last_changed_at": "2026-10-09T11:02:00.000Z"})
    assert r.status_code == 204
    assert deps.statsd.calls == [("distribution", "stock.display.delay", 1.5, None)]


def test_beacon_ignores_a_browser_supplied_timestamp(make):
    client, deps = make(now=_at("2026-10-09T11:02:01.500Z"))
    r = client.post("/api/beacon/display", json={
        "product_id": "P0042", "last_changed_at": "2026-10-09T11:02:00.000Z",
        "rendered_at": "2030-01-01T00:00:00Z"})
    assert r.status_code == 204
    assert deps.statsd.calls == [("distribution", "stock.display.delay", 1.5, None)]


@pytest.mark.parametrize("changed,now,reason", [
    ("2026-10-09T11:02:05Z", "2026-10-09T11:02:00Z", "negative"),
    ("2026-10-09T09:00:00Z", "2026-10-09T11:02:00Z", "too_large"),
    ("garbage", "2026-10-09T11:02:00Z", "invalid"),
])
def test_beacon_rejections_are_counted(make, changed, now, reason):
    client, deps = make(now=_at(now))
    r = client.post("/api/beacon/display", json={"product_id": "P0042",
                                                 "last_changed_at": changed})
    assert r.status_code == 422
    assert deps.statsd.calls == [("increment", "stock.display.beacon_rejected", 1, [f"reason:{reason}"])]


def test_beacon_default_clock_is_the_real_utc_now(make):
    client, deps = make()
    changed = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    r = client.post("/api/beacon/display", json={"product_id": "P0042",
                                                 "last_changed_at": changed})
    assert r.status_code == 204
    (_, name, value, _tags), = deps.statsd.calls
    assert name == "stock.display.delay" and 2.0 <= value < 10.0


def test_static_and_spa_fallback_and_api_404(make):
    client, _ = make()
    assert b"ui" in client.get("/").data
    assert client.get("/app.js").status_code == 200
    assert client.get("/missing.js").status_code == 404
    assert client.get("/api/nope").get_json()["error"] == "not_found"


def test_product_image(make):
    client, _ = make()
    r = client.get("/img/P0042.svg")
    assert r.mimetype == "image/svg+xml" and client.get("/img/evil.svg").status_code == 404
    assert client.get("/img/P9999.svg").status_code == 404
    assert "max-age" in r.headers["Cache-Control"]


def test_product_list_and_detail(make):
    client, _ = make()
    items = client.get("/api/products").get_json()
    assert len(items) == 200
    p42 = client.get("/api/products/P0042").get_json()
    assert (p42["name"], p42["brand"], p42["size"], p42["category"]) == ("Trailrunner GTX", "Alpenpace", "EU 42", "footwear")
    assert p42["price_eur"] > 0 and p42["colour"]["hex"].startswith("#")
    r = client.get("/api/products/P0999")
    assert r.status_code == 404 and r.get_json()["error"] == "not_found"


def test_product_images_per_category_are_self_contained_svg(make):
    client, _ = make()
    by_cat = {}
    for p in client.get("/api/products").get_json():
        by_cat.setdefault(p["category"], p["product_id"])
    assert set(by_cat) == {"footwear", "apparel", "accessories"}
    bodies = {}
    for cat, pid in by_cat.items():
        body = client.get(f"/img/{pid}.svg").get_data(as_text=True)
        assert body.startswith("<svg") and "http://www.w3.org/2000/svg" in body
        assert "href" not in body and pid not in body
        bodies[cat] = body
    assert len(set(bodies.values())) == 3
    ET = __import__("xml.etree.ElementTree", fromlist=["x"])
    for p in client.get("/api/products").get_json():  # every product renders well-formed XML
        ET.fromstring(client.get(f"/img/{p['product_id']}.svg").data)


class _Msg:
    def __init__(self, value=None, err=None):
        self._v, self._e = value, err

    def value(self):
        return self._v

    def error(self):
        return self._e


class _FakeConsumer:
    def __init__(self, items):
        self.items = list(items)
        self.closed = False

    def poll(self, timeout):
        if self.items:
            return self.items.pop(0)
        import time
        time.sleep(0.01)
        return None

    def close(self):
        self.closed = True


def _wait(pred):
    import time
    for _ in range(200):
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_consumer_fills_store_and_readyz_ok(make):
    from app.offers import OfferStore
    offer = dict(offer_id="o", cart_id="c", scenario_id="sc-1", offer_type="NOTIFY_ME", original_store_id="S03",
                 original_product_id="P0042", discount_pct=0, headline="h", body="b",
                 decision_route="RULE_DEFAULT", text_route="TEMPLATE")
    store = OfferStore()
    oc = OfferConsumer(_FakeConsumer([_Msg(offer)]), store)
    oc.start()
    try:
        _wait(lambda: store.get("sc-1", "c") is not None)
        client, _ = make(consumer=oc)
        r = client.get("/readyz")
        assert r.status_code == 200 and r.get_json()["offers_consumer"]["state"] == "running"
    finally:
        oc.stop()


def test_consumer_failure_surfaces_in_readyz(make, caplog):
    class Err:
        def code(self):
            return 1

        def __str__(self):
            return "broker transport failure"

    oc = OfferConsumer(_FakeConsumer([_Msg(err=Err())]), __import__("app.offers").offers.OfferStore())
    oc.start()
    _wait(lambda: oc.status()["state"] == "failed")
    client, _ = make(consumer=oc)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "broker transport failure" in r.get_json()["problems"]["offers_consumer"]
    # the failure state is set just before the thread logs: wait for the log record instead of racing it
    _wait(lambda: any("offers consumer failed" in rec.getMessage() for rec in caplog.records))


def test_readyz_without_offers_and_redis_down(make):
    client, deps = make(offers_enabled=False)
    assert client.get("/readyz").get_json()["offers_consumer"] == {"state": "disabled"}
    deps.redis.fail = ConnectionError("refused")
    r = client.get("/readyz")
    assert r.status_code == 503 and "refused" in r.get_json()["problems"]["redis"]


@pytest.mark.parametrize("bad", [{"product_id": 42}, {"last_changed_at": None}, {"last_changed_at": "2026-10-09T11:02:00"}])
def test_beacon_requires_product_and_zoned_last_changed_at(make, bad):
    client, deps = make(now=_at("2026-10-09T11:02:01.500Z"))
    body = {"product_id": "P0042", "last_changed_at": "2026-10-09T11:02:00.000Z", **bad}
    r = client.post("/api/beacon/display", json=body)
    assert r.status_code == 422
    assert deps.statsd.calls == [("increment", "stock.display.beacon_rejected", 1, ["reason:invalid"])]


def test_config_has_no_rum_block_without_env(make):
    client, _ = make()
    assert "rum" not in client.get("/config").get_json()


def test_config_rum_block_from_env(make):
    client, _ = make(DD_RUM_APPLICATION_ID="app-1", DD_RUM_CLIENT_TOKEN="pub-tok", STACK="rehearsal",
                     EXPOSURE_BUDGET_MS="500")
    assert client.get("/config").get_json()["rum"] == {
        "application_id": "app-1", "client_token": "pub-tok", "site": "datadoghq.eu",
        "service": "storefront-web", "env": "dd-demo", "version": "1.0.0", "stack": "rehearsal",
        "exposure_budget_ms": 500}


def test_config_rum_site_override(make):
    client, _ = make(DD_RUM_APPLICATION_ID="a", DD_RUM_CLIENT_TOKEN="t", DD_SITE="datadoghq.com")
    assert client.get("/config").get_json()["rum"]["site"] == "datadoghq.com"


def test_config_rum_half_configured_fails_loudly(make):
    with pytest.raises(ConfigError, match="DD_RUM_CLIENT_TOKEN"):
        make(DD_RUM_APPLICATION_ID="a")


def test_config_live_values_from_demo_config(make):
    client, deps = make(POLL_MS="500", DD_RUM_APPLICATION_ID="a", DD_RUM_CLIENT_TOKEN="t", EXPOSURE_BUDGET_MS="500")
    deps.redis.data["demo:config"] = {"poll_ms": "2000", "exposure_budget_ms": "1200"}
    body = client.get("/config").get_json()
    assert body["poll_ms"] == 2000 and body["rum"]["exposure_budget_ms"] == 1200
    deps.redis.data["demo:config"]["poll_ms"] = "300"  # live: next request sees it
    assert client.get("/config").get_json()["poll_ms"] == 300


def test_config_live_fallbacks_and_log_once(make, caplog):
    client, deps = make(POLL_MS="500")
    deps.redis.data["demo:config"] = {"exposure_budget_ms": "5"}  # out of range 50..10000
    with caplog.at_level("WARNING"):
        assert client.get("/config").get_json()["poll_ms"] == 500
        assert client.get("/config").get_json()["poll_ms"] == 500
    assert len([r for r in caplog.records if "poll_ms unavailable" in r.getMessage()]) == 1
    deps.redis.fail = RuntimeError("redis down")
    assert client.get("/config").get_json()["poll_ms"] == 500


def test_config_time_compression_live_and_fallback(make, caplog):
    client, deps = make()
    with caplog.at_level("WARNING"):
        assert client.get("/config").get_json()["time_compression"] == 60  # absent: fallback 60, logged once
        assert client.get("/config").get_json()["time_compression"] == 60
    assert len([r for r in caplog.records if "time_compression unavailable" in r.getMessage()]) == 1
    deps.redis.data["demo:config"] = {"time_compression": "120"}
    assert client.get("/config").get_json()["time_compression"] == 120
    deps.redis.data["demo:config"]["time_compression"] = "0"  # out of range
    assert client.get("/config").get_json()["time_compression"] == 60
