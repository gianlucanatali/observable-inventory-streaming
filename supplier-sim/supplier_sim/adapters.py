"""PostgreSQL and Redis adapters. Autocommit everywhere: a default (non-autocommit) psycopg connection
opens an implicit transaction on the first read and a following write would not commit until close."""
from __future__ import annotations

import logging

import psycopg

from .core import DEFAULT_TIME_COMPRESSION, ETA_PREFIX, Order

log = logging.getLogger("supplier_sim")


class _Lazy:
    """One connection, reopened after any error so a restarted database is picked up."""

    def __init__(self, name: str, **kw):
        self._name, self._kw, self._conn = name, kw, None

    def conn(self):
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(connect_timeout=5, autocommit=True, **self._kw)
            except Exception as exc:
                raise RuntimeError(f"cannot connect to {self._name}: {exc!r}") from exc
        return self._conn

    def reset(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            log.warning("closing a failed connection raised", exc_info=True)
        self._conn = None


class PgProcurement:
    def __init__(self, host: str, database: str, user: str, password: str, port: int = 5432):
        self._c = _Lazy(f"procurement database at {host}", host=host, port=port, dbname=database,
                        user=user, password=password)

    def _run(self, fn):
        try:
            return fn(self._c.conn())
        except Exception:
            self._c.reset()
            raise

    def lead_time_raw(self) -> str | None:
        def q(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM procurement_config WHERE key = 'lead_time_s'")
                row = cur.fetchone()
                return None if row is None else row[0]
        return self._run(q)

    def jitter_pct_raw(self) -> str | None:
        def q(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM procurement_config WHERE key = 'lead_time_jitter_pct'")
                row = cur.fetchone()
                return None if row is None else row[0]
        return self._run(q)

    def set_drawn(self, request_id: str, drawn_s: float, base_s: int) -> None:
        def q(conn):
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("UPDATE purchase_order SET lead_time_s_drawn = %s, lead_base_s_at_draw = %s "
                            "WHERE request_id = %s AND lead_time_s_drawn IS NULL", (drawn_s, base_s, request_id))
        self._run(q)

    def open_orders(self) -> list[Order]:
        def q(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT request_id, store_id, product_id, quantity_requested, requested_at_ms, "
                            "lead_time_s_drawn, lead_base_s_at_draw "
                            "FROM purchase_order WHERE delivered_at IS NULL AND cancelled_at IS NULL "
                            "ORDER BY requested_at_ms, request_id")
                return [Order(r, s, p, int(q_), int(t), None if d is None else float(d), None if b is None else int(b))
                        for r, s, p, q_, t, d, b in cur.fetchall()]
        return self._run(q)

    def mark_delivered(self, request_id: str) -> None:
        def q(conn):
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("UPDATE purchase_order SET delivered_at = now() WHERE request_id = %s "
                            "AND delivered_at IS NULL", (request_id,))
        self._run(q)


class PgSources:
    """restock(store, product, qty) in that store's own source, as the writer role."""

    def __init__(self, hosts: dict[str, str], database: str, user: str, password: str, port: int):
        self._c = {sid: _Lazy(f"source of store {sid} at {h}", host=h, port=port, dbname=database,
                              user=user, password=password) for sid, h in hosts.items()}

    def restock(self, store_id: str, product_id: str, quantity: int) -> None:
        if store_id not in self._c:
            raise RuntimeError(f"order for store {store_id!r} which is not in STORE_HOSTS")
        lazy = self._c[store_id]
        try:
            with lazy.conn().transaction(), lazy.conn().cursor() as cur:
                cur.execute("SELECT quantity, revision FROM restock(%s, %s, %s)", (store_id, product_id, quantity))
                if cur.fetchone() is None:
                    raise RuntimeError(f"restock({store_id}, {product_id}, {quantity}) changed no row "
                                       "(position missing or deleted)")
        except Exception:
            lazy.reset()
            raise


class RedisEtas:
    def __init__(self, client):
        self._r = client

    def set_eta(self, product_id: str, eta_ms: int) -> None:
        self._r.set(ETA_PREFIX + product_id, str(eta_ms))

    def clear_eta(self, product_id: str) -> None:
        self._r.delete(ETA_PREFIX + product_id)

    def eta_products(self) -> set[str]:
        return {k.removeprefix(ETA_PREFIX) for k in self._r.scan_iter(match=ETA_PREFIX + "*", count=500)}


class RedisCompression:
    """Demo clock C from Redis `demo:config` field `time_compression` (written by demo-control), read every call.
    Missing -> DEFAULT_TIME_COMPRESSION, logged once. A non-numeric or non-positive value raises."""
    HASH, FIELD = "demo:config", "time_compression"

    def __init__(self, client):
        self._r, self._missing_logged = client, False

    def __call__(self) -> float:
        raw = self._r.hget(self.HASH, self.FIELD)
        if raw is None:
            if not self._missing_logged:
                self._missing_logged = True
                log.error(f"Redis {self.HASH}.{self.FIELD} is not set: using the default {DEFAULT_TIME_COMPRESSION:g}",
                          extra={"ctx": {"default_time_compression": DEFAULT_TIME_COMPRESSION}})
            return DEFAULT_TIME_COMPRESSION
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"Redis {self.HASH}.{self.FIELD} holds {raw!r}, not a number") from None
        if not v > 0:
            raise ValueError(f"Redis {self.HASH}.{self.FIELD} is {v}, must be > 0")
        return v
