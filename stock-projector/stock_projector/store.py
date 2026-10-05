"""Thin Redis adapter: namespace, apply script, readiness meta."""
from __future__ import annotations

import time
from pathlib import Path

import redis

from .core import ApplyResult, ProjectorError, StockChange

_SCRIPT = (Path(__file__).parent / "apply.lua").read_text()
DEFAULT_NS = "n1"
SNAPSHOT_PREFIX = "snapshot_done:"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class RedisStore:
    def __init__(self, client: redis.Redis):
        self._r = client
        self._apply = client.register_script(_SCRIPT)  # SCRIPT LOAD / EVALSHA, reloaded on NOSCRIPT
        self.ns: str | None = None

    @property
    def _meta(self) -> str:
        return f"stock:{self.ns}:meta"

    def init_namespace(self) -> set[str]:
        """Read stock:active_ns; if absent initialise n1 with ready=0. Returns the stores whose snapshot is done."""
        try:
            active = self._r.get("stock:active_ns")
            if active is None:
                self._r.set("stock:active_ns", DEFAULT_NS)
                self.ns = DEFAULT_NS
                self._r.hset(self._meta, mapping={"ready": 0, "ready_reason": "starting", "snapshot_done": 0})
                return set()
            self.ns = active.decode() if isinstance(active, bytes) else active
            meta = self._r.hgetall(self._meta)
            done = set()
            for k, v in meta.items():
                k = k.decode() if isinstance(k, bytes) else k
                if k.startswith(SNAPSHOT_PREFIX) and v in (b"1", "1"):
                    done.add(k[len(SNAPSHOT_PREFIX):])
            return done
        except redis.RedisError as e:
            raise ProjectorError("redis_error", f"namespace init failed: {e}") from e

    def apply(self, change: StockChange, applied_at_ms: int) -> ApplyResult:
        key = f"stock:{self.ns}:{change.store_id}:{change.product_id}"
        try:
            r = self._apply(keys=[key, self._meta], args=[
                change.quantity, change.revision, int(change.deleted), change.changed_at_ms, applied_at_ms])
        except redis.RedisError as e:
            raise ProjectorError("redis_error", f"apply {key} rev={change.revision} failed: {e}") from e
        return ApplyResult(bool(r[0]), r[1], r[2], bool(r[3]), r[4], r[5], r[6])

    def mark_store_snapshot_done(self, store_id: str) -> None:
        self._set_meta({f"{SNAPSHOT_PREFIX}{store_id}": 1})

    def mark_ready(self) -> None:
        self._set_meta({"ready": 1, "ready_reason": "", "snapshot_done": 1})

    def mark_not_ready(self, reason: str) -> None:
        self._set_meta({"ready": 0, "ready_reason": reason})

    def _set_meta(self, mapping: dict) -> None:
        try:
            self._r.hset(self._meta, mapping=mapping)
        except redis.RedisError as e:
            raise ProjectorError("redis_error", f"meta update {mapping} failed: {e}") from e
