#!/bin/bash
# Runs once, on first start with an empty data directory, after 001/002 (contracts/source/*.sql).
# Sets role passwords from the container environment; passwords never appear in tracked files.
# Debezium does not create the slot here: the connector creates `dbz_inventory` on first start.
set -eo pipefail  # no -u: this file may be sourced by the image entrypoint (compose ignores config mode outside Swarm)

for v in STORE_ID PG_DEBEZIUM_PASSWORD PG_WRITER_PASSWORD PG_DATADOG_PASSWORD; do
  if [ -z "${!v}" ]; then
    echo "postgres init (003_passwords.sh): required environment variable $v is not set; run 'make secrets'" >&2
    exit 1
  fi
done

# psql variables + :'name' quoting avoid any SQL injection through password characters.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v store="$STORE_ID" -v dbz="$PG_DEBEZIUM_PASSWORD" -v wr="$PG_WRITER_PASSWORD" -v dd="$PG_DATADOG_PASSWORD" <<'SQL'
ALTER ROLE debezium    PASSWORD :'dbz';
ALTER ROLE stock_writer PASSWORD :'wr';
ALTER ROLE datadog     PASSWORD :'dd';
-- This source holds only one store's rows; the stamp trigger in 001_inventory.sql rejects others.
-- DB name via current_database(): ALTER DATABASE cannot take a psql variable as identifier.
SELECT format('ALTER DATABASE %I SET app.store_id = %L', current_database(), :'store') \gexec
-- Default pg_hba has no replication entry for remote hosts; the Connect container is remote.
SQL

# Replication connections (Debezium) from the compose network, and normal connections for the three roles.
cat >> "$PGDATA/pg_hba.conf" <<HBA
host replication debezium all scram-sha-256
HBA
echo "postgres init: store $STORE_ID pinned, role passwords set, pg_hba replication entry for debezium added"
