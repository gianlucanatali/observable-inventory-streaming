"""Processing loop: validate -> Redis apply -> publish current state (delivery awaited) -> commit offset.

Micro-batches: every record already fetched (up to BATCH_MAX) is applied and queued for publishing in order, then
one flush awaits every delivery and one commit stores the batch's last offset. A failure commits nothing of the
batch; redelivery is idempotent (stale/duplicate records republish the current state). One round trip per batch
instead of three per record: about 80 ms per record against Confluent Cloud from EC2 (2026-10-04)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from ddtrace import tracer

from .core import ProjectorError, build_key, build_movement, build_movement_key, build_state, parse_envelope
from .metrics import Metrics
from .store import RedisStore, now_ms

log = logging.getLogger("stock_projector")


@dataclass(frozen=True)
class Record:
    topic: str
    partition: int
    offset: int
    value: Any
    raw: Any = None  # the transport message, handed back to commit()


BATCH_MAX = 500


class Source(Protocol):
    def poll_batch(self, max_records: int) -> list[Record]: ...
    def commit(self, record: Record) -> None: ...


class Publisher(Protocol):
    def produce(self, key: dict, value: dict) -> None:
        """Queue one record; raise ProjectorError('publish_failed', ...) if it cannot be queued."""

    def confirm(self) -> None:
        """Return only after the broker acknowledged every queued record; raise ProjectorError otherwise."""


class Projector:
    def __init__(self, store: RedisStore, publisher: Publisher, source: Source, metrics: Metrics,
                 stores: Iterable[str], *, movements: Publisher, clock: Callable[[], int] = now_ms):
        self.store, self.publisher, self.source, self.metrics, self.clock = store, publisher, source, metrics, clock
        self.movements = movements
        self.stores = tuple(stores)
        if not self.stores:
            raise ValueError("Projector needs at least one store to wait for")
        self.done: set[str] = set()
        self._reopen = False   # snapshots were done before this start: ready reopens after the first committed record

    @property
    def snapshot_done(self) -> bool:
        return all(s in self.done for s in self.stores)

    def start(self) -> None:
        self.done = self.store.init_namespace() & set(self.stores)
        # A restart after a failure (e.g. publish_failed while Confluent Cloud dropped connections) finds the snapshots
        # done but ready=0. Readiness reopens only once a record has been applied, published and committed again.
        self._reopen = self.snapshot_done
        self.metrics.ready(False if self._reopen else self.snapshot_done)
        log.info("projector started", extra={"ctx": {
            "namespace": self.store.ns, "snapshot_done": self.snapshot_done, "stores_done": sorted(self.done)}})

    def process(self, rec: Record) -> None:
        self.process_batch([rec])

    def process_batch(self, recs: list[Record]) -> None:
        done = []
        for rec in recs:
            change = parse_envelope(rec.value)
            with tracer.trace("stock.project", service=None, resource=change.op) as span:
                span.set_tag("stock.revision", change.revision)
                result = self.store.apply(change, self.clock())
                self.publisher.produce(build_key(change), build_state(change, result))
                movement = build_movement(change, result)
                if movement is not None:  # only what Redis applied, after inventory.state, before the offset commit
                    self.movements.produce(build_movement_key(change), movement)
                if change.completes_snapshot and change.store_id in self.stores and change.store_id not in self.done:
                    self.store.mark_store_snapshot_done(change.store_id)
                    self.done.add(change.store_id)
                    log.info("store snapshot complete", extra={"ctx": {
                        "store_id": change.store_id, "offset": rec.offset, "pending": [s for s in self.stores if s not in self.done]}})
                    if self.snapshot_done:
                        self._reopen = True   # ready once this batch is confirmed and committed
            done.append((rec, change, result))
        self.publisher.confirm()
        self.movements.confirm()
        self.source.commit(recs[-1])
        if self._reopen:
            self._reopen = False
            self.store.mark_ready()
            self.metrics.ready(True)
            log.info("serving view ready (snapshots complete, batch committed)", extra={"ctx": {"offset": recs[-1].offset}})
        for rec, change, result in done:
            outcome = "applied" if result.applied else "stale_or_duplicate"
            self.metrics.record(change.op, outcome)
            if result.applied:
                self.metrics.apply_delay((result.applied_at_ms - result.changed_at_ms) / 1000.0, change.is_probe)
            log.info("processed", extra={"ctx": {
                "store_id": change.store_id, "product_id": change.product_id, "revision": change.revision,
                "offset": rec.offset, "op": change.op, "outcome": outcome, "batch": len(recs)}})

    def fail(self, rec: Record | None, reason: str, detail: str) -> None:
        self.metrics.error(reason)
        self.metrics.ready(False)
        log.error("projector stopping, offset not committed", extra={"ctx": {
            "reason": reason, "detail": detail,
            "offset": None if rec is None else rec.offset, "partition": None if rec is None else rec.partition}})
        try:
            self.store.mark_not_ready(reason)
        except ProjectorError as e:  # Redis itself may be the failure; say so, the exit is non-zero anyway
            log.error("could not close readiness in Redis", extra={"ctx": {"reason": e.reason, "detail": e.detail}})

    def run(self, should_stop: Callable[[], bool] = lambda: False) -> int:
        """Returns 0 on a requested stop, 1 on a fatal error (nothing committed for the failing record)."""
        rec: Record | None = None
        try:
            self.start()
            while not should_stop():
                recs = self.source.poll_batch(BATCH_MAX)
                if not recs:
                    continue
                rec = recs[-1]
                self.process_batch(recs)
        except ProjectorError as e:
            self.fail(rec, e.reason, e.detail)
            return 1
        except Exception as e:
            self.fail(rec, "unexpected", f"{type(e).__name__}: {e}")
            log.exception("unexpected error")
            return 1
        return 0
