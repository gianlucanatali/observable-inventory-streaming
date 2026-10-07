"""Confirmed minimum stock: the same trust rule as the availability API (contracts section 5).

This is a deliberate copy of the rule in overlay/inventory-api/app/main.py (`store_feed` and the `confirmed_min`
block of `availability()`): the two services have separate images and build contexts, and the API's version is
interleaved with its response body. Both test suites run the shared case table
overlay/contracts/stock-trust-cases.json against their own code through Redis, so a change to one that is not
made in the other fails a test. Change both, and the table, together.

A store is live when the namespace is ready, its feed:status:{store} is `ok` and not older than 10 s, and its
position is present (`not_stocked` is live and counts 0). With every store live, confirmed_min is the Flink total
`sellable:{product_id}`; otherwise it is the live stores' sum capped by that total. A quiet store's last known
quantity is never counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

FEED_MAX_AGE_S = 10  # inventory-api Config.feed_max_age_s
UNCOMPUTABLE = ("not_ready", "redis_error", "not_found")  # confirmed_min is None for these


class MalformedStock(ValueError):
    """A serving-view hash that cannot be parsed (the API answers 500 for it)."""


@dataclass(frozen=True)
class StockAnswer:
    confirmed_min: int | None   # None when unknown_reason is one of UNCOMPUTABLE
    at_least: bool              # any store not live
    status: str                 # available | out_of_stock | unknown
    unknown_reason: str | None  # not_ready | not_found | stores_unknown | None (redis_error is the caller's)
    sellable: int | None        # the Flink total, for logs only


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


def _str_hash(h) -> dict[str, str]:
    return {_s(k): _s(v) for k, v in (h or {}).items()}


def feed_state(h: dict[str, str], now_ms: int, max_age_s: int = FEED_MAX_AGE_S) -> str:
    """feed:status:{store} hash -> ok|stale|unknown (missing, malformed or older than max_age_s = unknown)."""
    if not h:
        return "unknown"
    try:
        age_ms = now_ms - int(h["checked_at_ms"])
    except (KeyError, ValueError):
        return "unknown"
    if age_ms > max_age_s * 1000:
        return "unknown"
    return h["state"] if h.get("state") in ("ok", "stale", "unknown") else "unknown"


def answer(ready: bool, sellable_h: dict[str, str], positions: Sequence[dict[str, str]],
           feeds: Sequence[dict[str, str]], now_ms: int) -> StockAnswer:
    """The rule, on already-read (decoded) hashes; positions and feeds are aligned with the store list."""
    any_unknown, live_sum = False, 0
    for pos, fh in zip(positions, feeds):
        live, qty = False, 0
        if pos:
            try:
                q, _rev, deleted = int(pos["quantity"]), int(pos["revision"]), pos.get("deleted", "0") == "1"
            except (KeyError, ValueError) as e:
                raise MalformedStock(f"malformed position {pos!r}: {e}") from e
            qty = 0 if deleted else q
            live = ready and feed_state(fh, now_ms) == "ok"
        if live:
            live_sum += qty
        else:
            any_unknown = True
    if not ready:
        return StockAnswer(None, any_unknown, "unknown", "not_ready", None)
    if not sellable_h:
        return StockAnswer(None, any_unknown, "unknown", "not_found", None)
    try:
        sellable = int(sellable_h["sellable"])
    except (KeyError, ValueError) as e:
        raise MalformedStock(f"malformed sellable {sellable_h!r}: {e}") from e
    confirmed = min(live_sum, sellable) if any_unknown else sellable
    if confirmed > 0:
        return StockAnswer(confirmed, any_unknown, "available", None, sellable)
    if any_unknown:
        return StockAnswer(confirmed, True, "unknown", "stores_unknown", sellable)
    return StockAnswer(confirmed, False, "out_of_stock", None, sellable)


class ConfirmedStock:
    """Reads the same keys as the API in the same two pipelines. Raises redis errors and MalformedStock;
    the caller decides (offer-worker: not eligible, logged and counted)."""

    def __init__(self, redis_client, store_ids: Sequence[str], clock: Callable[[], float]):
        self._r, self._stores, self._clock = redis_client, tuple(store_ids), clock

    def read(self, product_id: str) -> StockAnswer:
        pipe = self._r.pipeline(transaction=False)
        pipe.get("stock:active_ns")
        pipe.hgetall(f"sellable:{product_id}")
        for s in self._stores:
            pipe.hgetall(f"feed:status:{s}")
        res = pipe.execute()
        ns, sellable_h, feeds = _s(res[0]), _str_hash(res[1]), [_str_hash(h) for h in res[2:]]
        now_ms = int(self._clock() * 1000)
        if not ns:
            return answer(False, sellable_h, [{}] * len(self._stores), feeds, now_ms)
        pipe = self._r.pipeline(transaction=False)
        pipe.hget(f"stock:{ns}:meta", "ready")
        for s in self._stores:
            pipe.hgetall(f"stock:{ns}:{s}:{product_id}")
        res = pipe.execute()
        ready = _s(res[0]) == "1"
        positions = [_str_hash(h) for h in res[1:]] if ready else [{}] * len(self._stores)
        return answer(ready, sellable_h, positions, feeds, now_ms)
