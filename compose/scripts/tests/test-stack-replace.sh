#!/usr/bin/env bash
# Offline guard for TF_REPLACE: <dir>:<address> entries become terraform plan -replace flags for that dir only,
# brackets are not globbed, malformed entries stop with an error, and a dir's entries are dropped after its apply.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
fail() { printf 'test-stack-replace: %s\n' "$*" >&2; exit 1; }
run() { # run <TF_REPLACE value> <command...>; prints combined output, returns the status
  TF_REPLACE="$1" STACK=test OVERLAY="$ROOT" ENV_DIR="$ROOT" STACK_SOURCE_ONLY=1 \
    bash -c '. "$OVERLAY/compose/scripts/stack.sh"; '"$2" 2>&1
}
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
touch "$tmp/confluent_flink_statement.dmls"   # a file a glob of the address could match
a='confluent_flink_statement.dml["sellable-1"]' b='confluent_flink_statement.dml_late["offers-1"]'
out="$(cd "$tmp" && run "cloud:$a aws:aws_ecs_service.x cloud:$b" 'tf_replace_args cloud')" || fail "valid entries rejected: $out"
[ "$out" = "-replace=$a
-replace=$b" ] || fail "unexpected cloud flags: $out"
out="$(run "cloud:$a aws:aws_ecs_service.x cloud:$b" 'tf_replace_args cloud others')" || fail "others failed: $out"
[ "$out" = "aws:aws_ecs_service.x" ] || fail "unexpected remaining entries: $out"
out="$(run "cloud:$a" 'tf_replace_args aws')" || fail "other dir failed: $out"
[ -z "$out" ] || fail "aws got cloud flags: $out"
out="$(run "" 'tf_replace_args cloud')" || fail "empty TF_REPLACE failed: $out"
[ -z "$out" ] || fail "empty TF_REPLACE printed: $out"
for bad in "$a" "cloud:" ":$a"; do
  out="$(run "$bad" 'tf_replace_args cloud')" && fail "malformed entry accepted: $bad"
  case "$out" in *"is not <dir>:<resource address>"*) ;; *) fail "malformed-entry message missing for $bad: $out";; esac
done
for contract in \
  'rf="$(tf_replace_args "$dir")" || die "TF_REPLACE: see the error above"' \
  'tf "$dir" plan $plan_args ${replace_args[@]+"${replace_args[@]}"} -out="$plan"' \
  'rf="$(tf_replace_args "$dir" others)" || die "TF_REPLACE: see the error above"'; do
  grep -Fq "$contract" "$STACK" || fail "missing tf_apply contract: $contract"
done
printf 'test-stack-replace: PASS (TF_REPLACE: per-dir -replace flags, no globbing, loud on malformed entries)\n'
