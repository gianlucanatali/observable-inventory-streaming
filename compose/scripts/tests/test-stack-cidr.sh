#!/usr/bin/env bash
# Offline guard: stack preflight refuses an empty or malformed ingress CIDR before any other work.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-stack-cidr: %s\n' "$*" >&2; exit 1; }
run() { # run <PRESENTER_CIDR value> <command...>; prints combined output, returns the status
  PRESENTER_CIDR="$1" STACK=test OVERLAY="$ROOT" ENV_DIR="$ROOT" STACK_SOURCE_ONLY=1 \
    bash -c '. "$OVERLAY/compose/scripts/stack.sh"; '"$2" 2>&1
}
out="$(run '' preflight)" && fail "preflight passed with an empty CIDR"
case "$out" in *"set allowed_cidr in demo.yaml"*) ;; *) fail "empty-CIDR message missing: $out";; esac
case "$out" in *MISSING*|*"AWS login"*) fail "preflight did the other checks before the CIDR check: $out";; esac
for bad in 203.0.113.7 203.0.113.0/24 not-a-cidr; do
  out="$(run "$bad" preflight)" && fail "preflight passed with CIDR $bad"
  case "$out" in *"/32 form"*) ;; *) fail "malformed-CIDR message missing for $bad: $out";; esac
done
out="$(run 203.0.113.7/32 presenter_cidr)" || fail "valid CIDR rejected: $out"
[ "$out" = "203.0.113.7/32" ] || fail "unexpected output for valid CIDR: $out"
printf 'test-stack-cidr: PASS (empty/malformed CIDR stops preflight first)\n'
