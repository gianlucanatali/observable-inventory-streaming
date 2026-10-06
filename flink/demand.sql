-- Demand per (store, product) from stock movements (contracts section 13).
-- Statement order: CREATE first, then the INSERT (Terraform splits this file on the semicolon
-- character: do not put a semicolon inside a comment or a string).
--
-- Input: `stock.movements`, inferred from the topic and Schema Registry. It is an append table (not
-- compacted, not Debezium). Nothing to create. Value: store_id, product_id, qty_before, qty_after, delta,
-- kind (SALE, RESTOCK, ADJUST), revision, changed_at_ms.
--
-- Event time: the Kafka record timestamp `$rowtime`, which already carries the default watermark of every
-- Confluent Cloud table (SOURCE_WATERMARK(), max event time seen in the partition minus 180 ms).
-- Why not changed_at_ms: it would need ALTER TABLE ... ADD (ts AS TO_TIMESTAMP_LTZ(changed_at_ms, 3)) and
-- ALTER TABLE ... MODIFY WATERMARK FOR ts AS ts - INTERVAL ... on an inferred table. The ALTER TABLE page
-- documents MODIFY WATERMARK only with $rowtime and ADD of computed columns, not the combination on an
-- inferred table, verify this timing in the first run. The projector publishes the movement right after the change, so the Kafka
-- timestamp is within milliseconds of changed_at_ms. Replay of old movements would be windowed by publish
-- time instead of change time, which is acceptable for a demo.
--
-- Window: HOP, slide 1 minute, size 10 minutes (the demand_window_min parameter is read-only and baked in
-- here). Only SALE rows count. units_per_hour = units sold in the window * 6 (10 minutes -> per hour).
-- A window emits when the watermark passes its end, so the first value for a key appears after the first
-- window closes and needs later movements on the single partition to advance the watermark. An idle topic
-- holds the last windows back. A key with no sale in the last 10 minutes gets no new row: the
-- last value stays in the upsert table (stale demand, documented limit).
-- Output: UPSERT table keyed by (store_id, product_id), compacted. Every closing window overwrites the key.
CREATE TABLE IF NOT EXISTS `stock.demand` (
  store_id STRING,
  product_id STRING,
  units_per_hour DOUBLE,
  window_end_ms BIGINT,
  PRIMARY KEY (store_id, product_id) NOT ENFORCED
) WITH (
  'changelog.mode' = 'upsert',
  'kafka.cleanup-policy' = 'compact',
  'key.format' = 'avro-registry',
  'value.format' = 'avro-registry',
  'value.fields-include' = 'all'
);

-- window_end_ms uses the millisecond expression of sellable.sql, verify the expression on CC before the first run.
INSERT INTO `stock.demand`
SELECT
  store_id,
  product_id,
  CAST(SUM(-delta) AS DOUBLE) * 6.0 AS units_per_hour,
  CAST(UNIX_TIMESTAMP(DATE_FORMAT(window_end, 'yyyy-MM-dd HH:mm:ss')) AS BIGINT) * 1000
    + CAST(DATE_FORMAT(window_end, 'SSS') AS BIGINT) AS window_end_ms
FROM TABLE(
  HOP(
    TABLE `stock.movements`,
    DESCRIPTOR(`$rowtime`),
    INTERVAL '1' MINUTE,
    INTERVAL '10' MINUTE))
WHERE kind = 'SALE' AND product_id <> '__probe__'
GROUP BY store_id, product_id, window_start, window_end;
