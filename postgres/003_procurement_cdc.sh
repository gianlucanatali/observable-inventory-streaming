#!/bin/bash
# procurement-db only. Runs once, on first start with an empty data directory, after 001_procurement.sql and 002_password.sh.
# Prepares CDC of purchase_order for the Debezium connector `procurement-orders`: a replication role, the
# publication and the pg_hba entry. Same role name and password variable as the store sources (PG_DEBEZIUM_PASSWORD).
# The connector creates its slot `dbz_procurement` on first start. wal_level=logical comes from postgresql.conf (compose command).
set -eo pipefail  # no -u: this file may be sourced by the image entrypoint

if [ -z "$PG_DEBEZIUM_PASSWORD" ]; then
  echo "postgres init (003_procurement_cdc.sh): required environment variable PG_DEBEZIUM_PASSWORD is not set; run 'make secrets'" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v dbz="$PG_DEBEZIUM_PASSWORD" <<'SQL'
CREATE ROLE debezium WITH LOGIN REPLICATION;
ALTER ROLE debezium PASSWORD :'dbz';
GRANT SELECT ON purchase_order TO debezium;
CREATE PUBLICATION dbz_procurement FOR TABLE purchase_order;
SQL

cat >> "$PGDATA/pg_hba.conf" <<HBA
host replication debezium all scram-sha-256
HBA
echo "postgres init: procurement CDC ready (role debezium, publication dbz_procurement, pg_hba replication entry)"
