"""The delivery cycle, independent of PostgreSQL and Redis (adapters are injected; see adapters.py)."""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, replace
from typing import Callable, Protocol

from ddtrace import tracer

from .config import parse_lead_time

log = logging.getLogger("supplier_sim")
ETA_PREFIX = "restock:eta:"
DEFAULT_JITTER_PCT = 20.0  # used (with an error log) only when procurement_config has no lead_time_jitter_pct row
FACTOR_MIN, FACTOR_MAX = 0.5, 2.0


def product_factor(product_id: str) -> float:
    """Deterministic per-product lead-time factor in [0.5, 2.0] from sha256(product_id)."""
    frac = int.from_bytes(hashlib.sha256(product_id.encode()).digest()[:8], "big") / 2**64
    return FACTOR_MIN + (FACTOR_MAX - FACTOR_MIN) * frac


def draw_lead_time_s(request_id: str, product_id: str, base_s: int, jitter_pct: float) -> float:
    """lead_time_s x factor(product) x (1 +- jitter): jitter uniform in +-jitter_pct %, deterministic per order
    (seeded from the request id) so a restart before the draw is persisted draws the same value."""
    u = random.Random(hashlib.sha256(request_id.encode()).digest()).uniform(-1.0, 1.0)
    return base_s * product_factor(product_id) * (1.0 + u * jitter_pct / 100.0)


def parse_jitter_pct(raw: str, where: str) -> float:
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"{where}: jitter {raw!r} is not a number (percent)") from None
    if not 0.0 <= v <= 100.0:
        raise ValueError(f"{where}: jitter {v} is outside 0..100 percent")
    return v


@dataclass(frozen=True)
class Order:
    request_id: str
    store_id: str
    product_id: str
    quantity: int
    requested_at_ms: int
    drawn_s: float | None = None          # lead time drawn when the order was first seen (None = not drawn yet)
    base_at_draw_s: int | None = None     # the base lead_time_s in force at that draw


def due_ms(o: Order, base_s: int, compression: float = 1.0) -> int:
    """requested_at + drawn x (current base / base at draw) / C: a base change rescales every open order
    consistently (the factor and jitter of the order are kept). Needs o.drawn_s.
    Drawn and base are BUSINESS seconds; the real wait is business / C (demo clock, contracts 13b), so a change
    of C also applies to open orders. The result is a real epoch time (restock:eta stays real)."""
    if o.drawn_s is None or o.base_at_draw_s is None:
        raise ValueError(f"order {o.request_id} has no drawn lead time")
    ratio = base_s / o.base_at_draw_s if o.base_at_draw_s > 0 else 1.0
    return o.requested_at_ms + int(o.drawn_s * ratio / compression * 1000)


class Procurement(Protocol):
    def lead_time_raw(self) -> str | None: ...        # None when the config row is missing
    def jitter_pct_raw(self) -> str | None: ...       # procurement_config.lead_time_jitter_pct, None when missing
    def open_orders(self) -> list[Order]: ...         # not delivered, not cancelled
    def set_drawn(self, request_id: str, drawn_s: float, base_s: int) -> None: ...
    def mark_delivered(self, request_id: str) -> None: ...


class Sources(Protocol):
    def restock(self, store_id: str, product_id: str, quantity: int) -> None: ...  # raises on any failure


class EtaStore(Protocol):
    def set_eta(self, product_id: str, eta_ms: int) -> None: ...
    def clear_eta(self, product_id: str) -> None: ...
    def eta_products(self) -> set[str]: ...           # products that currently have a restock:eta key


DEFAULT_TIME_COMPRESSION = 60.0  # used (with an error log, once) only when Redis demo:config has no time_compression


