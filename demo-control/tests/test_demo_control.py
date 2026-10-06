import json

import pytest

from conftest import PASSWORD, auth
from demo_control.backends import KafkaConfigBackend
from demo_control.config import Config, ConfigError, split_host_port
from demo_control.registry import ValidationError, load_registry
from conftest import REGISTRY

ENV = {"CONTROL_PASSWORD": "x", "REDIS_URL": "redis://r", "KAFKA_BOOTSTRAP": "k:9092", "SR_URL": "http://sr",
       "STACK": "dev", "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}


def put(client, key, value, **kw):
    return client.put(f"/control/api/params/{key}", json={"value": value}, headers=auth(), **kw)


def params(client):
    return {p["key"]: p for p in client.get("/control/api/params", headers=auth()).get_json()["params"]}


# --- config / auth ------------------------------------------------------------------------------------------------
def test_hybrid_store_endpoint_overrides_default_port():
    assert split_host_port("10.0.0.8:15431", 5432) == ("10.0.0.8", 15431)
    assert split_host_port("store-s01", 5432) == ("store-s01", 5432)


@pytest.mark.parametrize("endpoint", ["host:", ":15431", "host:nope", "host:70000"])
def test_hybrid_store_endpoint_rejects_invalid_port(endpoint):
    with pytest.raises(ConfigError, match="invalid TCP port"):
        split_host_port(endpoint, 5432)


def test_password_is_required_at_startup():
    env = {k: v for k, v in ENV.items() if k != "CONTROL_PASSWORD"}
    with pytest.raises(ConfigError, match="CONTROL_PASSWORD"):
        Config.from_env(env)


def test_sasl_needs_credentials():
    env = {k: v for k, v in ENV.items() if k != "KAFKA_SECURITY_PROTOCOL"}
    with pytest.raises(ConfigError, match="KAFKA_API_KEY"):
        Config.from_env(env)


def test_auth_required_everywhere_but_healthz(client):
    assert client.get("/control/").status_code == 401
    assert client.get("/control/api/params").status_code == 401
    assert client.put("/control/api/params/poll_ms", json={"value": 500}).status_code == 401
    assert client.get("/control/api/params", headers=auth(pw="nope")).status_code == 401
    assert client.get("/control/api/params", headers=auth(user="admin")).status_code == 401
    assert client.get("/control/healthz").status_code == 200
    assert client.get("/control/api/params", headers=auth()).status_code == 200


def test_page_renders_not_cached_and_grouped(client):
    r = client.get("/control/", headers=auth())
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    assert "Supplier lead time (base)" in html and "<h2>restock</h2>" in html and "Layers running" in html


def test_page_has_storefront_aligned_visual_contract_and_actions_before_parameters(client):
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert "--bg:#f4effb" in html and "--accent:#632ca6" in html
    assert "font:18px/1.5" in html and "--radius:16px" in html
    assert '<header class="control-header">' in html
    assert '<main class="control-page">' in html
    assert 'class="control-card actions-card"' in html
    assert '<label for="product-id">Product ID</label>' in html
    assert 'id="sell-out">Sell out product</button>' in html
    assert 'id="reset-data" class="secondary">Reset demo data</button>' in html
    assert 'class="action-progress"' in html
    assert html.index('class="control-card actions-card"') < html.index('<h2>core</h2>')
    assert html.index('id="sell-out"') < html.index('class="action-progress"')


# --- validation ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key,value,msg", [
    ("poll_ms", 100, "outside the allowed range 250..10000"),
    ("poll_ms", "fast", "must be a number"),
    ("poll_ms", True, "must be a number"),
    ("jev_min_confidence", 1.5, "outside"),
    ("offers_kill_switch", 0.5, "must be 0 or 1"),
    ("cart_active_window_min", 10, "read-only"),
])
def test_invalid_values_are_400_with_message(client, parts, key, value, msg):
    r = put(client, key, value)
    assert r.status_code == 400 and msg in r.get_json()["error"]
    assert parts["redis"].hgetall("demo:config") == {} and parts["statsd"].events == []


def test_bad_body_and_unknown_key(client):
    assert client.put("/control/api/params/poll_ms", data="x", headers=auth()).status_code == 400
    assert put(client, "nope", 1).status_code == 404


