-- Observed lead time and on-order quantity per (store, product) (contracts section 13).
-- CREATE statements first, then the INSERT (Terraform splits this file on the semicolon character: do not
-- put a semicolon inside a comment or a string).
--
-- Input: `procurement.orders`, the Debezium CDC of table purchase_order (RegexRouter, envelope in Avro).
-- Source prerequisite: purchase_order REPLICA IDENTITY FULL (contracts/procurement/001_procurement.sql).
-- DEFAULT identity can omit the before image on updates, failing Debezium deserialization before
-- this query runs. Changing timestamp expressions or filtering rows cannot repair that input.
-- Existing deployments need the source ALTER explicitly, init SQL only runs on empty volumes.
-- Verify pg_class.relreplident = 'f' for public.purchase_order and full before/after update images.
-- Old malformed events remain in Kafka: restarting at the same offset is not a repair. The operator must
-- inspect the nested exception and decoded record before choosing a consistent snapshot/rebuild
-- of the input and downstream state. Do not skip errors or switch to append (updates double-count).
-- Confluent infers a Debezium table with changelog mode `retract` and exposes the "after" columns:
-- request_id, store_id, product_id, quantity_requested, requested_at_ms, requested_at, delivered_at,
-- cancelled_at, lead_time_s_drawn. Check with SHOW CREATE TABLE first (see README).
-- The Debezium timestamptz fields (delivered_at, cancelled_at) arrive as STRING (ISO-8601 ZonedTimestamp, seen on
-- CC 2026-10-04). The query tests them for NULL, orders by delivered_at (ISO sorts as time) and parses its first
-- 19 characters (yyyy-MM-ddTHH:mm:ss, UTC) for the lead time: whole seconds are enough for lead times.
--
-- lead_time_s = average of (delivered_at - requested) in seconds over the LAST 5 DELIVERIES per key:
-- ROW_NUMBER() OVER (PARTITION BY store_id, product_id ORDER BY delivered_at DESC) <= 5 (Top-N).
-- A Top-N over an updating (retract) input is documented as supported. Confirm the Debezium update shape
-- in the first run. If the planner refuses, average over all
-- deliveries (drop the Top-N CTE and aggregate delivered rows directly).
-- on_order = SUM(quantity_requested) of orders with delivered_at and cancelled_at both NULL.
-- deliveries = number of delivered orders of the key (all of them, not only the last 5).
-- lead_time_s is 0 when the key has no delivery yet: consumers must treat deliveries = 0 as "unknown"
-- (restock.sql then uses the configured base lead time).
--
-- Output: UPSERT table keyed by (store_id, product_id), compacted. The join of two aggregates is an
-- updating query, so the sink must be upsert (an append table would be rejected).
CREATE TABLE IF NOT EXISTS `restock.forecast` (
  store_id STRING,
  product_id STRING,
  lead_time_s DOUBLE NOT NULL,
  on_order INT NOT NULL,
  deliveries INT NOT NULL,
  PRIMARY KEY (store_id, product_id) NOT ENFORCED
) WITH (
  'changelog.mode' = 'upsert',
  'kafka.cleanup-policy' = 'compact',
  'key.format' = 'avro-registry',
  'value.format' = 'avro-registry',
  'value.fields-include' = 'all'
);

INSERT INTO `restock.forecast`
WITH orders AS (
  SELECT store_id, product_id, quantity_requested, requested_at_ms, delivered_at, cancelled_at
  FROM `procurement.orders`
),
per_key AS (
  SELECT
    store_id,
    product_id,
    CAST(COALESCE(SUM(CASE WHEN delivered_at IS NULL AND cancelled_at IS NULL THEN quantity_requested ELSE 0 END), 0) AS INT) AS on_order,
    CAST(COUNT(delivered_at) AS INT) AS deliveries
  FROM orders
  GROUP BY store_id, product_id
),
recent AS (
  SELECT store_id, product_id, requested_at_ms, delivered_at
  FROM (
    SELECT
      store_id, product_id, requested_at_ms, delivered_at,
      ROW_NUMBER() OVER (PARTITION BY store_id, product_id ORDER BY delivered_at DESC) AS rn
    FROM orders
    WHERE delivered_at IS NOT NULL
  )
  WHERE rn <= 5
),
lead_times AS (
  SELECT
    store_id,
    product_id,
    AVG(CAST(TIMESTAMPDIFF(SECOND, TO_TIMESTAMP_LTZ(requested_at_ms, 3), TO_TIMESTAMP_LTZ(SUBSTRING(delivered_at FROM 1 FOR 19), 'yyyy-MM-dd''T''HH:mm:ss', 'UTC')) AS DOUBLE)) AS lead_time_s
  FROM recent
  GROUP BY store_id, product_id
)
SELECT
  k.store_id,
  k.product_id,
  COALESCE(l.lead_time_s, CAST(0 AS DOUBLE)) AS lead_time_s,
  k.on_order,
  k.deliveries
FROM per_key AS k
LEFT JOIN lead_times AS l ON k.store_id = l.store_id AND k.product_id = l.product_id;