class Supplier:
    def __init__(self, procurement: Procurement, sources: Sources, etas: EtaStore, metrics,
                 default_lead_time_s: int, clock_ms: Callable[[], int], compression: Callable[[], float]):
        """`compression()` returns the demo clock C (business seconds per real second), read live every cycle."""
        self._p, self._s, self._e, self._m = procurement, sources, etas, metrics
        self._compression = compression
        self._default = default_lead_time_s
        self._clock_ms = clock_ms
        self._jitter_default_logged = False

    def lead_time_s(self) -> int:
        raw = self._p.lead_time_raw()
        if raw is None:
            log.error("procurement_config has no lead_time_s row: using DEFAULT_LEAD_TIME_S",
                      extra={"ctx": {"default_lead_time_s": self._default}})
            return self._default
        return parse_lead_time(raw, "procurement_config.lead_time_s")

    def jitter_pct(self) -> float:
        raw = self._p.jitter_pct_raw()
        if raw is None:
            if not self._jitter_default_logged:
                self._jitter_default_logged = True
                log.error("procurement_config has no lead_time_jitter_pct row: using the default",
                          extra={"ctx": {"default_jitter_pct": DEFAULT_JITTER_PCT}})
            return DEFAULT_JITTER_PCT
        return parse_jitter_pct(raw, "procurement_config.lead_time_jitter_pct")

    def _drawn(self, o: Order, base_s: int, jitter: float) -> Order:
        """Return the order with its lead time drawn; the first sighting draws and stores it."""
        if o.drawn_s is not None and o.base_at_draw_s is not None:
            return o
        drawn = draw_lead_time_s(o.request_id, o.product_id, base_s, jitter)
        try:
            self._p.set_drawn(o.request_id, drawn, base_s)
        except Exception as exc:  # keep the in-memory draw; the next pass draws again from the same seed
            log.error(f"storing the drawn lead time of {o.request_id} failed: {exc!r}",
                      extra={"ctx": {"request_id": o.request_id, "drawn_s": drawn}})
        log.info("lead time drawn", extra={"ctx": {
            "request_id": o.request_id, "product": o.product_id, "base_s": base_s,
            "factor": round(product_factor(o.product_id), 3), "jitter_pct": jitter, "drawn_s": round(drawn, 1)}})
        return replace(o, drawn_s=drawn, base_at_draw_s=base_s)

    def cycle(self) -> None:
        """One pass. Errors of a single delivery are logged and the order stays open (retried next pass).
        Errors reading procurement or the lead time propagate to the caller, which logs and retries."""
        lead_s = self.lead_time_s()
        jitter = self.jitter_pct()
        c = self._compression()
        if not c > 0:
            raise ValueError(f"time_compression must be > 0, got {c!r}")
        now = self._clock_ms()
        open_orders = [self._drawn(o, lead_s, jitter) for o in self._p.open_orders()]
        remaining: list[Order] = []
        for o in open_orders:
            if due_ms(o, lead_s, c) > now:
                remaining.append(o)
                continue
            if not self._deliver(o):
                remaining.append(o)
        self._publish_etas(remaining, lead_s, c)
        self._m.orders_open(len(remaining))
        self._m.lead_time(lead_s)

    def _deliver(self, o: Order) -> bool:
        ctx = {"request_id": o.request_id, "store": o.store_id, "product": o.product_id, "quantity": o.quantity}
        with tracer.trace("restock.deliver", service=None, resource=o.store_id) as span:
            span.set_tag("request_id", o.request_id)
            span.set_tag("store", o.store_id)
            span.set_tag("product_id", o.product_id)
            try:
                self._s.restock(o.store_id, o.product_id, o.quantity)
            except Exception as exc:
                span.set_exc_info(type(exc), exc, exc.__traceback__)
                log.error(f"restock in the source of {o.store_id} failed, order stays open: {exc!r}",
                          extra={"ctx": ctx})
                return False
            try:
                self._p.mark_delivered(o.request_id)
            except Exception as exc:
                span.set_exc_info(type(exc), exc, exc.__traceback__)
                log.error("restock applied but delivered_at not written: the order will be delivered "
                          f"again on the next pass (accepted for the demo): {exc!r}", extra={"ctx": ctx})
                return False
            log.info("delivered", extra={"ctx": ctx})
            self._m.delivered(o.store_id)
            return True

    def _publish_etas(self, remaining: list[Order], lead_s: int, c: float) -> None:
        eta: dict[str, int] = {}
        for o in remaining:
            due = due_ms(o, lead_s, c)  # real epoch ms
            eta[o.product_id] = min(due, eta.get(o.product_id, due))
        try:
            for product, due in eta.items():
                self._e.set_eta(product, due)
            for product in self._e.eta_products() - eta.keys():
                self._e.clear_eta(product)
        except Exception as exc:
            log.error(f"updating restock:eta keys in Redis failed: {exc!r}")
