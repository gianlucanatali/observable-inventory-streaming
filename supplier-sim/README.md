# supplier-sim

Simulated supplier of the restock layer (contracts section 10). Every second: reads `procurement_config.lead_time_s` (a missing row falls back to `DEFAULT_LEAD_TIME_S` with an error log), finds open purchase orders (not delivered, not cancelled) with `requested_at_ms + lead_time <= now`, and for each calls `restock(store, product, quantity)` in that store's own source as the writer role, then sets `delivered_at`. A crash between the two steps re-delivers at most once more (logged). A store source that is down is logged and its orders stay open: other stores keep being served.

Also writes Redis `restock:eta:{product_id}` (earliest due epoch ms among the product's open orders, deleted when none or stale) and metrics `restock.orders.open` (gauge), `restock.orders.delivered{store}` (count), `restock.lead_time` (gauge, seconds). JSON logs with `request_id`, a `restock.deliver` span per delivery.

Per-order lead time (contracts section 13): the first time supplier-sim sees an open order it draws `drawn = base x factor(product) x (1 + u x jitter_pct/100)`, `factor` deterministic in [0.5, 2.0] from sha256(product_id), `u` uniform in [-1, 1] seeded from the request id (so a retry draws the same value), `jitter_pct` = `procurement_config.lead_time_jitter_pct` (seeded 20; a missing row uses 20 with one error log). It stores `purchase_order.lead_time_s_drawn` and `lead_base_s_at_draw` (the base in force at the draw; written once, `WHERE lead_time_s_drawn IS NULL`). Rule: `due = requested_at + drawn x (current base / base at draw)`, recomputed every cycle, so changing `lead_time_s` later rescales every open order consistently (its factor and jitter are kept) and "a lead-time change applies to open orders" still holds. If storing the draw fails it is logged and the in-memory value is used. `restock:eta:*` uses the same due time. `delivered_at` is set as before; Flink's forecast therefore sees real, per-product, jittered lead times.

Clock: the due time is computed with the container clock against `requested_at_ms` (source `changed_at`), consistent with the other services.

Environment (all required, missing ones stop the process naming them): `STORE_HOSTS`, `PG_PORT`, `PG_DATABASE`, `PG_WRITER_USER`, `PG_WRITER_PASSWORD`, `PROCUREMENT_HOST`, `PROCUREMENT_DATABASE`, `PROCUREMENT_USER`, `PROCUREMENT_PASSWORD`, `REDIS_URL`, `DEFAULT_LEAD_TIME_S`, `DD_AGENT_HOST`, `DD_ENV`, `DD_SERVICE`, `DD_VERSION`.

```sh
uv run --python 3.12 pytest -q
docker build -f supplier-sim/Dockerfile -t dd-supplier-sim:dev .   # from overlay/
```

Demo clock (contracts 13b): `lead_time_s` and the drawn lead time are BUSINESS seconds. The real wait is business / C: `due = requested_at + drawn x (current base / base at draw) / C`, with C read every cycle from Redis `demo:config` field `time_compression` (written by demo-control; missing -> 60 with one error log; non-numeric or <= 0 fails the cycle loudly). A change of C therefore applies to open orders. `restock:eta` stays a real epoch time; `delivered_at` is real, so the Flink forecast sees real seconds and multiplies by C.
