#!/usr/bin/env bash
# Offline guard for entrypoint command logging. The dry-run path must not source
# env files or emit inherited environment values, and it never invokes a stack action.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
STATE="$ROOT/.state"
LOG_DIR="$STATE/logs"
TEST_STACK="logging-guard-$$"
SECRET_SENTINEL="logging-secret-$$"
COMMANDS=(preflight up down status layer-tf layer-post account-up account-down)

fail() { printf 'test-stack-logging: %s\n' "$*" >&2; exit 1; }
cleanup() { rm -f "$LOG_DIR/$TEST_STACK-"*.log "$STATE/$TEST_STACK.stdout" "$STATE/$TEST_STACK.stderr"; }
trap cleanup EXIT

bash -n "$STACK" || fail "stack.sh is not valid bash"
grep -Fq 'LOG_DIR="$STATE_DIR/logs"' "$STACK" || fail "missing ignored log directory contract"
grep -Fq 'chmod 600 "$log_file"' "$STACK" || fail "logs must be mode 600"
grep -Fq 'STACK_LOG_DRY_RUN' "$STACK" || fail "missing no-secrets dry-run guard"
grep -Eq 'set -x|printenv' "$STACK" && fail "stack.sh may expose environment values"

mkdir -p "$STATE"
for command in "${COMMANDS[@]}"; do
  SECRET_SENTINEL="$SECRET_SENTINEL" STACK_LOG_DRY_RUN=1 STACK="$TEST_STACK" OVERLAY="$ROOT" \
    bash "$STACK" "$command" >"$STATE/$TEST_STACK.stdout" 2>"$STATE/$TEST_STACK.stderr" \
    || fail "dry-run logging entrypoint failed for $command"
  latest="$LOG_DIR/$TEST_STACK-latest.log"
  [ -L "$latest" ] || fail "latest log symlink was not created for $command"
  log_target="$(readlink "$latest")"
  case "$log_target" in "$TEST_STACK-$command-"*.log) ;; *) fail "latest symlink target is unexpected: $log_target";; esac
  log_file="$LOG_DIR/$log_target"
  [ -f "$log_file" ] || fail "latest symlink target does not exist for $command"
  [ "$(stat -f '%Lp' "$log_file")" = 600 ] || fail "log mode is not 600 for $command"
  grep -Fq 'log started:' "$STATE/$TEST_STACK.stdout" || fail "start path was not printed for $command"
  grep -Fq 'log finished:' "$STATE/$TEST_STACK.stdout" || fail "end path was not printed for $command"
  ! grep -Fq "$SECRET_SENTINEL" "$log_file" "$STATE/$TEST_STACK.stdout" "$STATE/$TEST_STACK.stderr" \
    || fail "dry-run logging leaked an environment sentinel for $command"
  ! grep -Eiq '(api[_-]?key|api[_-]?secret|password|token)[[:space:]]*[:=][[:space:]]*[^[:space:]]+' "$log_file" \
    || fail "dry-run log contains a key/secret-shaped value for $command"
done
printf 'test-stack-logging: PASS (offline entrypoint logging and no-secrets dry-run)\n'
