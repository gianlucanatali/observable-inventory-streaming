-- carts.at-risk: an active ONLINE cart holds a product whose sellable stock is 0 (contract section 9).
-- Target: Confluent Cloud Flink SQL. The DDL below runs once. With the offers layer on, Terraform does not run
-- the INSERT alone: it runs it together with the INSERT of sellable.sql in ONE statement,
-- EXECUTE STATEMENT SET (overlay/terraform/cloud/flink_statements.tf). The INSERT is still self-contained and
-- can be run alone by hand.
--
-- Inputs (tables Confluent Cloud derives from the topics and their Schema Registry subjects):
--   `carts.events`    append stream. Fields: event_id, scenario_id, cart_id, shopper_id, store_id, product_id,
--                     event_type ('ADD' | 'ABANDON'), event_time (Avro timestamp-millis -> TIMESTAMP_LTZ(3)).
--   `inventory.state` upsert table keyed by (store_id, product_id), the same input as sellable.sql.
--   The sellable stock is computed in this query (CTE `sellable`), NOT read back from the `stock.sellable` topic:
--   one Kafka round trip and one statement fewer on the offers path. The CTE body is a verbatim copy of the
--   SELECT in sellable.sql (a test keeps them identical), so inside the statement set the planner shares the
--   `inventory.state` scan and its changelog normalization with sellable.sql. The small per-product aggregate
--   runs twice (the two sinks need different changelog modes). `stock.sellable` stays the published topic.
--   Assumption A2: an ABANDON event carries the product_id of the item that was abandoned (one event per
--   item change, cart_event.avsc). A cart-level abandon with a different product_id would NOT cancel the item.
--
-- Step 1 (once): output table. Upsert changelog because the join below is a regular join between updating
-- inputs and emits retractions: when the cart is abandoned, expires from the 30-minute window, or sellable
-- rises above 0, Flink deletes the key, which Kafka shows as a tombstone (null value). An append-only sink
-- cannot take that join (planner rejects retractions). Cost: consumers must ignore null values (offer-worker
-- does) and the same risk may appear again after a delete/insert pair (offer-worker dedups on risk_id).
-- PRIMARY KEY = the query's upsert key (scenario_id, cart_id, product_id), one row per cart item.
-- Confluent: when the sink primary key and the upsert key differ, the planner adds a state-intensive correction
-- operator (UPSERT_AND_PRIMARY_KEYS_DIFFERENT, upsert materialize), so keep them identical:
-- https://docs.confluent.io/cloud/current/flink/how-to-guides/resolve-common-query-problems.html
-- risk_id stays a value field: a new zero (newer sellable_changed_at_ms) updates the same key with a new risk_id.
CREATE TABLE IF NOT EXISTS `carts.at-risk` (
  risk_id STRING,
  scenario_id STRING,
  cart_id STRING,
  shopper_id STRING,
  product_id STRING,
  cart_value_eur DOUBLE,
  returning_shopper BOOLEAN,
  item_count INT,
  sellable_changed_at_ms BIGINT,
  detected_at TIMESTAMP_LTZ(3),
  PRIMARY KEY (scenario_id, cart_id, product_id) NOT ENFORCED
) DISTRIBUTED INTO 1 BUCKETS WITH (
  'changelog.mode' = 'upsert',
  'kafka.cleanup-policy' = 'delete',
  'kafka.retention.time' = '1 d',
  'key.format' = 'avro-registry',
  'value.format' = 'avro-registry',
  'value.fields-include' = 'all'      -- key columns also in the value, as cart_at_risk.avsc says
);
-- Verify the option names above against the CC Flink CREATE TABLE reference before the first run:
-- https://docs.confluent.io/cloud/current/flink/reference/statements/create-table.html
-- This statement creates the topic `carts.at-risk` and both subjects, carts.at-risk-key and carts.at-risk-value
-- (Terraform creates neither the topic nor the schemas). CC registers its own record names, offer-worker reads with
-- the writer schema, so that does not matter.

