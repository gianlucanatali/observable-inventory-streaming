"""PostgreSQL source operations (stock_writer role)."""
from __future__ import annotations

UPSERT_SQL = """
INSERT INTO stock_position (store_id, product_id, quantity, revision, changed_at, deleted)
VALUES (%s, %s, %s, 1, now(), false)
ON CONFLICT (store_id, product_id)
DO UPDATE SET quantity = EXCLUDED.quantity, deleted = false
RETURNING revision, changed_at
"""  # revision/changed_at in VALUES are placeholders: the stamp trigger overwrites them


CHANGE_REASONS = ("seed", "reset")  # what the stamp trigger copies into stock_position.change_reason


def upsert_positions(conn, rows, reason: str) -> dict[tuple[str, str], tuple[int, int]]:
    """Absolute upsert in one transaction; returns {(store, product): (quantity, new_revision)}.

    `reason` ('seed' or 'reset') is set for the transaction (app.change_reason), so the stamp trigger records it
    and stock-projector publishes these changes as ADJUST, not as sales or restocks."""
    if reason not in CHANGE_REASONS:
        raise ValueError(f"change reason {reason!r} is not one of {CHANGE_REASONS}")
    written = {}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT set_config('app.change_reason', %s, true)", (reason,))  # is_local: this transaction only
        for store, product, qty in rows:
            cur.execute(UPSERT_SQL, (store, product, qty))
            got = cur.fetchone()
            if got is None:
                raise RuntimeError(f"upsert of {store}/{product} returned no row")
            written[(store, product)] = (qty, int(got[0]))
    return written


def sell(conn, store: str, product: str, qty: int = 1):
    """Returns (quantity, revision, changed_at) or None if nothing was sold."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT quantity, revision, changed_at FROM sell(%s, %s, %s)",
                    (store, product, qty))
        return cur.fetchone()


def read_quantity(conn, store: str, product: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT quantity FROM stock_position WHERE store_id = %s AND product_id = %s "
                    "AND NOT deleted", (store, product))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"{store}/{product} not found in its source (run `scenario seed`)")
    return int(row[0])


def read_all(conn) -> dict[tuple[str, str], tuple[int, int, bool]]:
    """Every row of this source except the probe: {(store, product): (quantity, revision, deleted)}."""
    with conn.cursor() as cur:
        cur.execute("SELECT store_id, product_id, quantity, revision, deleted FROM stock_position "
                    "WHERE product_id <> '__probe__'")
        return {(s, p): (int(q), int(r), bool(d)) for s, p, q, r, d in cur.fetchall()}


def write_product_weights(conn, weights: dict[str, float]) -> None:
    """Rewrite product_weight (the background sales demand per product) in one transaction."""
    with conn.transaction(), conn.cursor() as cur:
        for product, weight in weights.items():
            cur.execute("INSERT INTO product_weight (product_id, weight) VALUES (%s, %s) "
                        "ON CONFLICT (product_id) DO UPDATE SET weight = EXCLUDED.weight", (product, weight))
