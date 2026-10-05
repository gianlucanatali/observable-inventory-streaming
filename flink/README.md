# flink

Confluent Cloud Flink SQL for the demo (`overlay/contracts/README.md` section 3b).

| File | What |
|---|---|
| `sellable.sql` | Sink table `stock.sellable` and the INSERT that aggregates `inventory.state` per product |
| `demand.sql` | Sink `stock.demand` and the HOP (1 min / 10 min) INSERT over SALE movements |
| `procurement.sql` | Sink `restock.forecast` and the INSERT: on-order and last-5 lead time from the Debezium `procurement.orders` |
| `restock.sql` | Sink `restock.requests` and the INSERT with the demand-driven reorder rule (state, demand, forecast, config) |
| `cart_at_risk.sql` | Written by the offers work (not here) |

## What `sellable.sql` does

Per `product_id` (the probe product `__probe__` included on purpose, it gives the aggregate path its own freshness signal): `sellable = SUM(CASE WHEN deleted THEN 0 ELSE quantity END)` (BIGINT), `stores_reporting = SUM(CASE WHEN deleted THEN 0 ELSE 1 END)` (INT), `last_changed_at_ms` = max `changed_at` as epoch milliseconds (BIGINT, plain long: the Redis sink stringifies fields). Output key is a record with only `product_id`; schemas in `overlay/contracts/avro/sellable_key.avsc` and `sellable.avsc`.

## How to run

- Terraform (normal path): `terraform/cloud/flink_statements.tf` reads the file, splits it on the semicolon character (so no semicolon inside comments or strings), runs CREATE statements first, then the INSERT. Complete the documented cost and approval checks before applying.
- By hand: paste the two statements, one at a time, in the Confluent Cloud Flink workspace (compute pool `dd-demo`, catalog = environment `dd-demo`, database = cluster `dd-demo`).
- Before the first run check the inferred input: `SHOW CREATE TABLE `inventory.state`;` and make sure the changelog mode is `upsert`. If it is not, `ALTER TABLE `inventory.state` SET ('changelog.mode' = 'upsert');`.

## Sources for behaviour claims (docs.confluent.io, read 2026-10-03; the pages were read through a summarising fetcher, so re-read the quoted lines when in doubt)

- Table inference from a topic and Schema Registry; default changelog mode `append` (uncompacted, not Debezium), `upsert` (compacted), `retract`: https://docs.confluent.io/cloud/current/flink/reference/statements/create-table.html . `inventory.state` is compacted, so `upsert` is the expected default.
- `ALTER TABLE ... SET ('changelog.mode' = 'append' | 'retract' | 'upsert')`, also for inferred tables: https://docs.confluent.io/cloud/current/flink/reference/statements/alter-table.html
- Options used in the CREATE TABLE (`changelog.mode`, `kafka.cleanup-policy`, `key.format` / `value.format` = `avro-registry`, `PRIMARY KEY ... NOT ENFORCED`): examples on the CREATE TABLE page above.
- Avro `timestamp-millis` maps to `TIMESTAMP_LTZ`: https://docs.confluent.io/cloud/current/flink/reference/serialization.html
- Only `UNIX_TIMESTAMP` (seconds) is documented for epoch conversion: https://docs.confluent.io/cloud/current/flink/reference/functions/datetime-functions.html
- `max_cfu` accepted values 5, 10, 20, 30, 40, 50 and one SQL statement per `confluent_flink_statement`: provider docs `docs/resources/confluent_flink_compute_pool.md`, `confluent_flink_statement.md` in github.com/confluentinc/terraform-provider-confluent.

## Checks for the first cloud run

- How the Avro key record `StockKey` (`store_id`, `product_id`) is mapped to columns and primary key when the value schema repeats those fields. The docs I read do not say. If the inferred table has odd columns, define the table explicitly instead.
- The millisecond expression (`DATE_FORMAT` pieces): `DATE_FORMAT` and `UNIX_TIMESTAMP(string)` were not checked in a live pool, and the result depends on the session time zone being the same in both calls.
- `CREATE TABLE` over a topic and subjects that Terraform already created, and the Schema Registry key/value record names Flink generates for `stock.sellable` (the Redis sink only needs the key field `product_id`).
- Update latency of the aggregate (and with it the sell-out pacing and the `stock.sellable.age` threshold). Measure it in the first run.
- Flink statements bill CFU hours while running, price not checked here.

## Demand-driven restock (contracts section 13)

Order of statements (each file: CREATE first, then INSERT): `demand.sql`, `procurement.sql`, then `restock.sql`.

