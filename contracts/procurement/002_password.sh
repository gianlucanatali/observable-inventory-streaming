#!/bin/bash
# Runs once, on first start with an empty data directory, after 001_procurement.sql.
# Sets the `procurement` role password from the container environment; it never appears in tracked files.
set -eo pipefail  # no -u: this file may be sourced by the image entrypoint (compose ignores config mode outside Swarm)

if [ -z "$PG_PROCUREMENT_PASSWORD" ]; then
  echo "postgres init (002_password.sh): required environment variable PG_PROCUREMENT_PASSWORD is not set; run 'make secrets'" >&2
  exit 1
fi

# psql variable + :'name' quoting avoids any SQL injection through password characters.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v pw="$PG_PROCUREMENT_PASSWORD" <<'SQL'
ALTER ROLE procurement PASSWORD :'pw';
SQL
echo "postgres init: procurement role password set"
