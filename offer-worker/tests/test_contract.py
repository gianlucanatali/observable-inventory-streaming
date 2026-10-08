"""offers value schema evolution: the schema without restock_eta (the previous version) and the current one must read
each other's records (backward and forward, so FULL compatibility). Schema Registry's default mode is BACKWARD; this
also keeps an older storefront working while the offer-worker rolls out first, or the other way round."""
import io
import json

import fastavro

from conftest import CONTRACT_AVRO

CURRENT = json.loads((CONTRACT_AVRO / "offer.avsc").read_text())
PREVIOUS = {**CURRENT, "fields": [f for f in CURRENT["fields"] if f["name"] != "restock_eta"]}

BASE = {"offer_id": "offer|r1", "risk_id": "r1", "scenario_id": "S1", "cart_id": "C1", "original_store_id": "ONLINE",
        "original_product_id": "P0042", "offer_type": "ALTERNATIVE_PRODUCT", "store_id": None, "product_id": "P0061",
        "discount_pct": 10, "headline": "Trailrunner GTX just sold out", "body": "b", "decision_route": "JEV",
        "decision_reason": "accepted", "jev_choice": "alt:P0061", "jev_confidence": 0.86, "min_confidence": 0.8,
        "rule_choice": "alt:P0160", "chosen_choice": "alt:P0061", "text_route": "TEMPLATE", "text_reason": "disabled",
        "created_at": 1_800_000_000_000}


def roundtrip(record, writer, reader):
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, fastavro.parse_schema(writer), record)
    buf.seek(0)
    return fastavro.schemaless_reader(buf, fastavro.parse_schema(writer), fastavro.parse_schema(reader))


def test_restock_eta_is_optional_with_a_null_default():
    field = next(f for f in CURRENT["fields"] if f["name"] == "restock_eta")
    assert field["type"][0] == "null" and field["default"] is None


def test_new_reader_reads_old_records_as_no_restock_notice():
    got = roundtrip(BASE, PREVIOUS, CURRENT)
    assert got["restock_eta"] is None and got["product_id"] == "P0061"


def test_old_reader_reads_new_records_and_ignores_the_restock_notice():
    both = {**BASE, "restock_eta": 1_800_003_600_000}
    got = roundtrip(both, CURRENT, PREVIOUS)
    assert "restock_eta" not in got and got["product_id"] == "P0061"
    none = {**BASE, "offer_type": "NOTIFY_ME", "product_id": None, "chosen_choice": None, "discount_pct": 0,
            "decision_reason": "no_good_substitute", "jev_choice": "none", "restock_eta": None}
    assert roundtrip(none, CURRENT, PREVIOUS)["offer_type"] == "NOTIFY_ME"


def test_offer_type_enum_is_unchanged():
    offer_type = next(f for f in CURRENT["fields"] if f["name"] == "offer_type")["type"]
    assert offer_type["symbols"] == ["SAME_PRODUCT_OTHER_STORE", "ALTERNATIVE_PRODUCT", "NOTIFY_ME"]
