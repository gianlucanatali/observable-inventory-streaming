-- Synthetic recorded inventory (the source). Stands in for the Retailer's legacy store systems.
-- Contract: absolute quantities, immutable (store_id, product_id) keys, a revision that only grows,
-- soft delete only. The database enforces these rules, so every writer (jr, scripts, freshness probe) obeys them.

CREATE TABLE stock_position (
    store_id    text        NOT NULL,
    product_id  text        NOT NULL,
    quantity    integer     NOT NULL CHECK (quantity >= 0),
    revision    bigint      NOT NULL CHECK (revision > 0 AND revision < 9007199254740992), -- < 2^53: exact in Lua/JSON doubles
    changed_at  timestamptz NOT NULL,
    deleted     boolean     NOT NULL DEFAULT false,
    -- Why this change happened: sale | restock | reset | seed | NULL (writer did not say). Copied by the stamp trigger
    -- from the transaction setting app.change_reason; stock-projector maps it to the stock.movements kind.
    change_reason text     NULL,
    PRIMARY KEY (store_id, product_id)
);

CREATE SEQUENCE stock_revision_seq;

-- Every insert or update gets a new revision and change time, whatever the writer sends.
CREATE FUNCTION stock_position_stamp() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.store_id <> OLD.store_id OR NEW.product_id <> OLD.product_id) THEN
        RAISE EXCEPTION 'stock_position: key change % / % -> % / % is not allowed',
            OLD.store_id, OLD.product_id, NEW.store_id, NEW.product_id;
    END IF;
    -- One source per store: `ALTER DATABASE ... SET app.store_id` (init, from env STORE_ID) pins the store.
    IF coalesce(current_setting('app.store_id', true), '') <> ''
       AND NEW.store_id <> current_setting('app.store_id', true) THEN
        RAISE EXCEPTION 'stock_position: this source holds only store %, refusing a row for store %',
            current_setting('app.store_id', true), NEW.store_id;
    END IF;
    NEW.revision   := nextval('stock_revision_seq');
    NEW.changed_at := clock_timestamp();
    -- Whatever the writer put in the column is ignored: only the transaction setting counts (set_config(..., true)).
    NEW.change_reason := nullif(current_setting('app.change_reason', true), '');
    IF NEW.change_reason IS NOT NULL AND NEW.change_reason NOT IN ('sale', 'restock', 'reset', 'seed') THEN
        RAISE EXCEPTION 'stock_position: app.change_reason % is not one of sale, restock, reset, seed', NEW.change_reason;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER stock_position_stamp BEFORE INSERT OR UPDATE ON stock_position
    FOR EACH ROW EXECUTE FUNCTION stock_position_stamp();

-- Physical deletes would lose stock silently downstream: use deleted = true instead.
CREATE FUNCTION stock_position_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stock_position: physical delete of % / % is not allowed, set deleted = true',
        OLD.store_id, OLD.product_id;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER stock_position_no_delete BEFORE DELETE ON stock_position
    FOR EACH ROW EXECUTE FUNCTION stock_position_no_delete();

CREATE FUNCTION stock_position_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stock_position: TRUNCATE is not allowed';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER stock_position_no_truncate BEFORE TRUNCATE ON stock_position
    FOR EACH STATEMENT EXECUTE FUNCTION stock_position_no_truncate();

-- A sale: never below zero. Returns the new row, or nothing if there was no stock to sell.
-- Used by the background sales generator and by `make sell-out`. Marks the change as 'sale' for the transaction
-- (change_reason), then puts the previous setting back so a caller's own reason (e.g. reset) survives.
CREATE FUNCTION sell(p_store text, p_product text, p_qty integer DEFAULT 1)
RETURNS SETOF stock_position AS $$
DECLARE
    v_prev text := coalesce(current_setting('app.change_reason', true), '');
BEGIN
    PERFORM set_config('app.change_reason', 'sale', true);
    RETURN QUERY
    WITH u AS (
        UPDATE stock_position
           SET quantity = quantity - p_qty
         WHERE store_id = p_store AND product_id = p_product
           AND NOT deleted AND quantity >= p_qty AND p_qty > 0
        RETURNING *
    ) SELECT * FROM u;
    PERFORM set_config('app.change_reason', v_prev, true);
END $$ LANGUAGE plpgsql;

-- A replenishment (change_reason 'restock', same mechanics as sell).
CREATE FUNCTION restock(p_store text, p_product text, p_qty integer)
RETURNS SETOF stock_position AS $$
DECLARE
    v_prev text := coalesce(current_setting('app.change_reason', true), '');
BEGIN
    PERFORM set_config('app.change_reason', 'restock', true);
    RETURN QUERY
    WITH u AS (
        UPDATE stock_position
           SET quantity = quantity + p_qty
         WHERE store_id = p_store AND product_id = p_product
           AND NOT deleted AND p_qty > 0
        RETURNING *
    ) SELECT * FROM u;
    PERFORM set_config('app.change_reason', v_prev, true);
END $$ LANGUAGE plpgsql;

-- Logical replication for Debezium (pgoutput). Needs wal_level=logical on the server.
CREATE PUBLICATION dbz_inventory FOR TABLE stock_position;
