#!/usr/bin/env bash
# Offline guard for Flink statement dependency ordering.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
FLINK="$ROOT/terraform/cloud/flink_statements.tf"
VARIABLES="$ROOT/terraform/cloud/variables.tf"
STACK="$ROOT/compose/scripts/stack.sh"
fail() { printf 'test-flink-order: %s\n' "$*" >&2; exit 1; }
grep -Fq 'startswith(k, "restock-") || startswith(k, "offers-")' "$FLINK" \
  || fail "offers DML must be late so stock.sellable exists before validation"
grep -Fq '!startswith(k, "restock-") && !startswith(k, "offers-")' "$FLINK" \
  || fail "offers DML must be excluded from the first DML wave"
grep -Fq 'variable "flink_dml_deferred"' "$VARIABLES" \
  || fail "cloud Terraform needs a DDL-only phase switch"
grep -Fq '!contains(var.flink_dml_deferred, f)' "$FLINK" \
  || fail "the DDL-only phase must omit DML for newly created table groups"
grep -Fq 'FLINK_DML_DEFERRED="$pending"' "$STACK" \
  || fail "stack-up must create Flink tables before DML"
grep -Fq 'apply_flink_statements "$ALL_FLINK" "$flink_tables"' "$STACK" \
  || fail "stack-up must gate every enabled Flink table group"
grep -Fq 'has offers && flink_tables="$flink_tables carts.at-risk"' "$STACK" \
  || fail "offers table must be included in readiness polling"
grep -Fq 'stock.demand restock.forecast restock.requests' "$STACK" \
  || fail "restock tables must be included in readiness polling"
grep -Fq 'step "wait for Flink CREATE TABLE readiness" wait_subjects 300 $flink_tables' "$STACK" \
  || fail "stack-up must poll every created table subject before DML"
grep -Fq 'readiness timeout after {timeout}s for table/topic' "$STACK" \
  || fail "readiness timeout must loudly name missing tables"
grep -Fq 'FLINK_DML_DEFERRED=""' "$STACK" \
  || fail "stack-up must enable DML only after table readiness"
if grep -Fq 'command = "sleep 30"' "$FLINK"; then
  fail "fixed Flink DDL sleeps are forbidden; poll readiness instead"
fi
printf 'test-flink-order: PASS (DDL readiness precedes ordered DML)\n'