-- Step 2 (the long-running statement).
-- Bound: no time filter in Flink. A temporal filter (`event_time > NOW() - INTERVAL '30' MINUTE`) was tried and CC
-- rejected it: NOW() is non-deterministic and this is an updating pipeline (seen 2026-10-04). The worker's
-- MAX_RISK_AGE_S guard discards old risks instead (the fallback below).
-- Rejected alternative: SET 'sql.state-ttl' = '30 min'. TTL expires idle state silently (no retraction, so old
-- risks would stay in the topic) AND would also expire the sellable rows held in join state: a product whose
-- sellable has not changed for 30 minutes would stop matching new carts. Fallback if the temporal filter is not
-- accepted: keep this query without the filter and let the worker's MAX_RISK_AGE_S guard (default 300 s) discard old risks.
INSERT INTO `carts.at-risk`
WITH sellable AS (
  -- Same SELECT as the INSERT of sellable.sql (keep identical: overlay/flink/tests checks it).
  SELECT
    product_id,
    CAST(SUM(CASE WHEN deleted THEN 0 ELSE quantity END) AS BIGINT) AS sellable,
    CAST(SUM(CASE WHEN deleted THEN 0 ELSE 1 END) AS INT) AS stores_reporting,
    MAX(
      CAST(TIMESTAMPDIFF(SECOND, TO_TIMESTAMP_LTZ(0, 3), changed_at) AS BIGINT) * 1000
      + CAST(DATE_FORMAT(changed_at, 'SSS') AS BIGINT)
    ) AS last_changed_at_ms
  FROM `inventory.state`
  GROUP BY product_id
),
latest_cart_item AS (
  -- Latest event per cart item. Top-1 deduplication keeping the last row by arrival time ($rowtime).
  -- A later ABANDON replaces the ADD, so the item stops being active (and the join row is retracted).
  SELECT scenario_id, cart_id, shopper_id, product_id, event_type, event_time, cart_value_eur, returning_shopper, item_count
  FROM (
    SELECT *,
      ROW_NUMBER() OVER (PARTITION BY scenario_id, cart_id, product_id ORDER BY `$rowtime` DESC) AS rn
    FROM `carts.events`
    WHERE store_id = 'ONLINE'            -- carts are online only, store-level events never fire this
  )
  WHERE rn = 1
),
active_cart_item AS (
  SELECT scenario_id, cart_id, shopper_id, product_id, event_time, cart_value_eur, returning_shopper, item_count
  FROM latest_cart_item
  WHERE event_type = 'ADD'
)
SELECT
  CONCAT_WS('|', a.scenario_id, a.cart_id, a.product_id, CAST(s.last_changed_at_ms AS STRING)) AS risk_id,
  a.scenario_id,
  a.cart_id,
  a.shopper_id,
  a.product_id,
  a.cart_value_eur,
  a.returning_shopper,
  a.item_count,
  s.last_changed_at_ms AS sellable_changed_at_ms,
  -- Deterministic (no CURRENT_TIMESTAMP): the row must be identical when retracted. Later of the two facts that
  -- made the risk true. Plain CASE instead of GREATEST to avoid depending on timestamp support in GREATEST.
  CASE WHEN a.event_time > TO_TIMESTAMP_LTZ(s.last_changed_at_ms, 3)
       THEN a.event_time ELSE TO_TIMESTAMP_LTZ(s.last_changed_at_ms, 3) END AS detected_at
FROM active_cart_item AS a
JOIN sellable AS s
  ON a.product_id = s.product_id
WHERE s.sellable = 0;
-- Behaviour notes:
--  * The probe product (`__probe__`) has a sellable row but is never in a cart, so it never matches.
--  * Run alone (by hand), this INSERT keeps its own copy of the aggregation state. Inside the statement set the
--    planner shares the identical lower sub-plan with sellable.sql (check with EXPLAIN STATEMENT SET, "(reused)").
--  * If sellable goes 0 -> 0 with a newer last_changed_at_ms (a restock-and-sell inside the same zero), the same key
--    gets a new risk_id and the same cart fires again: acceptable for the demo, one more offer for the same cart item.
--  * Scenario reset: this query retains older ADD rows by design, but the offer-worker compares every risk's
--    scenario_id with Redis scenario:current before deduplication or Jev. Older-scenario rows are logically expired
--    and cannot publish an offer when a later sell-out reactivates their join output.
--  * Bedrock/Jev are never called from Flink, the offer-worker owns both.
