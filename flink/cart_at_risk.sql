-- carts.at-risk: an active ONLINE cart holds a product whose sellable stock is 0 (contract section 9).
-- Target: Confluent Cloud Flink SQL (one INSERT statement, the DDL below runs once).
-- NOT RUN: written without a Flink compute pool. Check each assumption in rehearsal.
--
-- Inputs (tables Confluent Cloud derives from the topics and their Schema Registry subjects):
--   `carts.events`   append stream. Fields: event_id, scenario_id, cart_id, shopper_id, store_id, product_id,
--                    event_type ('ADD' | 'ABANDON'), event_time (Avro timestamp-millis -> TIMESTAMP_LTZ(3)).
--   `stock.sellable` compacted upsert table keyed by product_id: product_id, sellable BIGINT,
--                    stores_reporting INT, last_changed_at_ms BIGINT (plain longs, contract section 2).
--   Assumption A1: the table for `stock.sellable` is already an upsert table keyed by product_id, verify this in the first run.
--   (key schema {product_id}), if CC infers it as append, run
--   ALTER TABLE `stock.sellable` SET ('changelog.mode' = 'upsert') first, and check it has PRIMARY KEY (product_id).
--   Assumption A2: an ABANDON event carries the product_id of the item that was abandoned (one event per
--   item change, cart_event.avsc). A cart-level abandon with a different product_id would NOT cancel the item.
--
-- Step 1 (once): output table. Upsert changelog because the join below is a regular join between updating
-- inputs and emits retractions: when the cart is abandoned, expires from the 30-minute window, or sellable
-- rises above 0, Flink deletes the key, which Kafka shows as a tombstone (null value). An append-only sink
-- cannot take that join (planner rejects retractions). Cost: consumers must ignore null values (offer-worker
-- does) and the same risk may appear again after a delete/insert pair (offer-worker dedups on risk_id).
-- risk_id is the PRIMARY KEY, so replays and repeated output of the same fact collapse to one key.
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
  PRIMARY KEY (risk_id) NOT ENFORCED
) DISTRIBUTED INTO 1 BUCKETS WITH (
  'changelog.mode' = 'upsert',
  'kafka.cleanup-policy' = 'delete',
  'kafka.retention.time' = '1 d',
  'key.format' = 'avro-registry',
  'value.format' = 'avro-registry',
  'value.fields-include' = 'all'      -- risk_id also in the value, as cart_at_risk.avsc says
);
-- Verify the option names above against the CC Flink CREATE TABLE reference before the first run:
-- https://docs.confluent.io/cloud/current/flink/reference/statements/create-table.html
-- This statement creates the topic `carts.at-risk` and its subjects (Terraform does not create Flink output topics).
-- CC registers its own record name/namespace for it, offer-worker reads with the writer schema, so that does not matter.

-- Step 2 (the long-running statement).
-- Bound: no time filter in Flink. A temporal filter (`event_time > NOW() - INTERVAL '30' MINUTE`) was tried and CC
-- rejected it: NOW() is non-deterministic and this is an updating pipeline (seen 2026-10-04). The worker's
-- MAX_RISK_AGE_S guard discards old risks instead (the fallback below).
-- Rejected alternative: SET 'sql.state-ttl' = '30 min'. TTL expires idle state silently (no retraction, so old
-- risks would stay in the topic) AND would also expire the stock.sellable rows held in join state: a product whose
-- sellable has not changed for 30 minutes would stop matching new carts. Fallback if the temporal filter is not
-- accepted: keep this query without the filter and let the worker's MAX_RISK_AGE_S guard (default 300 s) discard old risks.
INSERT INTO `carts.at-risk`
WITH latest_cart_item AS (
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
JOIN `stock.sellable` /*+ OPTIONS('kafka.consumer.isolation-level' = 'read-uncommitted') */ AS s
  ON a.product_id = s.product_id
WHERE s.sellable = 0;
-- Behaviour notes:
--  * The probe product (`__probe__`) has a sellable row but is never in a cart, so it never matches.
--  * If sellable goes 0 -> 0 with a newer last_changed_at_ms (a restock-and-sell inside the same zero), the risk_id
--    changes and the same cart fires again: acceptable for the demo, one more offer for the same cart item.
--  * Scenario reset: this query retains older ADD rows by design, but the offer-worker compares every risk's
--    scenario_id with Redis scenario:current before deduplication or Jev. Older-scenario rows are logically expired
--    and cannot publish an offer when a later sell-out reactivates their join output.
--  * Bedrock/Jev are never called from Flink, the offer-worker owns both.
