import pytest

from stock_projector.core import ProjectorError
from stock_projector.projector import Projector, Record
from stock_projector.store import RedisStore
from conftest import fixture


class Events(list):
    pass


class FakeSource:
    def __init__(self, events, records):
        self.events, self.records = events, list(records)

    def poll_batch(self, max_records):
        return [self.records.pop(0)] if self.records else []   # one at a time: the per-record tests

    def commit(self, rec):
        self.events.append(("commit", rec.offset))


class FakePublisher:
    def __init__(self, events, fail=False):
        self.events, self.fail, self.published = events, fail, []

    def confirm(self):
        pass

    def produce(self, key, value):
        if self.fail:
            raise ProjectorError("publish_failed", "delivery error: boom")
        self.published.append((key, value))
        self.events.append(("publish", value["revision"]))


class FakeMovements:
    def __init__(self, events, fail=False):
        self.events, self.fail, self.published = events, fail, []

    def confirm(self):
        pass

    def produce(self, key, value):
        if self.fail:
            raise ProjectorError("publish_failed", "delivery error: movements boom")
        self.published.append((key, value))
        self.events.append(("movement", value["revision"]))


class FakeMetrics:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


class SpyStore(RedisStore):
    def __init__(self, client, events, crash_after_apply=False):
        super().__init__(client)
        self.events, self.crash = events, crash_after_apply

    def apply(self, change, applied_at_ms):
        r = super().apply(change, applied_at_ms)
        self.events.append(("apply", change.revision))
        if self.crash:
            raise RuntimeError("crash after redis, before publish")
        return r


def rec(name, offset):
    return Record("inventory.cdc", 0, offset, fixture(name))


def build(redis_client, records, *, fail_publish=False, crash=False, stores=("S03",), fail_movements=False):
    events = Events()
    store = SpyStore(redis_client, events, crash)
    pub = FakePublisher(events, fail_publish)
    src = FakeSource(events, records)
    m = FakeMetrics()
    mov = FakeMovements(events, fail_movements)
    p = Projector(store, pub, src, m, stores, movements=mov, clock=lambda: 1791543721000)
    p.mov = mov
    return p, events, pub, m


def run_until_empty(p, src):
    return p.run(lambda: not src.records)


def test_order_apply_publish_commit_and_snapshot_ready(redis_client):
    p, events, pub, m = build(redis_client, [rec("snapshot_last", 0), rec("update", 1)])
    assert run_until_empty(p, p.source) == 0
    assert events == [("apply", 1001), ("publish", 1001), ("movement", 1001), ("commit", 0),
                      ("apply", 1043), ("publish", 1043), ("movement", 1043), ("commit", 1)]
    assert redis_client.hgetall("stock:n1:meta")[b"ready"] == b"1"
    assert ("ready", True) in m.calls
    assert ("record", "r", "applied") in m.calls
    assert ("apply_delay", 0.877, False) in m.calls  # 1791543721000 - 1791543720123


def test_ready_not_set_before_snapshot_end(redis_client):
    p, _, _, _ = build(redis_client, [rec("snapshot_mid", 0)])
    run_until_empty(p, p.source)
    assert redis_client.hget("stock:n1:meta", "ready") == b"0"


def test_stale_record_publishes_current_state(redis_client):
    p, events, pub, m = build(redis_client, [rec("update", 0), rec("update_older", 1)])
    run_until_empty(p, p.source)
    assert pub.published[1][1]["revision"] == 1043 and pub.published[1][1]["quantity"] == 1
    assert ("record", "u", "stale_or_duplicate") in m.calls
    assert ("commit", 1) in events


def test_duplicate_redelivery_republishes_current_state(redis_client):
    p, _, pub, _ = build(redis_client, [rec("update", 0), rec("update", 0)])
    run_until_empty(p, p.source)
    assert pub.published[0][1] == pub.published[1][1]


def test_probe_published_with_is_probe(redis_client):
    p, _, pub, m = build(redis_client, [rec("probe", 0)])
    run_until_empty(p, p.source)
    assert pub.published[0][1]["is_probe"] is True
    assert ("apply_delay", 0.877, True) in m.calls


def test_crash_after_redis_before_publish_does_not_commit_then_redelivery_republishes(redis_client):
    p, events, pub, m = build(redis_client, [rec("update", 0)], crash=True)
    assert p.run(lambda: False) == 1
    assert events == [("apply", 1043)]  # no publish, no commit
    assert ("error", "unexpected") in m.calls
    # restart: same record redelivered; Redis already holds it, state is still published
    p2, events2, pub2, _ = build(redis_client, [rec("update", 0)])
    run_until_empty(p2, p2.source)
    # no movement on redelivery: Redis already applied it, so the change is stale. Known, accepted gap (the movement
    # of a crash between Redis and inventory.state is lost; the demand estimate tolerates it, see README).
    assert events2 == [("apply", 1043), ("publish", 1043), ("commit", 0)]
    assert pub2.published[0][1]["revision"] == 1043


