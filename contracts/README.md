# Contracts

The interfaces every overlay component builds against. Change a contract here first, then the code. The terms used here are defined where they first appear in the workshop guide.

## 1. Sources (one PostgreSQL per store)

Five sources, compose services `store-s01`..`store-s05` (host = service name, port 5432, database `inventory`), all with the same schema [`source/001_inventory.sql`](source/001_inventory.sql) and roles [`source/002_roles.sql`](source/002_roles.sql). Each source holds **only its own store's rows**: the database setting `app.store_id` (set at init from env `STORE_ID`) is checked by the stamp trigger, so a row for another store is rejected. Store list and hosts are configured everywhere as `STORE_HOSTS=S01=store-s01,S02=store-s02,S03=store-s03,S04=store-s04,S05=store-s05` (order = display order).

- Table `stock_position (store_id, product_id, quantity, revision, changed_at, deleted)`. A trigger sets `revision` (global sequence, `< 2^53`) and `changed_at` on every insert/update. Physical delete, truncate and key changes raise errors.
- Writers change stock only via `sell(store, product, qty)`, `restock(store, product, qty)` or an upsert of absolute quantity (seed/reset). `quantity >= 0` is a table constraint.
- Server (each source): `wal_level=logical`, `max_replication_slots>=4`, `max_wal_senders>=4`. Slot `dbz_inventory`, publication `dbz_inventory` (names repeat per server).
- Revisions come from each source's own sequence; a key lives in one source only, so per-position ordering holds.

### Demo data (seeded, fictional)

| Item | Value |
|---|---|
| Stores | `S01`..`S05` (UrbanStreet Milano Centrale, Torino Porta Nuova, Bologna Centro, Roma Termini, Firenze SMN) |
| Stocked products | `P0001`..`P0200`, every store, starting quantity 0..40 from seed 42 |
| Sell-out product | `P0042` ("Trailrunner GTX", Alpenpace, EU 42), seeded per store S01..S05 = 2, 1, 3, 1, 2 (sellable 9); excluded from background sales |
| Probes | one per source: store `S0n`, product `__probe__`; written only by the freshness probe, quantity = epoch seconds mod 1,000,000. Never a shop product |
| Catalogue | `P0001`..`P0200` plus padding products up to the calibrated size; generated from seed 42; SHA-256 recorded in `overlay/inventory-api/catalogue/README.md` |

## 2. Topics (Confluent Cloud, one partition each, Avro with Schema Registry, TopicNameStrategy)

| Topic | Producer | Key | Value | Cleanup |
|---|---|---|---|---|
| `inventory.cdc` | Debezium (Connect) | Debezium key `{store_id, product_id}` | Debezium envelope (`before`, `after`, `source`, `op`, `ts_ms`) | delete, 7 days |
| `inventory.state` | stock-projector | [`StockKey`](avro/stock_key.avsc) | [`StockState`](avro/stock_state.avsc) | compact, never delete |
| `stock.sellable` | Flink (cloud) | `{product_id: string}` | `{product_id: string, sellable: long, stores_reporting: int, last_changed_at_ms: long}` (plain longs, no logical types) | compact |
| `carts.events` | storefront backend, jr | [`CartKey`](avro/cart_key.avsc) | [`CartEvent`](avro/cart_event.avsc) | delete, 1 day |
| `carts.at-risk` | Flink | [`CartAtRiskKey`](avro/cart_at_risk_key.avsc) | [`CartAtRisk`](avro/cart_at_risk.avsc) | delete, 1 day |
| `offers` | offer-worker | [`OfferKey`](avro/offer_key.avsc) | [`Offer`](avro/offer.avsc) | compact |

Connect internal topics: `_connect.dd-demo.configs|offsets|status` (compact, 1 partition, replication 3).

