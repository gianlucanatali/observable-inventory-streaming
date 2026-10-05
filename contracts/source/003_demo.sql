-- Demo control data of a store source (contracts sections 12 and 13). Not published to Debezium.
-- Runs after 002_roles.sql (alphabetical order in /docker-entrypoint-initdb.d), so the roles exist.

-- Live knobs. demo-control writes them as the writer role; jr reads them in its sell statement on every tick.
CREATE TABLE demo_setting (
    key   text             PRIMARY KEY,
    value double precision NOT NULL
);
-- 24 sales per minute per store; jr ticks every 250 ms (240 ticks/min), so a tick sells with probability 24/240.
INSERT INTO demo_setting (key, value) VALUES ('sales_per_min_per_store', 24);

-- Relative demand per product (mean 1 over the selling products, 0 = never sold by the background sales).
-- Written by `scenario seed|reset` from seed 42 with the Zipf-like skew `demand_skew`; empty until the first seed,
-- which means no background sale happens (weight missing = 0 in the jr statement).
CREATE TABLE product_weight (
    product_id text             PRIMARY KEY,
    weight     double precision NOT NULL CHECK (weight >= 0)
);

GRANT SELECT, INSERT, UPDATE ON demo_setting   TO stock_writer;
GRANT SELECT, INSERT, UPDATE ON product_weight TO stock_writer;