def test_publish_failure_does_not_commit_and_closes_readiness(redis_client):
    p, events, _, m = build(redis_client, [rec("snapshot_last", 0)], fail_publish=True)
    assert p.run(lambda: False) == 1
    assert ("commit", 0) not in events
    assert ("error", "publish_failed") in m.calls
    meta = redis_client.hgetall("stock:n1:meta")
    assert meta[b"ready"] == b"0" and meta[b"ready_reason"] == b"publish_failed"


@pytest.mark.parametrize("name,reason", [
    ("delete_op", "unsupported_op"), ("null_after", "missing_after"),
    ("missing_field", "missing_field"), ("revision_too_big", "revision_range"),
])
def test_contract_violation_stops_without_commit_or_publish(redis_client, name, reason):
    p, events, pub, m = build(redis_client, [rec("update", 0), rec(name, 1), rec("create", 2)])
    assert p.run(lambda: False) == 1
    assert [e for e in events if e[0] == "commit"] == [("commit", 0)]
    assert len(pub.published) == 1  # later record never processed
    assert ("error", reason) in m.calls
    meta = redis_client.hgetall("stock:n1:meta")
    assert meta[b"ready"] == b"0" and meta[b"ready_reason"] == reason.encode()


def test_redis_error_is_fatal_with_reason(redis_client, monkeypatch):
    import redis
    p, events, _, m = build(redis_client, [rec("update", 0)])
    p.start()
    monkeypatch.setattr(p.store, "_apply", lambda **kw: (_ for _ in ()).throw(redis.ConnectionError("down")))
    assert p.run(lambda: False) == 1
    assert ("error", "redis_error") in m.calls
    assert not any(e[0] == "commit" for e in events)


def env_for(store, snapshot, op="r", product="P0001", revision=1):
    e = fixture("snapshot_last")
    e["after"].update(store_id=store, product_id=product, revision=revision)
    e["op"], e["source"]["snapshot"] = op, snapshot
    return Record("inventory.cdc", 0, revision, e)


def meta(client):
    return {k.decode(): v.decode() for k, v in client.hgetall("stock:n1:meta").items()}


def test_ready_only_when_every_store_snapshot_is_done(redis_client):
    stores = ("S01", "S02", "S03")
    recs = [env_for("S01", "last", revision=1), env_for("S02", "true", revision=2), env_for("S03", "last", revision=3)]
    p, _, _, m = build(redis_client, recs, stores=stores)
    p.start()
    p.process(p.source.records.pop(0))
    assert meta(redis_client)["ready"] == "0" and meta(redis_client)["snapshot_done:S01"] == "1"
    p.process(p.source.records.pop(0))  # S02 mid-snapshot: not done
    assert "snapshot_done:S02" not in meta(redis_client)
    p.process(p.source.records.pop(0))
    assert meta(redis_client)["ready"] == "0" and meta(redis_client)["snapshot_done:S03"] == "1"
    p.process(env_for("S02", "false", op="u", revision=4))  # first live record proves S02's snapshot is over
    assert meta(redis_client)["ready"] == "1" and meta(redis_client)["snapshot_done:S02"] == "1"
    assert m.calls.count(("ready", True)) == 1


def test_done_stores_survive_restart(redis_client):
    stores = ("S01", "S02")
    p, _, _, _ = build(redis_client, [env_for("S01", "last")], stores=stores)
    run_until_empty(p, p.source)
    p2, _, _, m2 = build(redis_client, [env_for("S02", "last", revision=2)], stores=stores)
    p2.start()
    assert p2.done == {"S01"} and ("ready", False) in m2.calls
    p2.process(p2.source.records.pop(0))
    assert meta(redis_client)["ready"] == "1"


def test_probe_is_per_store(redis_client):
    p, _, pub, _ = build(redis_client, [env_for("S02", "false", op="u", product="__probe__")], stores=("S02",))
    run_until_empty(p, p.source)
    assert pub.published[0][1]["is_probe"] is True
    assert pub.published[0][0] == {"store_id": "S02", "product_id": "__probe__"}


def reason_rec(reason, quantity, revision, op="u", product="P0001", snapshot="false"):
    e = fixture("update")
    e["after"].update(product_id=product, quantity=quantity, revision=revision, change_reason=reason)
    e["op"], e["source"]["snapshot"] = op, snapshot
    return Record("inventory.cdc", 0, revision, e)


