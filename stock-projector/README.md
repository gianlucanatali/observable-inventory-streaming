# stock-projector

Projects the Debezium change stream `inventory.cdc` into the Redis serving view and publishes the accepted state to the compacted topic `inventory.state`. The authoritative contract is in [`../contracts/README.md`](../contracts/README.md), sections 3, 4, 7 and 8.

## What it does, per record, in order

1. Validate the envelope (`op` in r/c/u, `after` present, required fields, `changed_at` ISO-8601 with timezone, `revision` integer in 1..2^53-1).
2. Apply to Redis with the atomic compare-and-set script [`stock_projector/apply.lua`](stock_projector/apply.lua) (newer revision wins; equal or older changes nothing).
3. Publish the **current accepted state** of the key to `inventory.state` (also when the change was stale or a duplicate), wait for the delivery report (30 s timeout; error or timeout is fatal).
3b. When Redis applied the change (not stale/duplicate, not the probe) and the quantity delta is not 0, append a movement to `stock.movements` (contract section 13): `kind` = ADJUST for snapshot/create records and for source `change_reason` `reset`/`seed`, SALE/RESTOCK from `change_reason` `sale`/`restock`, else the delta sign. The source column `change_reason` (optional in the envelope, any other value is a contract violation) is set by the stamp trigger from `app.change_reason`. A position first seen starts from 0. Delivery is awaited (fatal on failure).
4. Commit the CDC offset synchronously.

Probe rows (product `__probe__`, one per store) are published with `is_probe=true`. Snapshot completion is tracked per store: a store is done at its Debezium `source.snapshot == "last"` or at its first non-snapshot record, recorded as meta field `snapshot_done:{store_id}=1`. Meta `ready=1`, `snapshot_done=1` only once every store in `STORE_HOSTS` is done. At startup it reads `stock:active_ns`; if absent it sets `n1` with `ready=0`.

## At-least-once

A crash between steps 2 and 4 redelivers the record after restart. The script then reports stale/duplicate and the current state is published again, so the topic converges and nothing is lost for `inventory.state`. Known gap: a crash after Redis applied but before the movement was published loses that one movement (the redelivered record is stale, so no movement is emitted); accepted, the demand estimate tolerates it. `applied_at` on a republished state is the stored acceptance time, so a redelivery is byte-identical. `source_op` is the op of the record being processed, so a stale older record republishes the newer state with its own (older) op; consumers must use `revision`, not `source_op`.

## Failure behaviour

A contract violation, Redis error, publish failure, consume/deserialization error, commit failure or any unexpected exception: increments `stock.projector.errors{reason}`, sets `stock.serving.ready` to 0 and meta `ready=0` with `ready_reason`, logs one JSON line with key/offset/reason, and exits with code 1 **without committing**. The container restarts visibly and re-reads the failing record (it is never skipped). Reasons: `unsupported_op`, `missing_after`, `missing_field`, `invalid_field`, `revision_range`, `invalid_envelope`, `redis_error`, `publish_failed`, `deserialization`, `consume_error`, `commit_failed`, `unexpected`.

## Environment

Required (exit naming the variable if absent): `STORE_HOSTS` (`S01=store-s01,...`, parsed strictly; only the store ids are used), `KAFKA_BOOTSTRAP`, `KAFKA_API_KEY`, `KAFKA_API_SECRET`, `SR_URL`, `SR_API_KEY`, `SR_API_SECRET`, `REDIS_URL`, `DD_AGENT_HOST`, `DD_ENV`, `DD_SERVICE`, `DD_VERSION`.
Optional: `KAFKA_GROUP_ID` (`stock-projector`), `CDC_TOPIC` (`inventory.cdc`), `STATE_TOPIC` (`inventory.state`), `MOVEMENTS_TOPIC` (`stock.movements`), `SCHEMA_DIR` (`/app/contracts/avro`, set in the image).

Compose should also set `DD_DATA_STREAMS_ENABLED=true` (Data Streams Monitoring) and `DD_LOGS_INJECTION=true`. ddtrace instruments confluent-kafka (`DeserializingConsumer`/`SerializingProducer` included) and redis. DSM edges are verified only once it runs against real Kafka.

## Telemetry

DogStatsD to `DD_AGENT_HOST:8125`, tagged `env`/`service`/`version`: `stock.freshness.apply_delay` (distribution, seconds, `is_probe`, applied records only), `stock.projector.records{op,outcome}`, `stock.projector.errors{reason}`, `stock.serving.ready` (gauge). Keys and revisions appear only in logs and span tags.

## Build and test

Schemas are not duplicated: the Dockerfile copies `contracts/avro/stock_key.avsc` and `stock_state.avsc` from the build context.

```
docker build -f stock-projector/Dockerfile .
cd stock-projector && uv run --python 3.12 pytest -q     # no Kafka, no containers
```

Dependency pins in `pyproject.toml` are exact and should be reviewed together when they are updated.
