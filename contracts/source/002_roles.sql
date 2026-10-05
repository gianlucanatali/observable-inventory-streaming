-- Least-privilege roles. Passwords are set at container start from untracked env files, never here.
-- debezium: replication + read of the published table only.
-- writer: the synthetic workload (jr, scripts, watchdog probe) may only call sell/restock and upsert rows.
CREATE ROLE debezium WITH LOGIN REPLICATION;
GRANT SELECT ON stock_position TO debezium;

CREATE ROLE stock_writer WITH LOGIN;
GRANT SELECT, INSERT, UPDATE ON stock_position TO stock_writer;
GRANT USAGE ON SEQUENCE stock_revision_seq TO stock_writer;
GRANT EXECUTE ON FUNCTION sell(text, text, integer), restock(text, text, integer) TO stock_writer;

-- Datadog Agent postgres check (basic connectivity, replication slot / WAL; no DBM).
CREATE ROLE datadog WITH LOGIN;
GRANT pg_monitor TO datadog;
