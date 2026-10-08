# sellable-dev (local mode only)

**Local mode only:** this service stands in for Confluent Cloud Flink when running without cloud resources. It implements [`overlay/flink/sellable.sql`](../flink/sellable.sql): it consumes `inventory.state` (group `sellable-dev`, from earliest), keeps the latest state per `(store_id, product_id)` and, after each record, produces the product's aggregate to `stock.sellable` (key `{product_id}`, value `{product_id, sellable, stores_reporting, last_changed_at_ms}`), exactly as the Flink job would (deleted rows count 0 and are not "reporting"; probe product included; `last_changed_at_ms` is the max over all rows). Schemas: [`sellable_key.avsc`](../contracts/avro/sellable_key.avsc), [`sellable.avsc`](../contracts/avro/sellable.avsc).

Environment: `KAFKA_BOOTSTRAP`, `SR_URL`, `KAFKA_SECURITY_PROTOCOL` (`PLAINTEXT` or `SASL_SSL`, default `SASL_SSL`, which also needs `KAFKA_API_KEY/SECRET` and `SR_API_KEY/SECRET`). Optional: `KAFKA_GROUP_ID`, `STATE_TOPIC`, `SELLABLE_TOPIC`, `SCHEMA_DIR`. Missing variables stop the process naming them.

```sh
uv run --python 3.12 pytest -q                      # unit tests
docker build -f sellable-dev/Dockerfile -t dd-sellable-dev:dev .   # from overlay/
```

It aggregates in memory from the beginning of the topic, so a restart replays everything (the output is idempotent per product).

## Restock layer (local mode only)

When `RESTOCK_TOPIC` is set (for example `restock.requests`) the service also stands in for [`demand.sql`](../flink/demand.sql), [`procurement.sql`](../flink/procurement.sql) and [`restock.sql`](../flink/restock.sql). It additionally consumes `stock.movements`, `procurement.orders` (Debezium envelope of `purchase_order`, Avro/SR) and the compacted `demo.config`, and produces:

- `stock.demand` (key `{store_id, product_id}`): `units_per_hour = 6 * units sold in the 10 minutes before the movement` (in-memory sliding window, one value after each SALE, event time `changed_at_ms`; Flink emits when each 1-minute-slide HOP window closes, so values converge but are not identical per instant). ADJUST/RESTOCK and the probe are ignored.
- `restock.forecast` (same key): `lead_time_s` = average request-to-delivery of the last 5 deliveries (0 = unknown), `on_order`, `deliveries`.
- `restock.requests` (key `{request_id}`, request id `store|product|revision`): the contracts section 13 rule over `inventory.state` joined with demand, forecast and config (all five config keys must exist, otherwise no request, like the SQL). Every input change re-evaluates the affected positions, a config change re-evaluates all. The same revision with the same quantity is emitted once.

Extra environment: `RESTOCK_TOPIC` (enables the layer), `MOVEMENTS_TOPIC` (`stock.movements`), `DEMAND_TOPIC` (`stock.demand`), `ORDERS_TOPIC` (`procurement.orders`), `FORECAST_TOPIC` (`restock.forecast`), `CONFIG_TOPIC` (`demo.config`). Schemas in `overlay/contracts/avro/`: `stock_demand(_key)`, `restock_forecast(_key)`, `restock_request(_key)`. Unset `RESTOCK_TOPIC` = disabled (the core layer needs none of these topics). The Python rule can drift from the SQL; the SQL is the reference.

Demo clock (contracts 13b): `time_compression` C (default 60) comes from `demo.config`. The rule runs in business time: demand per business hour is `units_per_hour / C` (`stock.demand` stays per real hour), the observed lead time is `lead_time_s * C` (real seconds from `restock.forecast`), while `lead_time_s` from config, `coverage_h` and `default_demand_per_hour` are already business units. Without `time_compression` in the config no request is made.

## Offers layer (local mode only)

When `OFFERS_ENABLED=true` the service also stands in for [`cart_at_risk.sql`](../flink/cart_at_risk.sql): it additionally subscribes to `carts.events` and produces `carts.at-risk` (key `{scenario_id, cart_id, product_id}`, one row per cart item, value `{risk_id, scenario_id, cart_id, shopper_id, product_id, sellable_changed_at_ms, detected_at}`, schemas `cart_at_risk(_key).avsc`). Unset, empty or `false` = disabled (it does not subscribe to `carts.events`); any other value stops the process.

- Latest event per `(scenario_id, cart_id, product_id)` by arrival order, `store_id = 'ONLINE'` only; active = latest is `ADD` and `event_time` is within 30 minutes of the wall clock.
- At risk when the product's current `stock.sellable` (computed by this same process) has `sellable == 0`. Like the SQL, `stores_reporting` is not checked, and a product with no stock row never matches. `risk_id = scenario|cart|product|sellable_changed_at_ms`, `detected_at = max(event_time, sellable_changed_at_ms)`.
- A tombstone (null value on the key) is produced when the item is abandoned, ages out of the window (swept about once a second, like the SQL's temporal filter) or sellable rises above 0; when `last_changed_at_ms` changes while still 0 the old key is tombstoned and a new risk_id is written. An identical record is never re-emitted.

Extra environment: `OFFERS_ENABLED`, `CARTS_TOPIC` (`carts.events`), `AT_RISK_TOPIC` (`carts.at-risk`). The Python logic can drift from the SQL; the SQL is the reference.
