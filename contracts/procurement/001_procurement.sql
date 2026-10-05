-- Procurement system (restock layer, contracts section 10). Fictional, synthetic.
-- Runs once in the empty `procurement` database of compose service procurement-db.
-- Password of the role is set by 002_password.sh from the container environment, never here.

CREATE TABLE purchase_order (
    request_id         text        PRIMARY KEY,           -- store_id|product_id|revision
    store_id           text        NOT NULL,
    product_id         text        NOT NULL,
    quantity_requested integer     NOT NULL CHECK (quantity_requested > 0),
    -- The JDBC sink writes the record field requested_at_ms (restock.requests value, epoch ms).
    requested_at_ms    bigint      NOT NULL,
    -- Contract column `requested_at`, derived so the sink never has to convert types.
    requested_at       timestamptz GENERATED ALWAYS AS (to_timestamp((requested_at_ms / 1000.0)::double precision)) STORED,
    -- Lead time of this order, drawn by supplier-sim the first time it sees the order (base x product factor x jitter),
    -- and the base lead_time_s in force at that draw. due = requested_at + drawn x (current base / base at draw).
    lead_time_s_drawn   double precision NULL,
    lead_base_s_at_draw integer          NULL,
    delivered_at       timestamptz NULL,                  -- set by supplier-sim after restock()
    cancelled_at       timestamptz NULL                   -- set by `scenario reset`; supplier-sim ignores these
);
-- Flink consumes this Debezium stream as retract: updates need the complete old row,
-- not DEFAULT replica identity's null/key-only before image (supplier draw/delivery, reset).
-- https://nightlies.apache.org/flink/flink-docs-stable/docs/connectors/table/formats/debezium/#consuming-data-produced-by-debezium-postgres-connector
-- Existing volumes do not rerun init: apply this ALTER as the table owner before resuming CDC.
-- This only fixes future WAL records, not old events already in procurement.orders.
ALTER TABLE public.purchase_order REPLICA IDENTITY FULL;

-- Open orders are the hot set the supplier-sim scans every second.
CREATE INDEX purchase_order_open ON purchase_order (requested_at_ms)
    WHERE delivered_at IS NULL AND cancelled_at IS NULL;

CREATE TABLE procurement_config (
    key   text PRIMARY KEY,
    value text NOT NULL
);
-- Supplier lead time in seconds: 172800 = 48 h. `scenario lead-time --seconds N` changes it.
INSERT INTO procurement_config (key, value) VALUES ('lead_time_s', '172800');
-- Per-order jitter on the lead time, uniform within +- this many percent (contracts section 13).
INSERT INTO procurement_config (key, value) VALUES ('lead_time_jitter_pct', '20');

-- One role for the three procurement clients: JDBC sink connector (insert/update orders),
-- supplier-sim (read, mark delivered), scenario (change lead time, cancel orders on reset).
-- No DELETE anywhere: orders are closed, never removed.
CREATE ROLE procurement WITH LOGIN;
GRANT SELECT, INSERT, UPDATE ON purchase_order TO procurement;
GRANT SELECT, INSERT, UPDATE ON procurement_config TO procurement;
