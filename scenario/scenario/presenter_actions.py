"""Shared source-only presenter operations used by scenario CLI and demo-control."""
from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from .demo_config import read_param
from .procurement import cancel_open_orders, clear_eta_keys, count_open_orders, eta_keys
from .redisview import active_ns, reset, wait_for_product_zero
from .source import read_all, read_quantity, sell


def sell_out_sources(conns: dict, redis_client, product_id: str, progress: Callable[[str], None],
                     sleep: Callable[[float], None] = time.sleep, settle_timeout_s: float = 30,
                     interval_s: float = 0.5, clock: Callable[[], float] = time.time) -> None:
    """Sell each source position, then prove the serving positions and sellable view reached zero."""
    gap_s = read_param(redis_client, "sell_out_gap_s")
    stores = list(conns)
    for step, store in enumerate(stores, start=1):
        quantity = read_quantity(conns[store], store, product_id)
        if quantity:
            row = sell(conns[store], store, product_id, quantity)
            if row is None:
                raise RuntimeError(f"sell({store}, {product_id}, {quantity}) sold nothing (stock changed concurrently?)")
        if read_quantity(conns[store], store, product_id) != 0:
            raise RuntimeError(f"source verification failed for {store}/{product_id}: quantity is not zero")
        progress(f"Sold out {store} ({step}/{len(stores)}); source verified")
        if step < len(stores):
            sleep(gap_s)
    expected = {}
    for store, conn in conns.items():
        row = read_all(conn).get((store, product_id))
        if row is None:
            raise RuntimeError(f"source verification failed for {store}/{product_id}: position is missing")
        quantity, revision, deleted = row
        if quantity != 0 or deleted:
            raise RuntimeError(f"source verification failed for {store}/{product_id}: expected a live zero position")
        expected[(store, product_id)] = (0, revision)
    wait_for_product_zero(redis_client, active_ns(redis_client), product_id, expected, settle_timeout_s,
                          interval_s=interval_s, clock=clock, sleep=sleep)
    progress(f"Sold out {product_id} in {len(conns)} store(s); source and Redis verified")


def reset_sources(conns: dict, redis_client, progress: Callable[[str], None], timeout_s: float = 120) -> None:
    """Reset and restock source data only; this does not run procurement, Flink, or cloud operations."""
    result = reset(conns, redis_client, timeout_s)
    progress(f"Reset {result.positions} source positions; Redis converged for {result.scenario_id}")


class RestockReset:
    """The restock half of `make reset` for the panel's Full demo reset: cancel open purchase orders before the data reset
    (so supplier-sim delivers nothing onto the baseline), clear `restock:eta:*` after it. Both steps verify.

    `connect` opens the procurement database (None: not configured in this deployment); `layers` returns the running
    layers from Redis `demo:layers` (None when unknown, then the database decides)."""

    def __init__(self, connect: Callable[[], Any] | None, redis_client, layers: Callable[[], list[str] | None],
                 sleep: Callable[[float], None] = time.sleep, settle_s: float = 1.5, attempts: int = 3):
        self._connect, self._r, self._layers = connect, redis_client, layers
        self._sleep, self._settle, self._attempts = sleep, settle_s, attempts

    def skip_reason(self) -> str | None:
        """Why the restock steps do not apply here, or None when they must run."""
        if self._connect is None:
            return "the restock layer is not configured in this deployment (no PROCUREMENT_HOST)"
        layers = self._layers()
        if layers is not None and "restock" not in layers:
            return f"the restock layer is off (running layers: {', '.join(layers) or 'none'})"
        return None

    def cancel_orders(self, progress: Callable[[str], None]) -> int:
        try:
            conn = self._connect()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - re-raised with where and what
            raise RuntimeError(f"cannot connect to the procurement database to cancel open purchase orders: "
                               f"{type(exc).__name__}: {exc} (make layers-status shows whether restock runs)") from exc
        with contextlib.closing(conn):
            cancelled = cancel_open_orders(conn)
            left = count_open_orders(conn)
        if left:
            raise RuntimeError(f"procurement verification failed: {left} purchase order(s) still open after "
                               f"cancelling {cancelled}")
        progress(f"Cancelled {cancelled} open purchase order(s); procurement verified (0 open)")
        return cancelled

    def clear_etas(self, progress: Callable[[str], None]) -> int:
        """Delete restock:eta:* and check they stay gone for a supplier-sim cycle (it runs every second and may
        republish once from a cycle that read the orders before they were cancelled)."""
        cleared = clear_eta_keys(self._r)
        for _ in range(self._attempts):
            self._sleep(self._settle)
            back = eta_keys(self._r)
            if not back:
                progress(f"Cleared {cleared} restock:eta key(s); none came back")
                return cleared
            cleared += clear_eta_keys(self._r)
        raise RuntimeError(f"restock:eta keys keep coming back after {self._attempts} clears (e.g. {back[0]}): "
                           "an open purchase order still exists; check purchase_order and supplier-sim")


def make_presenter_operations(store_conn: Callable[[str], AbstractContextManager[Any]], stores: tuple[tuple[str, str], ...], redis_client):
    """Return callbacks for the web process without a subprocess boundary."""
    def sell_out(product_id: str, progress: Callable[[str], None]) -> None:
        with contextlib.ExitStack() as stack:
            conns = {store: stack.enter_context(store_conn(host)) for store, host in stores}
            sell_out_sources(conns, redis_client, product_id, progress)

    def reset_data(progress: Callable[[str], None]) -> None:
        with contextlib.ExitStack() as stack:
            conns = {store: stack.enter_context(store_conn(host)) for store, host in stores}
            reset_sources(conns, redis_client, progress)

    return sell_out, reset_data
