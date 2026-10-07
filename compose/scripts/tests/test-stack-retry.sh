#!/usr/bin/env bash
# Offline guard for bounded Confluent topic-read recovery and transient network retries (behaviour: test-stack-down.sh (f)).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
fail() { printf 'test-stack-retry: %s\n' "$*" >&2; exit 1; }
for contract in \
  'attempt="${3:-1}"' \
  'This server does not host this topic-partition' \
  '[ "$attempt" -lt 3 ]' \
  'retrying terraform/cloud with a fresh plan' \
  'tf_apply "$dir" "$mode" "$((attempt + 1))"' \
  "TF_TRANSIENT_RE='no such host|connection reset by peer|TLS handshake timeout|RequestLimitExceeded'" \
  'TF_RETRY_DELAYS="${TF_RETRY_DELAYS:-10 30}"' \
  'if tf_retry_transient "$dir" plan "$attempt" "$err"; then' \
  'if tf_retry_transient "$dir" "${mode:-apply}" "$attempt" "$err"; then'; do
  grep -Fq "$contract" "$STACK" || fail "missing retry contract: $contract"
done
printf 'test-stack-retry: PASS (bounded fresh-plan retries: Confluent 404, transient network errors)\n'