# --- writers and read-back -----------------------------------------------------------------------------------------
def test_redis_writer_and_read_back(client, parts):
    r = put(client, "probe_interval_s", 2)
    assert r.status_code == 200 and r.get_json()["value"] == 2.0
    assert parts["redis"].hget("demo:config", "probe_interval_s") == b"2"
    assert params(client)["probe_interval_s"]["value"] == 2.0


def test_kill_switch_mirrors_to_existing_key(client, parts):
    put(client, "offers_kill_switch", 1)
    assert parts["redis"].get("offers:kill_switch") == b"1" and parts["redis"].hget("demo:config", "offers_kill_switch") == b"1"
    assert params(client)["offers_kill_switch"]["value"] == 1.0
    put(client, "offers_kill_switch", 0)
    assert parts["redis"].get("offers:kill_switch") is None and params(client)["offers_kill_switch"]["value"] == 0.0


def test_kafka_writer_produces_avro_shaped_record(client, parts):
    r = put(client, "safety_factor", 1.5)
    assert r.status_code == 200 and r.get_json()["value"] == 1.5
    assert parts["producer"].sent == [({"key": "safety_factor"},
                                       {"key": "safety_factor", "value": 1.5, "updated_at_ms": 1_800_000_000_000})]


def test_kafka_seed_defaults_only_for_missing_keys(parts):
    parts["topic"]["coverage_h"] = 48.0
    seeded = parts["backends"]["kafka_config"].seed_defaults(list(parts["registry"].values()))
    assert sorted(seeded) == ["default_demand_per_hour", "lead_time_s", "min_order_qty", "safety_factor", "time_compression"]
    assert parts["topic"]["coverage_h"] == 48.0 and parts["topic"]["safety_factor"] == 1.2
    assert parts["backends"]["kafka_config"].seed_defaults(list(parts["registry"].values())) == []


def test_kafka_produce_failure_is_502(client, parts):
    parts["producer"].fail = RuntimeError("broker down")
    r = put(client, "coverage_h", 12)
    assert r.status_code == 502 and "coverage_h" in r.get_json()["error"] and "broker down" in r.get_json()["error"]
    assert parts["statsd"].events == []


def test_procurement_writer_upserts_text_row(client, parts):
    r = put(client, "lead_time_s", 3600)
    assert r.status_code == 200 and r.get_json()["value"] == 3600.0
    assert parts["proc"].table == {"lead_time_s": "3600"}
    assert any("ON CONFLICT (key)" in sql for sql, _ in parts["proc"].executed)


def test_store_dbs_writer_hits_every_store_in_a_transaction(client, parts):
    r = put(client, "sales_per_min_per_store", 60)
    assert r.status_code == 200 and r.get_json()["value"] == 60.0
    for s in ("s01", "s02"):
        assert parts[s].table == {"sales_per_min_per_store": 60.0} and parts[s].transactions == 1


def test_store_dbs_partial_failure_is_reported(client, parts):
    parts["s02"].fail = RuntimeError("permission denied for table demo_setting")
    r = put(client, "sales_per_min_per_store", 60)
    assert r.status_code == 502 and "S01" in r.get_json()["error"] and "S02" in r.get_json()["error"]
    assert parts["statsd"].events == []


def test_store_dbs_disagreement_is_shown(client, parts):
    parts["s01"].table["sales_per_min_per_store"] = 1.0
    parts["s02"].table["sales_per_min_per_store"] = 2.0
    p = params(client)["sales_per_min_per_store"]
    assert p["status"] == "error" and "S01=1" in p["detail"] and "S02=2" in p["detail"]


def test_unset_means_default_in_use(client):
    p = params(client)["poll_ms"]
    assert p["status"] == "unset" and p["value"] is None and p["default"] == 1000


def test_flink_params_are_read_only_rows(client):
    p = params(client)["demand_window_min"]
    assert p["status"] == "readonly" and p["read_only"] is True and p["value"] == 10


