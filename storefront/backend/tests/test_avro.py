"""Round trip through the real serializers with the real contract schemas (mock Schema Registry)."""
from datetime import datetime, timezone

from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

from app import cart as cart_mod
from app import kafka_io
from app.config import Config
from pathlib import Path
from tests.conftest import ENV

AVRO = Path(__file__).resolve().parents[2].parent / "contracts" / "avro"


def test_cart_event_serialises_with_contract_schema_and_offer_schema_deserialises():
    cfg = Config.from_env({**ENV, "AVRO_DIR": str(AVRO)})
    sr = SchemaRegistryClient.new_client({"url": "mock://roundtrip"})
    key_ser, val_ser = kafka_io.cart_serializers(cfg, sr)
    key, value = cart_mod.build_cart_event(
        {"product_id": "P0042", "event_type": "ADD"}, "sc-1")
    kb = key_ser(key, SerializationContext("carts.events", MessageField.KEY))
    vb = val_ser(value, SerializationContext("carts.events", MessageField.VALUE))
    back = AvroDeserializer(sr, (AVRO / "cart_event.avsc").read_text())(
        vb, SerializationContext("carts.events", MessageField.VALUE))
    assert back["cart_id"] == key["cart_id"] and back["event_type"] == "ADD"
    assert isinstance(back["event_time"], datetime) and kb

    from confluent_kafka.schema_registry.avro import AvroSerializer
    offer = {
        "offer_id": "o1", "risk_id": "r1", "scenario_id": "sc-1", "cart_id": "c", "original_store_id": "S03",
        "original_product_id": "P0042", "offer_type": "NOTIFY_ME", "store_id": None, "product_id": None,
        "discount_pct": 0, "headline": "h", "body": "b", "decision_route": "RULE_DEFAULT",
        "decision_reason": "timeout", "text_route": "TEMPLATE", "text_reason": "disabled",
        "jev_choice": "none", "jev_confidence": 0.76, "min_confidence": 0.8,
        "rule_choice": "alt:P0160", "chosen_choice": "alt:P0160",
        "restock_eta": datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }
    ob = AvroSerializer(sr, (AVRO / "offer.avsc").read_text())(offer, SerializationContext("offers", MessageField.VALUE))
    got = AvroDeserializer(sr, (AVRO / "offer.avsc").read_text())(ob, SerializationContext("offers", MessageField.VALUE))
    from app.offers import offer_to_json
    assert offer_to_json(got)["offer_type"] == "NOTIFY_ME"
    assert {key: offer_to_json(got)[key] for key in ("jev_choice", "jev_confidence", "min_confidence", "rule_choice", "chosen_choice")} == {
        "jev_choice": "none", "jev_confidence": 0.76, "min_confidence": 0.8,
        "rule_choice": "alt:P0160", "chosen_choice": "alt:P0160"}
    assert offer_to_json(got)["restock_eta"] == "2026-10-09T10:00:00+00:00"


def test_cart_event_schema_adds_shopper_signals_with_backward_compatible_defaults():
    import fastavro
    import io
    import json

    schema = json.loads((AVRO / "cart_event.avsc").read_text())
    defaults = {field["name"]: field.get("default") for field in schema["fields"]}
    assert {"cart_value_eur": 0.0, "returning_shopper": False, "item_count": 1}.items() <= defaults.items()
    old_schema = {**schema, "fields": schema["fields"][:-3]}
    writer = io.BytesIO()
    fastavro.schemaless_writer(writer, old_schema, {
        "event_id": "e", "scenario_id": "sc", "cart_id": "c", "shopper_id": "s", "store_id": "ONLINE",
        "product_id": "P0042", "event_type": "ADD", "event_time": 0,
    })
    decoded = fastavro.schemaless_reader(io.BytesIO(writer.getvalue()), old_schema, schema)
    assert {field: decoded[field] for field in defaults if field in ("cart_value_eur", "returning_shopper", "item_count")} == {
        "cart_value_eur": 0.0, "returning_shopper": False, "item_count": 1}
