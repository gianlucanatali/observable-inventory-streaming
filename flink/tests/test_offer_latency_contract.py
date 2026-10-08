"""Offline contract for the sell-out-to-rendered-offer latency budget.

This checks configured limits only; the cloud measurement remains the release gate.
"""
from pathlib import Path
import re


ROOT = Path(__file__).parents[2]


def _int_constant(path: Path, name: str) -> int:
    match = re.search(rf"^{name}\s*=\s*(\d+)", path.read_text(), re.MULTILINE)
    assert match, f"{path.relative_to(ROOT)} must define {name}"
    return int(match.group(1))


def test_cart_at_risk_path_uses_uncommitted_flink_output_within_budget():
    sellable_sql = (ROOT / "flink" / "sellable.sql").read_text()
    worker_kafka = (ROOT / "offer-worker" / "offer_worker" / "kafka_io.py").read_text()
    storefront_kafka = (ROOT / "storefront" / "backend" / "app" / "kafka_io.py").read_text()
    storefront_app = (ROOT / "storefront" / "backend" / "app" / "__init__.py").read_text()
    makefile = (ROOT / "Makefile").read_text()

    # Confluent Cloud commits Flink transactions about once a minute. The cart-risk
    # statement must see the sellable update before that commit; both app consumers
    # deliberately accept duplicate correct records and deduplicate by risk/offer id.
    assert "'kafka.consumer.isolation-level' = 'read-uncommitted'" in sellable_sql
    assert "JOIN `stock.sellable` /*+ OPTIONS('kafka.consumer.isolation-level' = 'read-uncommitted') */ AS s" in (ROOT / "flink" / "cart_at_risk.sql").read_text()
    sellable_statements = [part for part in sellable_sql.split(";") if any(
        not line.strip().startswith("--") and line.strip() for line in part.splitlines()
    )]
    assert len(sellable_statements) == 2, "Terraform requires sellable.sql to contain CREATE and INSERT only"
    assert '"isolation.level": "read_uncommitted"' in worker_kafka
    assert '"isolation.level": "read_uncommitted"' in storefront_kafka

    gap_match = re.search(r"^GAP \?= (\d+(?:\.\d+)?)$", makefile, re.MULTILINE)
    assert gap_match, "Makefile must define the default sell-out GAP"
    gap = float(gap_match.group(1))
    sell_out_ms = int(4 * gap * 1000)  # five stores, with a gap only between stores
    flink_propagation_ms = 100  # Confluent's documented at-least-once path target
    risk_poll_ms = 1000
    offer_delivery_ms = _int_constant(ROOT / "offer-worker" / "offer_worker" / "kafka_io.py", "DELIVERY_TIMEOUT_MS")
    jev_timeout_ms = 800
    storefront_offer_consumer_ms = 1000
    browser_poll_ms = 2000

    total_ms = (sell_out_ms + flink_propagation_ms + risk_poll_ms + jev_timeout_ms
                + offer_delivery_ms + storefront_offer_consumer_ms + browser_poll_ms)
    assert total_ms < 15_000, f"configured sell-out-to-render budget is {total_ms} ms, not under 15 s"
    assert 'live_int("poll_ms", cfg.poll_ms, 250, 2_000)' in storefront_app


def test_offers_path_statements_run_without_watermark_alignment():
    tf = (ROOT / "terraform" / "cloud" / "flink_statements.tf").read_text()
    # Confluent documents drift 0 for regular joins and non-windowed aggregations (no event time needed).
    assert '"sql.tables.scan.watermark-alignment.max-allowed-drift" = "0 ms"' in tf
    files = re.search(r'no_alignment_files\s*=\s*\[([^\]]*)\]', tf)
    assert files, "flink_statements.tf must list the files that run without watermark alignment"
    assert sorted(re.findall(r'"([^"]+)"', files.group(1))) == ["offers", "sellable"]
    # restock uses HOP windows (event time): it must keep the default alignment.
    for f in ("demand", "procurement", "restock"):
        assert f'"{f}"' not in files.group(1)
    # Only the INSERT statements change: CREATE TABLE keeps the base properties, so no topic is recreated.
    ddl = tf[tf.index('resource "confluent_flink_statement" "ddl"'):tf.index('resource "confluent_flink_statement" "dml"')]
    assert "properties     = local.flink_props\n" in ddl
    for res in ('"dml"', '"dml_late"'):
        block = tf[tf.index(f'resource "confluent_flink_statement" {res}'):]
        block = block[:block.index("\n}\n")]
        assert "properties     = local.dml_props[each.key]" in block, res
    for sql in ("sellable.sql", "cart_at_risk.sql"):
        text = (ROOT / "flink" / sql).read_text().upper()
        for op in ("TUMBLE(", "HOP(", "CUMULATE(", "SESSION(", "MATCH_RECOGNIZE", "FOR SYSTEM_TIME AS OF", "WATERMARK FOR"):
            assert op not in text, f"{sql} uses {op}: it needs watermark alignment, remove it from no_alignment_files"