# --- layers off ---------------------------------------------------------------------------------------------------
def test_procurement_unreachable_rows_are_layer_off_not_errors(client, parts):
    parts["proc"].fail = ConnectionError("connection refused")
    p = params(client)
    assert p["lead_time_s"]["status"] == "layer_off" and p["lead_time_jitter_pct"]["status"] == "layer_off"
    assert p["poll_ms"]["status"] == "unset"  # the rest of the page is unaffected
    r = put(client, "lead_time_s", 100)
    assert r.status_code == 409 and "layer off" in r.get_json()["error"]
    assert "disabled" in client.get("/control/", headers=auth()).get_data(as_text=True)


def test_all_store_sources_down_is_layer_off(client, parts):
    for s in ("s01", "s02"):
        parts[s].fail = ConnectionError("down")
    assert params(client)["sales_per_min_per_store"]["status"] == "layer_off"
    assert put(client, "sales_per_min_per_store", 5).status_code == 409


def test_procurement_not_configured_is_layer_off(parts):
    from demo_control.backends import ProcurementBackend
    got = ProcurementBackend(None).read([parts["registry"]["lead_time_s"]])
    assert got["lead_time_s"].status == "layer_off"


# --- audit --------------------------------------------------------------------------------------------------------
def test_change_sends_event_and_json_log(client, parts, caplog):
    with caplog.at_level("INFO", logger="demo_control"):
        put(client, "stale_after_s", 30)
    (ev,) = parts["statsd"].events
    assert ev["title"] == "demo config: stale_after_s = 30"
    assert ev["tags"] == ["project:dd-demo", "stack:dev", "layer:core", "param:stale_after_s", "demo_event:config"]
    rec = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "demo_config_changed"]
    assert len(rec) == 1 and rec[0].fields["param"] == "stale_after_s" and rec[0].fields["value"] == 30.0


def test_statsd_failure_does_not_undo_the_change(client, parts):
    def boom(*a, **k):
        raise OSError("udp")
    parts["statsd"].event = boom
    assert put(client, "stale_after_s", 30).status_code == 200
    assert parts["redis"].hget("demo:config", "stale_after_s") == b"30"


# --- layers / routing context -------------------------------------------------------------------------------------
def test_layers_and_routing_unknown_then_known(client, parts):
    body = client.get("/control/api/params", headers=auth()).get_json()
    assert body["layers"] is None and body["routing"] is None
    parts["redis"].set("demo:layers", json.dumps(["core", "restock"]))
    parts["redis"].set("demo:routing", "v1=90 v2=10")
    body = client.get("/control/api/params", headers=auth()).get_json()
    assert body["layers"] == ["core", "restock"] and body["routing"] == "v1=90 v2=10"
    parts["redis"].set("demo:layers", "core, offers")
    assert parts["control"].layers() == ["core", "offers"]


def test_routing_file_wins(parts, tmp_path):
    f = tmp_path / "routing.json"
    f.write_text('{"canary_pct": 10}')
    parts["control"]._routing_file = str(f)
    assert parts["control"].routing() == '{"canary_pct": 10}'


# --- multi-store params (time_compression: kafka_config,redis) --------------------------------------------------------
def test_time_compression_is_in_two_stores():
    p = load_registry(REGISTRY)["time_compression"]
    assert p.stores == ("kafka_config", "redis") and p.default == 60


def test_multi_store_write_goes_to_every_store(client, parts):
    r = put(client, "time_compression", 120)
    assert r.status_code == 200 and r.get_json()["value"] == 120.0
    assert parts["topic"]["time_compression"] == 120.0
    assert parts["redis"].hget("demo:config", "time_compression") == b"120"
    assert params(client)["time_compression"]["value"] == 120.0


def test_multi_store_disagreement_is_an_error_row(client, parts):
    parts["topic"]["time_compression"] = 60.0
    parts["redis"].hset("demo:config", "time_compression", "30")
    p = params(client)["time_compression"]
    assert p["status"] == "error" and "kafka_config=60" in p["detail"] and "redis=30" in p["detail"]


def test_multi_store_second_store_failure_is_reported(client, parts):
    class Boom:
        def hset(self, *a): raise RuntimeError("redis down")
    parts["backends"]["redis"]._r = Boom()
    r = put(client, "time_compression", 120)
    assert r.status_code == 502 and "redis" in r.get_json()["error"] and "kafka_config" in r.get_json()["error"]
    assert parts["statsd"].events == []


def test_multi_store_unset_in_both_is_unset(client):
    assert params(client)["time_compression"]["status"] == "unset"