Debezium connector contract (one connector per source, names `inventory-s01`..`inventory-s05`, `topic.prefix` = `store-s01`..`store-s05`, key/value converters with `TopicRecordNameStrategy` because record names differ per prefix): `plugin.name=pgoutput`, `slot.name=dbz_inventory`, `publication.name=dbz_inventory`, `publication.autocreate.mode=disabled`, `table.include.list=public.stock_position`, `snapshot.mode=initial`, `tombstones.on.delete=false`, a `RegexRouter` routing to `inventory.cdc`, Avro key/value converters against Confluent Schema Registry. `changed_at` arrives as Debezium `ZonedTimestamp` (ISO-8601 string). `op` is `r` (snapshot), `c`, `u`. Any `d`, a null `after` or a missing field is a contract violation.

## 3. stock-projector

Consumes `inventory.cdc` (group `stock-projector`, manual commit), for each record in order:

1. Validate the envelope. Contract violation → count `stock.projector.errors{reason}`, mark readiness closed (`ready=0`), stop consuming without committing. Never skip silently.
2. Apply to Redis with the Lua script below (atomic compare-and-set on revision).
3. Publish the **current accepted state** for that key to `inventory.state` (also when the script said "stale/duplicate"), wait for delivery.
4. Commit the CDC offset.

