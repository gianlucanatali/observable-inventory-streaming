-- Sellable stock per product (contracts section 3b).
-- Run in the order below, one statement at a time (Terraform splits this file on the semicolon
-- character: do not put a semicolon inside a comment or a string).
-- Statements starting with CREATE are applied first, then the rest (see overlay/terraform/cloud).
--
-- Input: `inventory.state` is inferred by Confluent Cloud Flink from the topic and its Schema
-- Registry schemas (compacted topic, so the default changelog mode is upsert). Nothing to create.
-- Verify with SHOW CREATE TABLE `inventory.state` before the first run (overlay/flink/README.md).

-- Output table. Keeps the key a record with only product_id. Plain BIGINT/INT, no timestamp types:
-- the Redis sink stringifies fields. This statement creates the topic `stock.sellable` and its schemas.
CREATE TABLE IF NOT EXISTS `stock.sellable` (
  product_id STRING,
  sellable BIGINT,
  stores_reporting INT,
  last_changed_at_ms BIGINT,
  PRIMARY KEY (product_id) NOT ENFORCED
) WITH (
  'changelog.mode' = 'upsert',
  'kafka.cleanup-policy' = 'compact',
  -- Bootstrap default for a fresh table. cart_at_risk.sql also carries the
  -- reader-side OPTIONS hint so an existing inferred table changes immediately.
  'kafka.consumer.isolation-level' = 'read-uncommitted',
  'key.format' = 'avro-registry',
  'value.format' = 'avro-registry',
  'value.fields-include' = 'all'
);

-- The probe product (__probe__) is aggregated on purpose: it gives the aggregate path its own
-- freshness signal. changed_at is TIMESTAMP_LTZ(3) (Avro timestamp-millis). CC has no LTZ-to-epoch-ms function and
-- rejects UNIX_TIMESTAMP in an updating query (non-deterministic): whole seconds from TIMESTAMPDIFF, plus the
-- milliseconds from DATE_FORMAT 'SSS'. Verified on CC Flink 2026-10-04 (the UNIX_TIMESTAMP form was rejected).
INSERT INTO `stock.sellable`
SELECT
  product_id,
  CAST(SUM(CASE WHEN deleted THEN 0 ELSE quantity END) AS BIGINT) AS sellable,
  CAST(SUM(CASE WHEN deleted THEN 0 ELSE 1 END) AS INT) AS stores_reporting,
  MAX(
    CAST(TIMESTAMPDIFF(SECOND, TO_TIMESTAMP_LTZ(0, 3), changed_at) AS BIGINT) * 1000
    + CAST(DATE_FORMAT(changed_at, 'SSS') AS BIGINT)
  ) AS last_changed_at_ms
FROM `inventory.state`
GROUP BY product_id;
