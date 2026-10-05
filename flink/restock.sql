-- Demand-driven restock requests (amends contracts sections 10 and 13).
-- Statement order: CREATE first, then the INSERT (Terraform splits this file on the semicolon character:
-- do not put a semicolon inside a comment or a string).
--
-- Inputs (all inferred upsert tables, nothing to create): `inventory.state` (sellable.sql reads it too),
-- `stock.demand` (demand.sql), `restock.forecast` (procurement.sql), `demo.config` (demo-control).
--
-- Demo clock (contracts 13b): C = demo.config time_compression (default 60), business duration = real duration x C.
-- The rule runs in BUSINESS time. Units at each step:
--   units_per_hour (stock.demand)      units per REAL hour   -> demand_h = units_per_hour / C  (per business hour)
--   default_demand_per_hour (config)   units per BUSINESS hour, used as is
--   restock.forecast.lead_time_s       REAL seconds observed  -> x C = business seconds
--   demo.config lead_time_s            BUSINESS seconds, used as is
--   coverage_h (config)                BUSINESS hours
--
-- Rule (contract section 13), per non-probe, non-deleted position:
--   demand_h  = units_per_hour / C from stock.demand, else default_demand_per_hour (business hours)
--   lead_s    = restock.forecast.lead_time_s * C when deliveries > 0, else demo.config lead_time_s (business seconds)
--               NOTE for the operator: lead_time_s lives in TWO places (procurement_config in the procurement
--               database for supplier-sim and demo.config here). demo-control writes both so they agree.
--   position  = quantity + on_order (on_order = 0 when the key has no forecast row)
--   rop       = CEIL(demand_h * lead_h * safety_factor)
--   request when position <= rop AND (rop > 0 OR quantity = 0)
--   quantity_requested = MAX(min_order_qty, CEIL(demand_h * (lead_h + coverage_h)) - position)
--   An open order raises on_order, so position rises above rop and no second request is made while it is open.
--
-- demo.config pivot: one aggregate row (MAX(CASE WHEN key = ...) over a constant group) instead of six
-- joins, so the stream has ONE config row to join with and one config change re-evaluates every position
-- once. It joins on a constant column (c = 1), so the join is an equi join (a cross join is avoided).
-- If a key is missing in demo.config the pivot value is NULL, the comparison is NULL and NO request is
-- emitted (demo-control produces all defaults at startup, so a quiet pipeline means a missing config).
--
-- Output kind: UPSERT table with PRIMARY KEY request_id.
-- * An append table is not possible: the inputs are upsert tables and a join over them is an updating
--   query, which an append-mode table rejects (create-table page, changelog.mode section).
-- * request_id = store|product|revision, as before. One revision gives one request_id. If demand or config
--   change before the purchase order arrives in on_order (a few seconds) the same key is rewritten with a
--   new quantity, which the JDBC upsert applies to the still-open order. Accepted for a demo.
-- * When the position stops needing stock the row is retracted. Whether Flink writes that as a Kafka
--   tombstone on the upsert table is not covered by the referenced documentation. The JDBC sink must therefore
--   use delete.enabled=false so an order is never removed from purchase_order.
CREATE TABLE IF NOT EXISTS `restock.requests` (
  request_id STRING,
  store_id STRING,
  product_id STRING,
  quantity_requested INT,
  requested_at_ms BIGINT,
  PRIMARY KEY (request_id) NOT ENFORCED
) WITH (
  'changelog.mode' = 'upsert',
  'kafka.cleanup-policy' = 'compact',
  'key.format' = 'avro-registry',
  'value.format' = 'avro-registry',
  'value.fields-include' = 'all'
);

-- requested_at_ms reuses the millisecond expression of sellable.sql (TIMESTAMPDIFF + 'SSS', no UNIX_TIMESTAMP).
INSERT INTO `restock.requests`
WITH cfg AS (
  SELECT
    1 AS c,
    MAX(CASE WHEN `key` = 'safety_factor' THEN `value` END) AS safety_factor,
    MAX(CASE WHEN `key` = 'coverage_h' THEN `value` END) AS coverage_h,
    MAX(CASE WHEN `key` = 'min_order_qty' THEN `value` END) AS min_order_qty,
    MAX(CASE WHEN `key` = 'default_demand_per_hour' THEN `value` END) AS default_demand_per_hour,
    MAX(CASE WHEN `key` = 'lead_time_s' THEN `value` END) AS lead_time_s,
    MAX(CASE WHEN `key` = 'time_compression' THEN `value` END) AS time_compression
  FROM `demo.config`
  GROUP BY 1
),
live AS (
  SELECT store_id, product_id, revision, changed_at, quantity, 1 AS c
  FROM `inventory.state`
  WHERE NOT deleted AND product_id <> '__probe__'
),
base AS (
  SELECT
    s.store_id,
    s.product_id,
    s.revision,
    s.changed_at,
    s.quantity,
    CAST(s.quantity + COALESCE(f.on_order, 0) AS DOUBLE) AS pos,
    -- units per BUSINESS hour: the demand window reports per real hour, so divide by C
    COALESCE(d.units_per_hour / c.time_compression, c.default_demand_per_hour) AS demand_h,
    -- BUSINESS hours: observed lead time is real seconds (x C), the configured one is already business seconds
    CASE WHEN f.deliveries > 0 AND f.lead_time_s > 0 THEN f.lead_time_s * c.time_compression ELSE c.lead_time_s END / 3600.0 AS lead_h,
    c.safety_factor,
    c.coverage_h,
    c.min_order_qty
  FROM live AS s
  LEFT JOIN `stock.demand` AS d ON s.store_id = d.store_id AND s.product_id = d.product_id
  LEFT JOIN `restock.forecast` AS f ON s.store_id = f.store_id AND s.product_id = f.product_id
  JOIN cfg AS c ON s.c = c.c
),
rule AS (
  SELECT
    store_id, product_id, revision, changed_at, quantity, pos,
    CEIL(demand_h * lead_h * safety_factor) AS rop,
    CEIL(demand_h * (lead_h + coverage_h)) - pos AS needed,
    min_order_qty
  FROM base
)
SELECT
  store_id || '|' || product_id || '|' || CAST(revision AS STRING) AS request_id,
  store_id,
  product_id,
  CAST(CASE WHEN needed > min_order_qty THEN needed ELSE min_order_qty END AS INT) AS quantity_requested,
  CAST(TIMESTAMPDIFF(SECOND, TO_TIMESTAMP_LTZ(0, 3), changed_at) AS BIGINT) * 1000
    + CAST(DATE_FORMAT(changed_at, 'SSS') AS BIGINT) AS requested_at_ms
FROM rule
WHERE pos <= rop AND (rop > 0 OR quantity = 0);