- `demand.sql`: reads append table `stock.movements`, filters `kind = 'SALE'` and the probe, `HOP(TABLE ..., DESCRIPTOR(`$rowtime`), INTERVAL '1' MINUTE, INTERVAL '10' MINUTE)` grouped by store, product, window; `units_per_hour = SUM(-delta) * 6`. Event time is `$rowtime` (Kafka timestamp) with the default `SOURCE_WATERMARK()`. Reason: the documented watermark DDL (`ALTER TABLE ... MODIFY WATERMARK FOR $rowtime AS ...`) is shown only for `$rowtime`; deriving a column from `changed_at_ms` would need `ADD (ts AS TO_TIMESTAMP_LTZ(changed_at_ms, 3))` plus a watermark on it for an inferred table, not documented in that combination. The projector publishes right after the change, so the two times differ by milliseconds. Upsert sink keyed `(store_id, product_id)`; a key with no sales keeps its last value (stale demand).
- `procurement.sql`: reads `procurement.orders` (Debezium, expected changelog `retract`). CTEs: per-key `on_order` (open and not cancelled) and `deliveries`; a Top-N (`ROW_NUMBER() ... ORDER BY delivered_at DESC`, `rn <= 5`) feeding the average lead time; LEFT JOIN, upsert sink keyed `(store_id, product_id)`. `lead_time_s` is 0 when `deliveries = 0` (consumers treat it as unknown). Fallback if the planner refuses the Top-N on this input: average over all deliveries (documented deviation, not applied).
- `restock.sql`: `inventory.state` (non-probe, non-deleted) LEFT JOIN `stock.demand`, LEFT JOIN `restock.forecast`, JOIN a one-row `demo.config` pivot (`MAX(CASE WHEN key = ...)` grouped by the constant 1, joined on that constant). A pivot beats five joins: one config row, one re-evaluation per config change, an equi join instead of a cross join. Missing config key means NULL and no request. Rule exactly as contracts section 13; `lead_time_s` default comes from `demo.config`, which demo-control mirrors from `procurement_config.lead_time_s` (demo-control must write both). Same `restock.requests` schema and `request_id` as before.

Sources (docs.confluent.io, read 2026-10-03 through a summarising fetcher): HOP syntax and example `HOP(TABLE t, DESCRIPTOR($rowtime), slide, size)` and "window aggregation accepts either table type": https://docs.confluent.io/cloud/current/flink/reference/queries/window-tvf.html . `MODIFY WATERMARK FOR $rowtime ...`, `ADD (col AS expr)`: alter-table page above. Default watermark `SOURCE_WATERMARK()` (max event time seen in a partition minus 180 ms): https://docs.confluent.io/cloud/current/flink/concepts/timely-stream-processing.html .

The following checks require the first cloud run; none has been run yet:
- HOP over a filtered sub-query `TABLE (SELECT ...)` as the table argument, and the inserted HOP result into an upsert table (the window page does not discuss sinks).
- Windows close only when the watermark advances: with one partition and quiet traffic the last windows are held back, and a partition without events may stall (idle-partition handling not checked).
- `window_end` is `TIMESTAMP_LTZ(3)` and the `DATE_FORMAT`/`UNIX_TIMESTAMP` millisecond expression (same caveat as `sellable.sql`).
- That Confluent infers `retract` for the Debezium table `procurement.orders`, exposes the "after" columns, and maps Debezium `timestamptz` (ZonedTimestamp string) to `TIMESTAMP_LTZ` (the query needs it for `TIMESTAMPDIFF(SECOND, TO_TIMESTAMP_LTZ(requested_at_ms, 3), delivered_at)`).
- `ROW_NUMBER() ... <= 5` Top-N over a retract input, and the aggregate over the Top-N output.
- Regular LEFT joins of upsert tables produce an updating result the upsert sink accepts, and the shape of the primary key when the key record repeats value fields (also for `stock.demand`, `restock.forecast`, `demo.config`).
- `GREATEST` is avoided (CASE used); `CEIL` on DOUBLE and `TIMESTAMPDIFF` assumed available.
- Tombstones on `restock.requests` after a retraction (JDBC sink must keep `delete.enabled=false`).
- Update latency of the whole chain (movement, demand, order, forecast, rule), to be measured.

Topics and permissions the Terraform owner must add: topics `stock.demand` (compact, 1 partition), `restock.forecast` (compact, 1), plus existing contracts section 13 topics `stock.movements`, `procurement.orders`, `demo.config`, `restock.requests`. Flink service account: read `stock.movements`, `procurement.orders`, `inventory.state`, `stock.demand`, `restock.forecast`, `demo.config`; write `stock.demand`, `restock.forecast`, `restock.requests`; Schema Registry subjects for the three sink topics (`-key`, `-value`) writable. Statements as three `confluent_flink_statement` resources split per statement: `demand.sql`, `procurement.sql`, `restock.sql`, with `restock.sql` depending on the other two.
