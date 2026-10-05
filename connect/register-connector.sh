#!/usr/bin/env bash
# Register (or update) the five Debezium connectors, the Redis sink and, with LAYERS=restock, the JDBC sink. Thin wrapper: the work runs inside the connect container,
# which already has every variable the template needs (see compose.yaml). Works for local and SSH docker contexts.
# Usage (from the repository root): connect/register-connector.sh   (env DC = docker compose command, set by the Makefile)
set -euo pipefail
: "${DC:?register-connector.sh: DC (the docker compose command line) must be set; run via 'make register-connector'}"
# shellcheck disable=SC2086
# LAYERS (comma list, set by the Makefile) selects the optional layer connectors (restock -> restock-procurement).
exec $DC exec -T -e "LAYERS=${LAYERS:-}" connect python3 /opt/dd/register_connector.py