def movements(p):
    return [v for _, v in p.mov.published]


def test_movements_kinds_and_deltas(redis_client):
    recs = [
        reason_rec("seed", 10, 1, op="r", snapshot="last"),   # new position, snapshot -> ADJUST from 0
        reason_rec("sale", 8, 2),                              # SALE -2
        reason_rec("restock", 18, 3),                          # RESTOCK +10
        reason_rec("reset", 30, 4),                            # reset upsert looks like a restock -> ADJUST
        reason_rec(None, 25, 5),                               # no reason: delta sign -> SALE
        reason_rec(None, 26, 6),                               # no reason: RESTOCK
        reason_rec("sale", 26, 7),                             # delta 0 -> skipped
    ]
    p, events, pub, _ = build(redis_client, recs)
    assert run_until_empty(p, p.source) == 0
    got = [(m["kind"], m["qty_before"], m["qty_after"], m["delta"], m["revision"]) for m in movements(p)]
    assert got == [("ADJUST", 0, 10, 10, 1), ("SALE", 10, 8, -2, 2), ("RESTOCK", 8, 18, 10, 3),
                   ("ADJUST", 18, 30, 12, 4), ("SALE", 30, 25, -5, 5), ("RESTOCK", 25, 26, 1, 6)]
    assert movements(p)[0]["changed_at_ms"] == 1791543720123
    assert p.mov.published[1][0] == {"store_id": "S03", "product_id": "P0001"}
    assert len(pub.published) == 7  # inventory.state still gets every record


def test_create_is_adjust_even_with_sale_reason(redis_client):
    p, _, _, _ = build(redis_client, [reason_rec("sale", 5, 1, op="c")])
    run_until_empty(p, p.source)
    assert movements(p)[0]["kind"] == "ADJUST"


def test_no_movement_for_stale_duplicate_or_probe(redis_client):
    p, _, pub, _ = build(redis_client, [rec("update", 0), rec("update", 1), rec("update_older", 2), rec("probe", 3)])
    run_until_empty(p, p.source)
    assert [m["revision"] for m in movements(p)] == [1043]  # one applied update; probe and repeats publish none
    assert len(pub.published) == 4


def test_movement_failure_does_not_commit(redis_client):
    p, events, _, m = build(redis_client, [rec("update", 0)], fail_movements=True)
    assert p.run(lambda: False) == 1
    assert ("commit", 0) not in events
    assert ("error", "publish_failed") in m.calls
    assert events == [("apply", 1043), ("publish", 1043)]


def test_unknown_change_reason_is_a_contract_violation(redis_client):
    p, events, _, m = build(redis_client, [reason_rec("bogus", 5, 1)])
    assert p.run(lambda: False) == 1
    assert ("error", "invalid_field") in m.calls and not events


def test_restart_after_publish_failure_reopens_readiness_after_first_committed_record(redis_client):
    stores = ("S01",)
    p, _, _, _ = build(redis_client, [env_for("S01", "last")], stores=stores)
    run_until_empty(p, p.source)
    assert meta(redis_client)["ready"] == "1"
    p2, events2, _, _ = build(redis_client, [env_for("S01", "false", op="u", revision=2)], stores=stores, fail_publish=True)
    assert p2.run(lambda: False) == 1
    assert meta(redis_client)["ready"] == "0" and meta(redis_client)["ready_reason"] == "publish_failed"
    p3, _, _, m3 = build(redis_client, [env_for("S01", "false", op="u", revision=2)], stores=stores)
    p3.start()
    assert meta(redis_client)["ready"] == "0" and ("ready", False) in m3.calls   # not before a record succeeded
    p3.process(p3.source.records.pop(0))
    assert meta(redis_client)["ready"] == "1" and meta(redis_client)["ready_reason"] == ""
    assert ("ready", True) in m3.calls


def test_batch_applies_and_publishes_in_order_then_one_commit(redis_client):
    p, events, pub, m = build(redis_client, [rec("snapshot_last", 0), rec("update", 1)])
    p.start()
    p.process_batch([p.source.records.pop(0), p.source.records.pop(0)])
    assert events == [("apply", 1001), ("publish", 1001), ("movement", 1001),
                      ("apply", 1043), ("publish", 1043), ("movement", 1043), ("commit", 1)]
    assert redis_client.hgetall("stock:n1:meta")[b"ready"] == b"1"


def test_batch_publish_failure_commits_nothing(redis_client):
    p, events, _, _ = build(redis_client, [rec("snapshot_last", 0), rec("update", 1)], fail_publish=True)
    p.start()
    with pytest.raises(ProjectorError):
        p.process_batch([p.source.records.pop(0), p.source.records.pop(0)])
    assert not [e for e in events if e[0] == "commit"]
