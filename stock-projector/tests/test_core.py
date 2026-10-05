import fastavro
import pytest

from stock_projector.core import ApplyResult, ContractViolation, build_key, build_state, parse_envelope
from conftest import CONTRACT_AVRO, fixture


def test_snapshot_last_parses_and_completes_snapshot():
    c = parse_envelope(fixture("snapshot_last"))
    assert (c.op, c.revision, c.quantity, c.completes_snapshot) == ("r", 1001, 7, True)
    assert c.changed_at_ms == 1791543720123  # 2026-10-09T11:02:00.123456Z truncated to ms


def test_mid_snapshot_does_not_complete_but_first_live_record_does():
    assert not parse_envelope(fixture("snapshot_mid")).completes_snapshot
    assert parse_envelope(fixture("create")).completes_snapshot


@pytest.mark.parametrize("name,op", [("create", "c"), ("update", "u")])
def test_ops_accepted(name, op):
    assert parse_envelope(fixture(name)).op == op


def test_probe_flag():
    assert parse_envelope(fixture("probe")).is_probe
    assert not parse_envelope(fixture("update")).is_probe
    assert parse_envelope(fixture("probe")).store_id == "S03"


def test_soft_delete_is_a_state_not_a_violation():
    assert parse_envelope(fixture("soft_delete")).deleted is True


@pytest.mark.parametrize("name,reason", [
    ("delete_op", "unsupported_op"), ("null_after", "missing_after"),
    ("missing_field", "missing_field"), ("revision_too_big", "revision_range"),
])
def test_contract_violations(name, reason):
    with pytest.raises(ContractViolation) as e:
        parse_envelope(fixture(name))
    assert e.value.reason == reason


@pytest.mark.parametrize("field,value", [
    ("changed_at", 1791543720123), ("changed_at", "2026-10-09T11:02:00"), ("changed_at", "yesterday"),
    ("revision", "1043"), ("revision", True), ("quantity", -1), ("deleted", 0), ("store_id", ""),
])
def test_invalid_fields(field, value):
    env = fixture("update")
    env["after"][field] = value
    with pytest.raises(ContractViolation) as e:
        parse_envelope(env)
    assert e.value.reason in ("invalid_field", "revision_range")


def test_non_dict_value():
    with pytest.raises(ContractViolation):
        parse_envelope(None)


def test_build_state_matches_contract_schemas():
    change = parse_envelope(fixture("update"))
    state = build_state(change, ApplyResult(False, 3, 1100, False, 1791543720000, 1791543721000))
    # current accepted state, not the (stale) incoming change; source_op is the incoming op
    assert (state["quantity"], state["revision"], state["source_op"]) == (3, 1100, "UPDATE")
    for schema_file, record in (("stock_state.avsc", state), ("stock_key.avsc", build_key(change))):
        schema = fastavro.parse_schema(__import__("json").loads((CONTRACT_AVRO / schema_file).read_text()))
        fastavro.validation.validate(record, schema)


def test_movement_validates_against_contract_schemas():
    from dataclasses import replace
    from stock_projector.core import build_movement
    change = replace(parse_envelope(fixture("update")), change_reason="sale")
    result = ApplyResult(True, 1, 1043, False, 1791543720123, 1791543721000, 4)
    movement = build_movement(change, result)
    assert movement["kind"] == "SALE" and movement["delta"] == -3
    for schema_file, record in (("stock_movement.avsc", movement), ("stock_movement_key.avsc", build_key(change))):
        schema = fastavro.parse_schema(__import__("json").loads((CONTRACT_AVRO / schema_file).read_text()))
        fastavro.validation.validate(record, schema)
