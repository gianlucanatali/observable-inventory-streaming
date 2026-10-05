from stock_projector.core import parse_envelope
from stock_projector.store import RedisStore
from conftest import fixture


def make(redis_client):
    s = RedisStore(redis_client)
    s.init_namespace()
    return s


def test_namespace_initialised_not_ready(redis_client):
    s = RedisStore(redis_client)
    assert s.init_namespace() == set()
    assert redis_client.get("stock:active_ns") == b"n1"
    assert redis_client.hget("stock:n1:meta", "ready") == b"0"


def test_existing_namespace_is_kept(redis_client):
    redis_client.set("stock:active_ns", "n7")
    s = RedisStore(redis_client)
    s.init_namespace()
    assert s.ns == "n7"


def test_newer_applies_and_updates_meta(redis_client):
    s = make(redis_client)
    r = s.apply(parse_envelope(fixture("update")), 5000)
    assert r.applied and (r.quantity, r.revision, r.applied_at_ms) == (1, 1043, 5000)
    h = redis_client.hgetall("stock:n1:S03:P0042")
    assert h[b"revision"] == b"1043" and h[b"deleted"] == b"0" and h[b"changed_at_ms"] == b"1791543720123"
    meta = redis_client.hgetall("stock:n1:meta")
    assert meta[b"last_revision"] == b"1043" and meta[b"last_applied_at_ms"] == b"5000"


def test_equal_revision_is_noop_returning_current(redis_client):
    s = make(redis_client)
    s.apply(parse_envelope(fixture("update")), 5000)
    r = s.apply(parse_envelope(fixture("update")), 9000)
    assert not r.applied and r.applied_at_ms == 5000 and r.revision == 1043


def test_older_revision_is_noop_returning_newer_state(redis_client):
    s = make(redis_client)
    s.apply(parse_envelope(fixture("update")), 5000)
    r = s.apply(parse_envelope(fixture("update_older")), 6000)
    assert not r.applied and (r.quantity, r.revision) == (1, 1043)
    assert redis_client.hget("stock:n1:meta", "last_revision") == b"1043"


def test_soft_delete_stored(redis_client):
    s = make(redis_client)
    s.apply(parse_envelope(fixture("update")), 5000)
    r = s.apply(parse_envelope(fixture("soft_delete")), 6000)
    assert r.applied and r.deleted and r.quantity == 0


def test_mark_ready_and_not_ready(redis_client):
    s = make(redis_client)
    s.mark_store_snapshot_done("S02")
    s.mark_ready()
    assert redis_client.hgetall("stock:n1:meta")[b"snapshot_done"] == b"1"
    s.mark_not_ready("missing_field")
    meta = redis_client.hgetall("stock:n1:meta")
    assert meta[b"ready"] == b"0" and meta[b"ready_reason"] == b"missing_field"
    assert RedisStore(redis_client).init_namespace() == {"S02"}  # per-store snapshot_done survives a restart


def test_previous_quantity_none_for_new_then_old_value(redis_client):
    s = make(redis_client)
    first = s.apply(parse_envelope(fixture("snapshot_last")), 5000)
    assert first.applied and first.previous_quantity is None
    second = s.apply(parse_envelope(fixture("update")), 6000)
    assert second.applied and second.previous_quantity == 7  # snapshot_last quantity
    stale = s.apply(parse_envelope(fixture("update_older")), 7000)
    assert not stale.applied and stale.previous_quantity is None
