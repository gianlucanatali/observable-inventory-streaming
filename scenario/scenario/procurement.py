"""Restock layer helpers: procurement database (role `procurement`) and the restock:eta:* keys."""
from __future__ import annotations

import os

from .config import require

ETA_PATTERN = "restock:eta:*"


def restock_layer_on(env=None) -> bool:
    """The restock layer is on exactly when PROCUREMENT_HOST is set."""
    return bool((os.environ if env is None else env).get("PROCUREMENT_HOST"))


def procurement_connect():
    import psycopg
    e = require(["PROCUREMENT_HOST", "PROCUREMENT_DATABASE", "PROCUREMENT_USER", "PROCUREMENT_PASSWORD"])
    return psycopg.connect(host=e["PROCUREMENT_HOST"], port=int(os.environ.get("PG_PORT", "5432")),
                           dbname=e["PROCUREMENT_DATABASE"], user=e["PROCUREMENT_USER"],
                           password=e["PROCUREMENT_PASSWORD"], connect_timeout=5, autocommit=True)


def set_lead_time(conn, seconds: int) -> None:
    if seconds < 0:
        raise ValueError(f"lead time must be >= 0 seconds, got {seconds}")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO procurement_config (key, value) VALUES ('lead_time_s', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(seconds),))


def cancel_open_orders(conn) -> int:
    """Mark every open purchase order cancelled (supplier-sim ignores them). Returns how many."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE purchase_order SET cancelled_at = now() "
                    "WHERE delivered_at IS NULL AND cancelled_at IS NULL")
        return cur.rowcount


def count_open_orders(conn) -> int:
    """Purchase orders that supplier-sim would still deliver (not delivered, not cancelled)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM purchase_order WHERE delivered_at IS NULL AND cancelled_at IS NULL")
        return int(cur.fetchone()[0])


def open_order_products(conn) -> set[str]:
    """Products with at least one purchase order that supplier-sim would still deliver."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT product_id FROM purchase_order WHERE delivered_at IS NULL AND cancelled_at IS NULL")
        return {str(row[0]) for row in cur.fetchall()}


def eta_product(key) -> str:
    """Product id of a restock:eta:<product> key (bytes or str)."""
    text = key.decode() if isinstance(key, bytes) else key
    return text[len(ETA_PATTERN) - 1:]


def eta_keys(r) -> list:
    return list(r.scan_iter(match=ETA_PATTERN, count=500))


def clear_eta_keys(r) -> int:
    keys = eta_keys(r)
    return r.delete(*keys) if keys else 0
