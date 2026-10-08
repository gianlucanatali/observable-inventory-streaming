import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
ITEM_KEY = ["scenario_id", "cart_id", "product_id"]


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.strip().startswith("--"))


def _primary_key(code: str, table: str) -> list[str]:
    ddl = code.split(f"CREATE TABLE IF NOT EXISTS `{table}`", 1)[1].split(";", 1)[0]
    match = re.search(r"PRIMARY KEY \(([^)]*)\) NOT ENFORCED", ddl)
    assert match, f"{table} needs a PRIMARY KEY ... NOT ENFORCED"
    return [c.strip() for c in match.group(1).split(",")]


def test_at_risk_sink_key_is_the_query_upsert_key():
    """Sink PRIMARY KEY = the join's upsert key, so CC adds no upsert materializer."""
    code = _code(ROOT / "flink/cart_at_risk.sql")
    assert _primary_key(code, "carts.at-risk") == ITEM_KEY
    assert "PARTITION BY scenario_id, cart_id, product_id ORDER BY `$rowtime` DESC" in code
    assert "ON a.product_id = s.product_id" in code
    assert "'changelog.mode' = 'upsert'" in code and "'value.fields-include' = 'all'" in code


def test_key_contract_matches_the_sink_primary_key_and_risk_id_stays_in_the_value():
    key = json.loads((ROOT / "contracts/avro/cart_at_risk_key.avsc").read_text())
    value = json.loads((ROOT / "contracts/avro/cart_at_risk.avsc").read_text())
    assert [(f["name"], f["type"]) for f in key["fields"]] == [(name, "string") for name in ITEM_KEY]
    assert "risk_id" in [f["name"] for f in value["fields"]]


def test_sellable_sink_key_is_its_group_by_key():
    code = _code(ROOT / "flink/sellable.sql")
    assert _primary_key(code, "stock.sellable") == ["product_id"]
    assert re.search(r"GROUP BY product_id;?\s*$", code.strip())


def test_flink_output_subjects_are_registered_by_create_table_not_terraform():
    """On a fresh stack each CREATE TABLE registers <table>-key/-value; a pre-registered value schema would
    differ from the one Flink derives from the new key and make the readiness poll trivially true."""
    tf = (ROOT / "terraform/cloud/flink_statements.tf").read_text()
    assert 'resource "confluent_schema"' not in tf
    assert not (ROOT / "contracts/avro/flink_cart_at_risk_value.avsc").exists()
    main = (ROOT / "terraform/cloud/main.tf").read_text()
    assert 'flink-sr-write-at-risk = { sa = "flink", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=carts.at-risk-*" }' in main


def test_no_state_ttl_on_the_offers_join():
    """The design keeps join state without TTL: a hint expires a cart item 2 h after its own ADD while the cart hash
    lives 2 h after the cart's last change, so a still-open cart could silently stop firing."""
    code = _code(ROOT / "flink/cart_at_risk.sql")
    assert "STATE_TTL" not in code and "sql.state-ttl" not in code


def test_reader_hint_is_exact_and_create_remains_zero_state_only():
    sellable = (ROOT / "flink/sellable.sql").read_text()
    offers = (ROOT / "flink/cart_at_risk.sql").read_text()
    assert "'kafka.consumer.isolation-level' = 'read-uncommitted'" in sellable
    assert "JOIN `stock.sellable` /*+ OPTIONS('kafka.consumer.isolation-level' = 'read-uncommitted') */ AS s" in offers
    assert "CREATE TABLE IF NOT EXISTS `carts.at-risk`" in offers
    assert "CREATE TABLE IF NOT EXISTS `stock.sellable`" in sellable
