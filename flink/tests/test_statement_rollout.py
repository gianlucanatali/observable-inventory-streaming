"""Offline contract: a SQL or properties change to an INSERT statement replaces it.

The Confluent provider cannot update a running statement's properties in place (its update accepts only
`stopped`, `properties_sensitive`, `principal`, `compute_pool` and `credentials`). `statement` forces a new
resource; properties reach the replacement through a revision hash and replace_triggered_by.
"""
from pathlib import Path
import re


TF = Path(__file__).parents[2] / "terraform" / "cloud" / "flink_statements.tf"


def _resource(text: str, kind: str, name: str) -> str:
    match = re.search(rf'^resource "{kind}" "{name}" \{{\n(.*?)^\}}', text, re.MULTILINE | re.DOTALL)
    assert match, f"{TF.name} must define {kind}.{name}"
    return match.group(1)


def test_revision_hashes_statement_and_properties_of_every_dml_statement():
    revision = _resource(TF.read_text(), "terraform_data", "flink_dml_revision")
    assert "for_each = local.flink_dml_all" in revision
    assert 'statement = "${each.value};"' in revision
    assert "properties = local.dml_props[each.key]" in revision


def test_dml_and_dml_late_are_replaced_when_their_revision_changes():
    text = TF.read_text()
    for name in ("dml", "dml_late"):
        body = _resource(text, "confluent_flink_statement", name)
        assert 'statement      = "${each.value};"' in body, f"{name}: statement must match the revision input"
        assert "properties     = local.dml_props[each.key]" in body, f"{name}: properties must match the revision input"
        assert "replace_triggered_by = [terraform_data.flink_dml_revision[each.key]]" in body, name
        assert "create_before_destroy" not in body, f"{name}: the new statement reuses the name, so delete first"
