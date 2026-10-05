import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_existing_subject_schema_is_exact_backward_additive_shape():
    schema = json.loads((ROOT / "contracts/avro/flink_cart_at_risk_value.avsc").read_text())
    prior = json.loads((ROOT / "flink/tests/fixtures/carts_at_risk_value_prior.avsc").read_text())
    assert (schema["namespace"], schema["name"]) == ("org.apache.flink.avro.generated.record", "carts_at_risk_value")
    assert schema["fields"][:5] == prior["fields"][:5]
    fields = {field["name"]: field for field in schema["fields"]}
    for name in ("cart_value_eur", "returning_shopper", "item_count"):
        assert fields[name]["type"][0] == "null" and fields[name]["default"] is None
    assert fields["risk_id"]["type"] == "string" and "default" not in fields["risk_id"]


def test_schema_is_registered_before_fresh_catalog_ddl_and_dml():
    tf = (ROOT / "terraform/cloud/flink_statements.tf").read_text()
    assert 'subject_name  = "carts.at-risk-value"' in tf
    schema_block = tf.split('resource "confluent_schema" "carts_at_risk_value"', 1)[1].split('resource "confluent_flink_statement" "ddl"', 1)[0]
    assert "count" not in schema_block
    assert "prevent_destroy" not in schema_block
    assert 'confluent_api_key.sr["flink"]' in tf
    assert "confluent_schema.carts_at_risk_value" in tf
    assert "depends_on = [confluent_flink_statement.ddl]" in tf


def test_reader_hint_is_exact_and_create_remains_zero_state_only():
    sellable = (ROOT / "flink/sellable.sql").read_text()
    offers = (ROOT / "flink/cart_at_risk.sql").read_text()
    assert "'kafka.consumer.isolation-level' = 'read-uncommitted'" in sellable
    assert "JOIN `stock.sellable` /*+ OPTIONS('kafka.consumer.isolation-level' = 'read-uncommitted') */ AS s" in offers
    assert "CREATE TABLE IF NOT EXISTS `carts.at-risk`" in offers
    assert "CREATE TABLE IF NOT EXISTS `stock.sellable`" in sellable
