#!/usr/bin/env bash
# Offline guard for bounded Confluent topic-read recovery.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
fail() { printf 'test-stack-retry: %s\n' "$*" >&2; exit 1; }
for contract in \
  'attempt="${3:-1}"' \
  'This server does not host this topic-partition' \
  '[ "$attempt" -lt 3 ]' \
  'retrying terraform/cloud with a fresh plan' \
  'tf_apply "$dir" "$mode" "$((attempt + 1))"'; do
  grep -Fq "$contract" "$STACK" || fail "missing retry contract: $contract"
done
printf 'test-stack-retry: PASS (bounded fresh-plan retry)\n'
