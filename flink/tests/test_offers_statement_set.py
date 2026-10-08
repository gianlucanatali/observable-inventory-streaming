"""Offers layer: the INSERTs of sellable.sql and cart_at_risk.sql run as one EXECUTE STATEMENT SET.

The at-risk INSERT computes the sellable aggregation in-job instead of reading the stock.sellable topic.
Its CTE must stay a verbatim copy of the sellable.sql SELECT, so the planner can share the identical sub-plan.
"""
from pathlib import Path
import re

ROOT = Path(__file__).parents[2]
TF = (ROOT / "terraform" / "cloud" / "flink_statements.tf").read_text()


def _statements(name: str) -> list[str]:
    """Same split as flink_statements.tf: on ';', comment lines dropped, empty parts skipped."""
    out = []
    for part in (ROOT / "flink" / name).read_text().split(";"):
        s = "\n".join(l for l in part.split("\n") if not l.strip().startswith("--")).strip()
        if s:
            out.append(s)
    return out


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def test_at_risk_cte_is_the_sellable_select():
    sellable_insert = _statements("sellable.sql")[1]
    head, select = sellable_insert.split("\n", 1)
    assert head == "INSERT INTO `stock.sellable`", head
    at_risk_insert = _statements("cart_at_risk.sql")[1]
    m = re.search(r"WITH sellable AS \((.*?)\n\),\s*latest_cart_item AS", at_risk_insert, re.S)
    assert m, "cart_at_risk.sql must start its INSERT with the CTE `sellable` (copy of the sellable.sql SELECT)"
    assert _norm(m.group(1)) == _norm(select), "CTE `sellable` in cart_at_risk.sql differs from the SELECT in sellable.sql"


def test_at_risk_insert_does_not_read_the_sellable_topic():
    at_risk_insert = _statements("cart_at_risk.sql")[1]
    assert "`stock.sellable`" not in at_risk_insert, "the offers query must not read stock.sellable back from Kafka"
    assert "FROM `inventory.state`" in at_risk_insert
    assert "JOIN sellable AS s" in at_risk_insert


def test_terraform_runs_one_statement_set_when_offers_is_on():
    # Built from the two INSERTs, under its own key, so a layer toggle is create-new + delete-old.
    assert 'offers_statement_set = contains(keys(local.flink_dml_each), "sellable-1") && contains(keys(local.flink_dml_each), "offers-1")' in TF
    assert 'contains(["sellable-1", "offers-1"], k)) }' in TF
    for line in ('"EXECUTE STATEMENT SET",', '"BEGIN",', '"${lookup(local.flink_dml_each, "sellable-1", "")};",',
                 '"${lookup(local.flink_dml_each, "offers-1", "")};",', '"END",'):
        assert line in TF, line
    assert '{ for k in ["offers-set"] : k => join("\\n", [' in TF
    # Runs with watermark alignment off (key prefix "offers") and in the late DML wave (prefix "offers-").
    assert re.search(r'no_alignment_files\s*=\s*\[[^\]]*"offers"', TF)


def test_flink_check_describes_the_statement_set():
    check = (ROOT / "compose" / "scripts" / "flink-check.sh").read_text()
    assert 'stmt="${CTX}-offers-set"' in check
