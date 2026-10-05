"""Pure functions: Debezium envelope -> StockChange, StockChange + script result -> StockState record."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_REVISION = 2**53  # exclusive; exact in Lua numbers and JSON doubles
PROBE_ID = "__probe__"
SOURCE_OPS = {"r": "SNAPSHOT", "c": "CREATE", "u": "UPDATE"}
CHANGE_REASONS = {"sale": "SALE", "restock": "RESTOCK", "reset": "ADJUST", "seed": "ADJUST"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ProjectorError(Exception):
    """Fatal: stop without committing. `reason` is the bounded label used on stock.projector.errors."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class ContractViolation(ProjectorError):
    pass


@dataclass(frozen=True)
class StockChange:
    store_id: str
    product_id: str
    quantity: int
    revision: int
    deleted: bool
    changed_at_ms: int
    op: str  # r, c, u
    snapshot: str | None  # Debezium source.snapshot: "true", "last", "false", ...
    change_reason: str | None = None  # source column: sale, restock, reset, seed (null = writer did not say)

    @property
    def is_probe(self) -> bool:
        return self.product_id == PROBE_ID

    @property
    def completes_snapshot(self) -> bool:
        """Debezium marks the last snapshot record 'last'; a live (non-snapshot) record also proves it is over."""
        return self.snapshot == "last" or self.snapshot in (None, "false")


@dataclass(frozen=True)
class ApplyResult:
    """Current stored state returned by the Lua script."""
    applied: bool
    quantity: int
    revision: int
    deleted: bool
    changed_at_ms: int
    applied_at_ms: int
    previous_quantity: int | None = None  # quantity before this change; None when the position was new or not applied


def _int(after: dict, name: str) -> int:
    v = after[name]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ContractViolation("invalid_field", f"{name} must be an integer, got {v!r}")
    return v


def parse_zoned_timestamp_ms(value: Any) -> int:
    if not isinstance(value, str):
        raise ContractViolation("invalid_field", f"changed_at must be an ISO-8601 string, got {value!r}")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ContractViolation("invalid_field", f"changed_at {value!r} is not ISO-8601: {e}") from e
    if dt.tzinfo is None:
        raise ContractViolation("invalid_field", f"changed_at {value!r} has no timezone")
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def parse_envelope(envelope: Any) -> StockChange:
    if not isinstance(envelope, dict):
        raise ContractViolation("invalid_envelope", f"value is {type(envelope).__name__}, not a record")
    op = envelope.get("op")
    if op not in SOURCE_OPS:
        raise ContractViolation("unsupported_op", f"op={op!r}, only r/c/u are allowed")
    after = envelope.get("after")
    if not isinstance(after, dict):
        raise ContractViolation("missing_after", f"op={op} without an after image")
    missing = [f for f in ("store_id", "product_id", "quantity", "revision", "changed_at", "deleted") if f not in after]
    if missing:
        raise ContractViolation("missing_field", f"after lacks {missing}")
    for f in ("store_id", "product_id"):
        if not isinstance(after[f], str) or not after[f]:
            raise ContractViolation("invalid_field", f"{f} must be a non-empty string, got {after[f]!r}")
    quantity = _int(after, "quantity")
    if quantity < 0 or quantity > 2**31 - 1:
        raise ContractViolation("invalid_field", f"quantity {quantity} outside 0..2^31-1")
    revision = _int(after, "revision")
    if not 0 < revision < MAX_REVISION:
        raise ContractViolation("revision_range", f"revision {revision} outside 1..2^53-1")
    if not isinstance(after["deleted"], bool):
        raise ContractViolation("invalid_field", f"deleted must be boolean, got {after['deleted']!r}")
    reason = after.get("change_reason")
    if reason is not None and reason not in CHANGE_REASONS:
        raise ContractViolation("invalid_field", f"change_reason {reason!r} is not one of {sorted(CHANGE_REASONS)}")
    source = envelope.get("source")
    snapshot = source.get("snapshot") if isinstance(source, dict) else None
    return StockChange(
        store_id=after["store_id"], product_id=after["product_id"], quantity=quantity,
        revision=revision, deleted=after["deleted"],
        changed_at_ms=parse_zoned_timestamp_ms(after["changed_at"]),
        op=op, snapshot=None if snapshot is None else str(snapshot), change_reason=reason,
    )


def build_state(change: StockChange, result: ApplyResult) -> dict:
    """StockState value: the CURRENT accepted state for the key (the stored one when the change was stale).

    source_op is the op of the record being processed. applied_at is the stored acceptance time, so a
    redelivery republishes an identical state.
    """
    return {
        "store_id": change.store_id,
        "product_id": change.product_id,
        "quantity": result.quantity,
        "revision": result.revision,
        "deleted": result.deleted,
        "is_probe": change.is_probe,
        "changed_at": result.changed_at_ms,
        "applied_at": result.applied_at_ms,
        "source_op": SOURCE_OPS[change.op],
    }


def build_key(change: StockChange) -> dict:
    return {"store_id": change.store_id, "product_id": change.product_id}


def build_movement(change: StockChange, result: ApplyResult) -> dict | None:
    """StockMovement value (contract section 13), or None when there is nothing to publish.

    None for: a change Redis did not apply (stale/duplicate), the probe, and delta 0. Snapshot and create
    records are ADJUST. For an UPDATE the source's change_reason decides (sale/restock/reset/seed); when the
    writer gave none, the sign of the delta does (< 0 SALE, > 0 RESTOCK). A new position starts from 0.
    """
    if not result.applied or change.is_probe:
        return None
    before = 0 if result.previous_quantity is None else result.previous_quantity
    delta = result.quantity - before
    if delta == 0:
        return None
    if change.op != "u":
        kind = "ADJUST"
    elif change.change_reason is not None:
        kind = CHANGE_REASONS[change.change_reason]
    else:
        kind = "SALE" if delta < 0 else "RESTOCK"
    return {
        "store_id": change.store_id, "product_id": change.product_id,
        "qty_before": before, "qty_after": result.quantity, "delta": delta, "kind": kind,
        "revision": result.revision, "changed_at_ms": result.changed_at_ms,
    }


def build_movement_key(change: StockChange) -> dict:
    return build_key(change)
