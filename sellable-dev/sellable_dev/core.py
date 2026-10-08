"""Local-mode aggregation that mirrors flink/sellable.sql."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime


class Aggregator:
    """Keeps the latest state per (store_id, product_id) and aggregates per product."""

    def __init__(self) -> None:
        self._state: dict[tuple[str, str], dict] = {}

    def apply(self, state: dict) -> tuple[dict, dict]:
        """Apply one inventory.state value, return (key, value) for stock.sellable."""
        for f in ("store_id", "product_id", "quantity", "deleted", "changed_at"):
            if f not in state or state[f] is None:
                raise ValueError(f"sellable-dev: inventory.state record missing field {f!r}: {state!r}")
        pid = state["product_id"]
        self._state[(state["store_id"], pid)] = state
        rows = [s for (_, p), s in self._state.items() if p == pid]
        live = [s for s in rows if not s["deleted"]]
        return {"product_id": pid}, {
            "product_id": pid,
            "sellable": sum(s["quantity"] for s in live),
            "stores_reporting": len(live),
            "last_changed_at_ms": max(to_ms(s["changed_at"]) for s in rows),
        }


def to_ms(ts) -> int:
    """Avro timestamp-millis arrives as an aware datetime from fastavro, or as an int."""
    if isinstance(ts, datetime):
        return round(ts.timestamp() * 1000)
    if isinstance(ts, int):
        return ts
    if isinstance(ts, str):  # Debezium ZonedTimestamp (ISO 8601 with offset)
        try:
            return round(datetime.fromisoformat(ts).timestamp() * 1000)
        except ValueError as e:
            raise ValueError(f"sellable-dev: unsupported timestamp string {ts!r}: {e}") from e
    raise ValueError(f"sellable-dev: unsupported changed_at value {ts!r}")


WINDOW_MS = 10 * 60 * 1000  # same size as the HOP window in overlay/flink/demand.sql
CONFIG_KEYS = ("safety_factor", "coverage_h", "min_order_qty", "default_demand_per_hour", "lead_time_s",
               "time_compression")


def _need(rec: dict, fields: tuple[str, ...], what: str) -> None:
    for f in fields:
        if f not in rec or rec[f] is None:
            raise ValueError(f"sellable-dev: {what} record missing field {f!r}: {rec!r}")


class DemandWindow:
    """Mirrors overlay/flink/demand.sql with an in-memory sliding window.

    Flink emits one value per key when a 1-minute-slide HOP window closes. Here a value is emitted after
    every SALE movement, over the 10 minutes ending at that movement (event time = changed_at_ms), which
    converges to the same number. ADJUST and RESTOCK rows, and the probe product, are ignored.
    """

    def __init__(self, window_ms: int = WINDOW_MS) -> None:
        self._window_ms = window_ms
        self._sales: dict[tuple[str, str], deque] = defaultdict(deque)

    def apply(self, mv: dict) -> tuple[dict, dict] | None:
        _need(mv, ("store_id", "product_id", "delta", "kind", "changed_at_ms"), "stock.movements")
        if mv["kind"] != "SALE" or mv["product_id"] == "__probe__":
            return None
        k = (mv["store_id"], mv["product_id"])
        now = mv["changed_at_ms"]
        q = self._sales[k]
        q.append((now, -mv["delta"]))
        while q and q[0][0] <= now - self._window_ms:
            q.popleft()
        units = sum(u for _, u in q)
        key = {"store_id": k[0], "product_id": k[1]}
        return key, {**key, "units_per_hour": units * 6.0, "window_end_ms": now}


class ForecastTracker:
    """Mirrors overlay/flink/procurement.sql from the Debezium envelope of procurement.orders."""

    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}

    def apply(self, env: dict) -> tuple[dict, dict] | None:
        """Apply one Debezium envelope ({before, after, op}); None for a record that carries no row."""
        if "op" not in env:
            raise ValueError(f"sellable-dev: procurement.orders record is not a Debezium envelope: {env!r}")
        row = env.get("after") or env.get("before")
        if row is None:
            return None
        _need(row, ("request_id", "store_id", "product_id", "quantity_requested", "requested_at_ms"), "purchase_order")
        if env["op"] == "d":
            self._orders.pop(row["request_id"], None)
        else:
            self._orders[row["request_id"]] = row
        return self.forecast(row["store_id"], row["product_id"])

    def forecast(self, store: str, product: str) -> tuple[dict, dict]:
        mine = [o for o in self._orders.values() if o["store_id"] == store and o["product_id"] == product]
        on_order = sum(o["quantity_requested"] for o in mine
                       if o.get("delivered_at") is None and o.get("cancelled_at") is None)
        delivered = sorted(((to_ms(o["delivered_at"]), o["requested_at_ms"]) for o in mine
                            if o.get("delivered_at") is not None), reverse=True)
        last5 = delivered[:5]
        lead = sum((d - r) / 1000.0 for d, r in last5) / len(last5) if last5 else 0.0
        key = {"store_id": store, "product_id": product}
        return key, {**key, "lead_time_s": lead, "on_order": on_order, "deliveries": len(delivered)}


def decide(quantity: int, on_order: int, demand_h: float | None, lead_s: float | None,
           deliveries: int, cfg: dict) -> int | None:
    """The reorder rule of overlay/flink/restock.sql (contracts section 13). Returns the quantity or None.

    Missing demand -> default_demand_per_hour. Missing lead time (no delivery) -> cfg lead_time_s.
    Incomplete config -> None (the SQL yields NULL comparisons, so no request).

    Units (demo clock C = cfg time_compression, business duration = real duration x C):
      demand_h  units per REAL hour from stock.demand; default_demand_per_hour is already per business hour.
      lead_s    observed (forecast): REAL seconds from procurement; cfg lead_time_s: BUSINESS seconds.
    The rule runs in business time: demand per business hour = demand_h / C (stock.demand only), observed lead
    time in business seconds = lead_s * C.
    """
    if any(cfg.get(k) is None for k in CONFIG_KEYS):
        return None
    c = cfg["time_compression"]
    # units per business hour: stock.demand is per real hour, so divide by C; the default is already business
    d = demand_h / c if demand_h is not None else cfg["default_demand_per_hour"]
    # business seconds: observed real lead time x C, else the configured business lead time
    ls = lead_s * c if (deliveries > 0 and lead_s is not None and lead_s > 0) else cfg["lead_time_s"]
    lead_h = ls / 3600.0
    pos = quantity + on_order
    rop = math.ceil(d * lead_h * cfg["safety_factor"])
    if not (pos <= rop and (rop > 0 or quantity == 0)):
        return None
    needed = math.ceil(d * (lead_h + cfg["coverage_h"])) - pos
    return int(max(needed, cfg["min_order_qty"]))


class Restocker:
    """Mirrors overlay/flink/restock.sql: joins state, demand, forecast and config and applies decide().

    Every input change re-evaluates the affected positions (a config change re-evaluates all). Flink rewrites
    the same request_id key; here a request is skipped when the same revision was already emitted with the
    same quantity. Probe and deleted positions never request stock.
    """

    def __init__(self) -> None:
        self._state: dict[tuple[str, str], dict] = {}
        self._demand: dict[tuple[str, str], float] = {}
        self._forecast: dict[tuple[str, str], dict] = {}
        self.cfg: dict[str, float] = {}
        self._emitted: dict[tuple[str, str], tuple[int, int]] = {}

    def on_state(self, state: dict) -> list[tuple[dict, dict]]:
        _need(state, ("store_id", "product_id", "quantity", "revision", "deleted", "changed_at"), "inventory.state")
        k = (state["store_id"], state["product_id"])
        self._state[k] = state
        return self._eval([k])

    def on_demand(self, value: dict) -> list[tuple[dict, dict]]:
        _need(value, ("store_id", "product_id", "units_per_hour"), "stock.demand")
        k = (value["store_id"], value["product_id"])
        self._demand[k] = value["units_per_hour"]
        return self._eval([k])

    def on_forecast(self, value: dict) -> list[tuple[dict, dict]]:
        _need(value, ("store_id", "product_id", "lead_time_s", "on_order", "deliveries"), "restock.forecast")
        k = (value["store_id"], value["product_id"])
        self._forecast[k] = value
        return self._eval([k])

    def on_config(self, value: dict) -> list[tuple[dict, dict]]:
        _need(value, ("key", "value"), "demo.config")
        self.cfg[value["key"]] = value["value"]
        return self._eval(list(self._state))

    def _eval(self, keys) -> list[tuple[dict, dict]]:
        out = []
        for k in keys:
            s = self._state.get(k)
            if s is None or s["deleted"] or s["product_id"] == "__probe__":
                continue
            f = self._forecast.get(k)
            qty = decide(s["quantity"], f["on_order"] if f else 0, self._demand.get(k),
                         f["lead_time_s"] if f else None, f["deliveries"] if f else 0, self.cfg)
            if qty is None or self._emitted.get(k) == (s["revision"], qty):
                continue
            self._emitted[k] = (s["revision"], qty)
            rid = f"{k[0]}|{k[1]}|{s['revision']}"
            out.append(({"request_id": rid}, {
                "request_id": rid, "store_id": k[0], "product_id": k[1],
                "quantity_requested": qty, "requested_at_ms": to_ms(s["changed_at"]),
            }))
        return out


CART_WINDOW_MS = 30 * 60 * 1000  # the temporal filter of overlay/flink/cart_at_risk.sql


class CartAtRiskTracker:
    """Mirrors overlay/flink/cart_at_risk.sql (contracts section 9).

    Latest carts.events row per (scenario_id, cart_id, product_id) by arrival order, ONLINE only; an item is
    active when that row is ADD and event_time > now - 30 min. It is at risk when the product's current
    stock.sellable row has sellable == 0 (like the SQL, stores_reporting is not looked at, so a zero row with
    no store reporting also counts; a product without a row never matches).

    Every method returns the upsert changes for carts.at-risk as (key, value) pairs, value None = tombstone.
    The key is the cart item {scenario_id, cart_id, product_id}: a new risk_id for the same item is an
    update of that key, no tombstone first. A change is emitted only when the risk of an item changes (new risk_id,
    or none any more), so identical records are never re-emitted. Time comes in as `now_ms`; call sweep() periodically for window expiry.
    """

    def __init__(self, window_ms: int = CART_WINDOW_MS) -> None:
        self._window_ms = window_ms
        self._items: dict[tuple[str, str, str], dict] = {}
        self._sellable: dict[str, dict] = {}
        self._by_product: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        self._live: dict[tuple[str, str, str], dict] = {}  # item -> risk value currently in the topic

    def on_cart(self, ev: dict, now_ms: int) -> list[tuple[dict, dict | None]]:
        _need(ev, ("scenario_id", "cart_id", "shopper_id", "store_id", "product_id", "event_type", "event_time"),
              "carts.events")
        if ev["event_type"] not in ("ADD", "ABANDON"):
            raise ValueError(f"sellable-dev: carts.events unknown event_type {ev['event_type']!r}: {ev!r}")
        if ev["store_id"] != "ONLINE":
            return []
        k = (ev["scenario_id"], ev["cart_id"], ev["product_id"])
        self._items[k] = {"shopper_id": ev["shopper_id"], "event_type": ev["event_type"],
                          "event_time_ms": to_ms(ev["event_time"]), "cart_value_eur": ev.get("cart_value_eur", 0.0),
                          "returning_shopper": ev.get("returning_shopper", False), "item_count": ev.get("item_count", 1)}
        self._by_product[ev["product_id"]].add(k)
        return self._reconcile([k], now_ms)

    def on_sellable(self, value: dict, now_ms: int) -> list[tuple[dict, dict | None]]:
        _need(value, ("product_id", "sellable", "last_changed_at_ms"), "stock.sellable")
        self._sellable[value["product_id"]] = value
        return self._reconcile(sorted(self._by_product.get(value["product_id"], ())), now_ms)

    def sweep(self, now_ms: int) -> list[tuple[dict, dict | None]]:
        """Retract risks whose cart item aged out of the window."""
        return self._reconcile(list(self._live), now_ms)

    def _risk(self, k: tuple[str, str, str], now_ms: int) -> dict | None:
        it = self._items.get(k)
        s = self._sellable.get(k[2])
        if it is None or s is None or it["event_type"] != "ADD" or s["sellable"] != 0:
            return None
        if not it["event_time_ms"] > now_ms - self._window_ms:
            return None
        changed = s["last_changed_at_ms"]
        return {"risk_id": "|".join((k[0], k[1], k[2], str(changed))), "scenario_id": k[0], "cart_id": k[1],
                "shopper_id": it["shopper_id"], "product_id": k[2], "cart_value_eur": it["cart_value_eur"],
                "returning_shopper": it["returning_shopper"], "item_count": it["item_count"], "sellable_changed_at_ms": changed,
                "detected_at": max(it["event_time_ms"], changed)}

    def _reconcile(self, keys, now_ms: int) -> list[tuple[dict, dict | None]]:
        out: list[tuple[dict, dict | None]] = []
        for k in keys:
            want, have = self._risk(k, now_ms), self._live.get(k)
            if want == have:
                continue
            key = {"scenario_id": k[0], "cart_id": k[1], "product_id": k[2]}
            if want is None:
                del self._live[k]
                out.append((key, None))
            else:
                self._live[k] = want
                out.append((key, want))
        return out