Snapshot completion is tracked **per store** (from that store's Debezium `snapshot=last` or its first non-`r` record): meta field `snapshot_done:{store_id}=1`. `ready=1` on the active namespace only when every store in `STORE_HOSTS` is done. `is_probe` = `product_id == "__probe__"`.

## 3b. Sellable stock (Flink, cloud) and the Redis sink

Flink SQL ([`overlay/flink/sellable.sql`](../flink/sellable.sql)) reads `inventory.state` as an upsert table keyed by `(store_id, product_id)` and maintains, per `product_id` (the probe product included, which gives the aggregate path its own freshness signal): `sellable = SUM(non-deleted quantity)`, `stores_reporting = COUNT(non-deleted positions)`, `last_changed_at_ms = MAX(changed_at)` as epoch ms. Output upsert topic `stock.sellable`.

Redis sink connector `sellable-redis` (redis-kafka-connect 1.1.0): `topics=stock.sellable`, `redis.type=HASH`, `redis.keyspace=sellable`, `ExtractField$Key` on `product_id` → hash `sellable:{product_id}` with string fields `product_id, sellable, stores_reporting, last_changed_at_ms`; tombstone → DEL.

## 4. Redis (serving view)

`maxmemory` bounded, `maxmemory-policy noeviction`, AOF on, no TTL on stock keys.

| Key | Type | Content |
|---|---|---|
| `stock:active_ns` | string | active namespace, e.g. `n1` |
| `stock:{ns}:{store_id}:{product_id}` | hash | `quantity`, `revision`, `deleted` (`0/1`), `changed_at_ms`, `applied_at_ms` |
| `stock:{ns}:meta` | hash | `ready` (`0/1`), `ready_reason`, `last_revision`, `last_applied_at_ms`, `snapshot_done` (`0/1`) |
| `feed:status:{store_id}` | hash | written by the freshness probe per source: `state` (`ok/stale/unknown`), `probe_age_ms`, `checked_at_ms` |
| `feed:status` | hash | written by the freshness probe: worst state over all stores (`ok` only if all ok), `probe_age_ms` (max), `checked_at_ms` |
| `sellable:{product_id}` | hash | written by the Redis sink (section 3b); outside the namespace, not rebuilt by `rebuild-read-model` |
| `cart:{scenario_id}:{cart_id}` | hash | written by the storefront in the same `POST /api/cart` handler that publishes the cart event: `{product_id: quantity}` (ADD adds 1, ABANDON removes the line), TTL 2 h from the last change. Read by `GET /api/cart/<cart_id>` so a reload keeps the cart; a reset starts a new `scenario_id`, so older carts are not found |

Apply script contract (`KEYS[1]` = position key, `KEYS[2]` = meta key; `ARGV` = quantity, revision, deleted, changed_at_ms, applied_at_ms): if stored revision exists and `>= ARGV.revision` return `{0, stored...}` (stale/duplicate); otherwise `HSET` all fields, update `last_revision`/`last_applied_at_ms` in meta, return `{1, new...}`. Revisions are compared as integers below 2^53 (exact in Lua numbers).

## 5. Availability API (inventory-api)

`GET /api/availability/{product_id}` → `200`, `Content-Type: application/json` (the old per-store `/api/stock/...` is removed):

```json
{
  "product_id": "P0042",
  "status": "available | out_of_stock | unknown",
  "sellable": 9, "confirmed_min": 9, "at_least": false,
  "last_changed_at": "2026-10-09T11:02:00.123Z",
  "restock_eta": null,
  "unknown_reason": null,
  "feed": "ok | stale | unknown",
  "stores": [
    {"store_id": "S01", "status": "available | out_of_stock | not_stocked | unknown", "quantity": 2, "revision": 812, "feed": "ok", "live": true}
  ],
  "product": {"name": "Trailrunner GTX", "brand": "Alpenpace", "size": "EU 42", "image_url": "/img/P0042.svg"},
  "release": "1.1.0"
}
```

Two Redis pipelines per request inside the `stock.read` span (the namespace is needed to name the second batch): (1) `stock:active_ns`, `sellable:{product_id}`, `feed:status:{store}` for every store; (2) the namespace meta and the per-store positions `stock:{ns}:{store}:{product_id}` for every store in `STORE_HOSTS`.

- `sellable` comes from `sellable:{product_id}` (Flink): the sum of every store's last known position, including a quiet store's. The `stores` breakdown comes from the per-store positions (projector). They travel different paths and may briefly disagree.
- `confirmed_min` is the answer: the quantity the shop may promise. With every store live it equals `sellable`. With any store not live it is the sum of the **live** stores' quantities, capped by `sellable` (`min(live sum, sellable)`), so a quiet store's last known quantity is never promised. `null` when `unknown_reason` is `not_ready`, `redis_error` or `not_found`.
- A store is **live** (`live: true`) when the namespace is ready, its `feed:status:{store}` is `ok` and not older than 10 s, and its position is present (a `not_stocked` position is live and counts 0). Otherwise it is not live: its entry keeps quantity/revision when present (the **last seen** value, shown as detail and not counted), with `status: unknown` only if the position is missing.
- `at_least = true` when any store is not live.
- **The rule applies to every reader that decides "can I sell this?"**, not only to this API: the same keys, the same 10 s age limit, `not_stocked` live and 0, capped by `sellable`. Today that is the API and `offer-worker` (an alternative is eligible only if its confirmed minimum is > 0; uncomputable = not eligible), which keeps its own copy in `offer-worker/offer_worker/stock.py`. Both test suites run the shared case table [`stock-trust-cases.json`](stock-trust-cases.json); a new reader must run it too. `carts.at-risk` still fires on the Flink total (a known follow-up).
- `status`: namespace not ready, Redis error, or `sellable:{product_id}` missing → `unknown` (`unknown_reason` ∈ `not_ready, redis_error, not_found`); `confirmed_min > 0` → `available`; `confirmed_min == 0` and not `at_least` → `out_of_stock`; `confirmed_min == 0` and `at_least` → `unknown` (`unknown_reason: stores_unknown`), even when `sellable > 0` from a quiet store's last seen value. Never treated as zero or available. Redis errors still answer 200 with `unknown`; a 5xx means the API itself failed.
- Releases 1.0.0, 1.1.0 and 1.2.0 compute these fields with the same code; they differ only in `product` and in where the catalogue is prepared.
- `product_id` must match `^P\d{4}$` (the probe product is not a shop product) → otherwise 404.
- `feed` = worst store feed. `product`: `null` in release **1.0.0**; present in **1.1.0** and **1.2.0**. `release` = `DD_VERSION`.
- `GET /healthz` (liveness) and `GET /readyz` (ready only after any startup preparation).

### Releases

| `DD_VERSION` | Catalogue | Role in the demo |
|---|---|---|
| `1.0.0` | not used | healthy starting release |
| `1.1.0` | parses the catalogue JSON and rebuilds the product index **inside every request** (`CATALOGUE_MODE=per_request`) | regressed release; its full rollout is the Incident |
| `1.2.0` | prepares the same index **once per worker before readiness** (`CATALOGUE_MODE=startup`) | the fix, introduced by Canary against 1.1.0 |

All share `service:inventory-api`, `env:dd-demo`. Spans: request (Flask), `catalogue.prepare` (custom), `stock.read` (custom, wrapping the Redis call, which also has its auto-instrumented span).

## 6. Ingress routes (nginx, the only exposed port)

| Path | Upstream |
|---|---|
| `/api/availability/` | inventory-api releases, weights from `overlay/nginx/routing.conf` (rendered by `make route-*`), `proxy_next_upstream off` |
| everything else (`/`, other `/api/*` such as cart, offers, beacon/display, plus `/config`, `/img/`) | storefront (React build + Flask backend) |

Every response carries `X-Release` (from the API) so load tools can count requests per release.

## 7. Telemetry names

Unified tags on every service: `env:dd-demo`, `service:<name>`, `version:<release>`. IDs (cart, risk, offer, revision) go in logs and span tags, **never metric tags**.

| Metric (DogStatsD) | Type | Tags | Emitted by |
|---|---|---|---|
| `stock.freshness.apply_delay` | distribution, seconds (`applied_at - changed_at`) | `is_probe` | stock-projector |
| `stock.projector.records` | count | `op`, `outcome` (`applied/stale_or_duplicate`) | stock-projector |
| `stock.projector.errors` | count | `reason` | stock-projector |
| `stock.serving.ready` | gauge 0/1 | — | stock-projector |
| `stock.probe.age` | gauge, seconds since the last probe write that is not yet visible in Redis (0 when caught up) | `store` | freshness-probe |
| `stock.sellable.age` | gauge, seconds from the newest probe write to `sellable:__probe__.last_changed_at_ms` catching up (Flink + sink path; 0 when caught up) | — | freshness-probe |
| `stock.probe.write_errors` | count | `store` | freshness-probe |
| `stock.feed.state` | gauge (1 ok, 0 stale, -1 unknown) | `store` | freshness-probe |
| `stock.connect.task_running` | gauge 0/1 | `connector` | freshness-probe (Connect REST; the five Debezium connectors and `sellable-redis`) |
| `stock.lookup.result` | count | `status`, `unknown_reason`, `at_least` | inventory-api |
| `stock.display.delay` | distribution, seconds (`last_changed_at` of the rendered sellable answer → backend receive time of the beacon) | — | storefront backend (beacon from UI) |
| `offer.decision` | count | `route`, `reason` (adds `disabled` when no Jev key, `no_choice` when fewer than two options are left for Jev; notify-me is a Jev option only with a restock date) | offer-worker |
| `offer.text` | count | `route`, `reason` | offer-worker |
| `offer.completed` | count | `offer_type` | offer-worker |
| `offer.stock.unconfirmed` | count | `reason` (`not_ready`, `not_found`, `redis_error`, `malformed`, `stores_unknown`): an alternative's confirmed minimum was not computable, or 0 only because a store is not live; the alternative is not eligible | offer-worker |

## 8. Configuration (environment variables, values in untracked `.env`)

`KAFKA_BOOTSTRAP`, `KAFKA_API_KEY`, `KAFKA_API_SECRET`, `SR_URL`, `SR_API_KEY`, `SR_API_SECRET`, `PG_*` per role, `REDIS_URL`, `DD_API_KEY`, `DD_SITE`, `DD_ENV=dd-demo`, `DD_VERSION`, `DD_SERVICE`, `JEV_API_KEY`, `AWS_REGION`. Components fail at startup, naming the missing variable, when one is absent. Each service has its own Confluent identity: `terraform output env_file` emits per-app names (`PROJECTOR_KAFKA_API_KEY`, `CONNECT_KAFKA_API_KEY`, ...) and compose maps them onto the contract names per service. `stock.display.delay` is measured from source `changed_at` to the storefront backend's receive time of the beacon (server clocks), not the browser clock.

## 9. Online carts and the sell-out beat

- The shop is online: cart events carry `store_id = "ONLINE"`. Cart at risk fires when an active cart holds a product whose **sellable stock** reaches 0 (Flink joins `carts.events` with `stock.sellable`); `CartAtRisk.stock_revision` is replaced by `sellable_changed_at_ms` (the `last_changed_at_ms` of the zero row) and `risk_id = scenario_id|cart_id|product_id|sellable_changed_at_ms`.
- `make sell-out PRODUCT=P0042` (scenario `sell-out --product P0042 --gap-s 1.5`): for each store in `STORE_HOSTS` order, sell that store's whole quantity of the product in its own source, then wait the gap. Prints each step.
- Background sales: one jr per source (`jr-sales-s01`..`s05`), fixed store per container, seed `42 + n`, a 250 ms tick that sells with probability rate/240 x product weight (rate = `sales_per_min_per_store`, control panel, default 24), never `P0042`. `make reset` stops them.

## 10. Restock loop (layer `restock`)

- Flink [`overlay/flink/restock.sql`](../flink/restock.sql) reads `inventory.state` and emits a restock request when a non-probe, non-deleted position's `quantity` becomes 0: topic `restock.requests` (compact, 1 partition), key `{request_id}`, value `{request_id, store_id, product_id, quantity_requested: int, requested_at_ms: long}`, `request_id = store_id|product_id|revision`. `quantity_requested` = policy quantity 10 (fixed; not a demand window).
- **Procurement system**: compose service `procurement-db` (PostgreSQL, same image pin), database `procurement`, table `purchase_order (request_id text PK, store_id, product_id, quantity_requested int, requested_at timestamptz, delivered_at timestamptz NULL)` and `procurement_config (key text PK, value text)` with `lead_time_s` (default `172800` = 48 h). Schema in `overlay/contracts/procurement/001_procurement.sql`.
- JDBC sink connector `restock-procurement` (Confluent JDBC sink, installed in the Connect image): `topics=restock.requests`, upsert on `request_id` into `purchase_order`; duplicates are harmless. Tombstones (Flink retracts a request that no longer qualifies) are dropped by a `Filter` + `RecordIsTombstone` SMT: a purchase order once placed stays.
- **supplier-sim** (Python): every second, for open purchase orders with `requested_at + lead_time_s <= now()` (lead time read each cycle, so a change applies to every open order), calls `restock(store, product, quantity_requested)` in that store's source as the writer role and sets `delivered_at` in the same logical step (restock first, then mark; a crash in between re-delivers at most once more, which is accepted for a demo and counted). Writes Redis `restock:eta:{product_id}` = earliest due time (epoch ms) among the product's open orders, deleted when none. Metrics: `restock.orders.open` (gauge), `restock.orders.delivered` (count, tag `store`), `restock.lead_time` (gauge, seconds).
- `make lead-time SECONDS=60` updates `procurement_config.lead_time_s`.
- The availability API adds `"restock_eta": "<ISO time>" | null` from `restock:eta:{product_id}`; the shop shows "Back in stock by <time>" when out of stock and an ETA exists.

## 11. Stacks, layers and tags

- `STACK` (default `dev` locally) selects a stack. `DD_ENV=dd-demo-${STACK}`. Every service gets `DD_TAGS="project:dd-demo stack:${STACK} layer:<its layer>"` and the matching `com.datadoghq.tags.*` container labels; the Agent gets `DD_TAGS=project:dd-demo stack:${STACK}` (host tags).
- Compose profiles = layers: `core` (default, no profile), `releases`, `restock`, `offers`, `dd-synthetics`, `dd-rum` (config only), plus `tools` and `jr` as today. `dd-streams` is Datadog-side (DSM env + Confluent integration), no containers.
- Terraform: variable `stack` (required) and one boolean per layer (`enable_releases`, `enable_restock`, `enable_offers`, `enable_dd_streams`, `enable_dd_synthetics`, `enable_dd_rum`); state per stack (`terraform workspace` named after the stack); AWS provider `default_tags = {project = "dd-demo", stack = var.stack}` plus `layer` per resource; Confluent display names `dd-demo-${stack}-...`; Datadog resources tagged `project:dd-demo`, `stack:<stack>`, `layer:<layer>` and scoped to `env:dd-demo-<stack>`.
- `make layer-on L=<layer> STACK=<stack>` / `layer-off`: Terraform toggle (cloud only), compose profile up/down, connector registration where the layer has connectors, a layer health check; prints the duration of each step and the total.

## 12. Demo control parameters (control panel)

Registry: [`demo-params.json`](demo-params.json) (key, default, range, unit, where it lives, who reads it, live/restart). The **demo-control** service (`overlay/demo-control/`, page `/control/` through nginx, HTTP basic auth with `CONTROL_PASSWORD` from `.env.secrets`) is the only writer; it shows every parameter with its current value, default and apply mode, validates ranges, and on each change sends a Datadog event (`title: "demo config: <key> = <value>"`, tags `project:dd-demo stack:<stack> layer:<layer> param:<key> demo_event:config`) and a JSON log line. Panel actions (reset, sell-out, routing, checks, sales, store feed) send `demo action: <name> started|succeeded|failed`, tagged `project:dd-demo demo_event:action action:<name> stack:<stack>` plus the action parameters. `make lead-time` and the other terminal targets write directly and send no event.

Where values live and how consumers read them:
- `redis`: hash `demo:config` (field = key, value = string). Consumers read on every cycle/request and fall back to the registry default (logged once) when a field is absent. `offers_kill_switch` is mirrored to the existing key `offers:kill_switch`.
- `kafka_config`: compacted topic `demo.config`, key `{key: string}`, value `{key: string, value: double, updated_at_ms: long}` (Avro, SR). Flink joins it as an upsert table (`restock.sql`: `quantity <= reorder_point`, `quantity_requested = reorder_qty`); lowering or raising the reorder point re-evaluates current positions (request ids stay `store|product|revision`, so repeats are idempotent). demo-control produces the defaults at startup if the topic is empty.
- `procurement_db`: `procurement_config.lead_time_s` (section 10).
- `store_dbs`: table `demo_setting (key text PK, value double precision)` in every store source, row `sales_per_min_per_store`; jr ticks every 250 ms and its statement sells only when `random() < rate/240` (the product is swapped for a non-existent one otherwise, same trick as the P0042 exclusion).
- `flink_statement`: read-only in the panel.

The panel also shows (read-only) the current canary routing and which layers are running.

## 13. Demand-driven restock (replaces the trigger and quantity in section 10)

Topics (1 partition, Avro/SR):
| Topic | Producer | Key | Value | Cleanup |
|---|---|---|---|---|
| `stock.movements` | stock-projector | `{store_id, product_id}` | `{store_id, product_id, qty_before: int, qty_after: int, delta: int, kind: SALE/RESTOCK/ADJUST, revision: long, changed_at_ms: long}` | delete, 1 day |
| `stock.demand` | Flink `demand.sql` | `{store_id, product_id}` | `{store_id, product_id, units_per_hour: double, window_end_ms: long}` | compact |
| `procurement.orders` | Debezium `procurement-orders` (on `procurement-db`, table `purchase_order`, RegexRouter, default TopicNameStrategy: subjects `procurement.orders-key`/`-value`, which Confluent Cloud Flink needs to infer the table) | Debezium key | Debezium envelope | delete, 7 days |
| `restock.forecast` | Flink `procurement.sql` | `{store_id, product_id}` | `{store_id, product_id, lead_time_s: double, on_order: int, deliveries: int}` | compact |
| `demo.config` | demo-control | `{key}` | `{key, value: double, updated_at_ms: long}` | compact |

- Movements: published by the projector only when Redis applied the change (not for stale/duplicate), after `inventory.state`, before the commit. `kind`: `ADJUST` for snapshot records and for reset upserts (scenario marks them: a reset is an upsert of absolute quantity; the projector cannot tell, so: `SALE` when delta < 0 and source op is UPDATE, `RESTOCK` when delta > 0 and UPDATE, `ADJUST` otherwise; the demand query ignores ADJUST). Probe excluded.
- Demand: `units_per_hour = SUM(-delta) of SALE over HOP(changed_at, 1 min, 10 min) × 6`.
- Forecast: lead time = average of `delivered_at - requested_at` over the last 5 deliveries per (store, product); `on_order = SUM(quantity_requested)` of orders with `delivered_at IS NULL AND cancelled_at IS NULL`.
- Restock rule (`restock.sql`): inputs `inventory.state` ⋈ `stock.demand` ⋈ `restock.forecast` ⋈ `demo.config`. `position = quantity + on_order`; `rop = CEIL(demand × lead_h × safety_factor)`; request when `position <= rop` (and `rop > 0` or `quantity = 0`); `quantity_requested = GREATEST(min_order_qty, CEIL(demand × (lead_h + coverage_h)) - position)`. Defaults when missing: demand → `default_demand_per_hour`, lead time → `lead_time_s`. Output unchanged from section 10 (`restock.requests`, request id per revision).
- Per-item variety: store sources get table `product_weight (product_id PK, weight double)` seeded from seed 42 with a Zipf-like skew (`demand_skew`, default 1.0; mean weight 1); the jr statement sells when `random() < rate/240 × weight`. supplier-sim lead time per order = `lead_time_s × factor(product) × (1 ± jitter)`, `factor` deterministic from sha256(product_id) in [0.5, 2.0], jitter uniform within `lead_time_jitter_pct`.

New parameters (section 12 registry): `safety_factor` (1.2, live), `coverage_h` (24, live), `min_order_qty` (5, live), `default_demand_per_hour` (1.0, live), `lead_time_jitter_pct` (20, live, supplier-sim), `demand_skew` (1.0, applies on reseed), `demand_window_min` (10, read-only, Flink statement).

### 13b. Demo clock and per-item details (as decided 2026-10-03)

- `time_compression` C (default 60) lives in both `demo.config` and Redis `demo:config`. Business duration = real duration × C. `demand.sql` keeps reporting units per **real** hour; `restock.sql` converts: demand per business hour = units_per_real_hour / C; observed lead time (real seconds from procurement) × C = business seconds; `lead_time_s`, `coverage_h` are business units. supplier-sim waits drawn business lead time / C in real time. The shop shows ETAs as business durations ("back in about 2 days") with a small "demo clock 60×" note.
- Movements: `change_reason` column in the sources (`seed`, `reset`, `sale`, `restock`; set by the stamp trigger from `app.change_reason`) arrives as a nullable string in the Debezium `after`; projector maps seed/reset/snapshot/create → ADJUST, sale → SALE, restock → RESTOCK, null on UPDATE → delta sign; delta 0 skipped. `stock.movements` values are plain longs.
- Store sources also have `003_demo.sql` (`demo_setting`, `product_weight`). `demand_skew` is read from Redis by scenario seed/reset, which rewrites `product_weight`.
- Procurement: `procurement_config` rows `lead_time_s`, `lead_time_jitter_pct`; `purchase_order` columns `lead_time_s_drawn`, `lead_base_s_at_draw`, `cancelled_at`; due = requested_at + drawn × (current base / base at draw) / C.
- Synthetics: Datadog-managed public locations; SG ingress from Datadog's published Synthetics ranges only while `dd-synthetics` is on. No private-location container.
