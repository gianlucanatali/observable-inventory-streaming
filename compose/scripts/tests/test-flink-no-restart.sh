#!/usr/bin/env bash
# Offline guard: apply_flink_statements must not defer (and so destroy and recreate) a Flink DML statement that already
# runs. Runs the real flink_dml_pending and apply_flink_statements of stack.sh with stubbed terraform.
set -euo pipefail
OVERLAY="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK_SH="$OVERLAY/compose/scripts/stack.sh"
fail() { printf 'test-flink-no-restart: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# run <layers, space separated> <state file>: prints the stub's log (deferred value per tf_apply, other calls)
run() {
  ( LAYERS_FILE="$tmp/layers"; printf '%s\n' $1 > "$LAYERS_FILE"; STATE_FILE="$2"; LOG="$tmp/log"; : > "$LOG"
    die() { echo "die: $*" >&2; exit 9; }
    eval "$(grep -E '^ALL_FLINK=' "$STACK_SH")"
    eval "$(sed -n '/^has() {/p' "$STACK_SH")"
    eval "$(sed -n '/^flink_dml_pending() {/,/^}/p' "$STACK_SH")"
    eval "$(sed -n '/^apply_flink_statements() {/,/^}/p' "$STACK_SH")"
    tf_workspace() { :; }
    tf() { cat "$STATE_FILE"; }
    step() { shift; "$@"; }
    tf_apply() { echo "tf_apply $1 deferred=[${FLINK_DML_DEFERRED-}]" >> "$LOG"; }
    wait_subjects() { echo "wait_subjects $*" >> "$LOG"; }
    apply_flink_statements "$ALL_FLINK" "stock.sellable carts.at-risk" >> "$LOG"
    cat "$LOG" )
}
S='confluent_flink_statement.dml["sellable-1"]'
R='confluent_flink_statement.dml["demand-1"]
confluent_flink_statement.dml["procurement-1"]
confluent_flink_statement.dml_late["restock-1"]'
SET='confluent_flink_statement.dml_late["offers-set"]'
DDL='confluent_flink_statement.ddl["sellable-0"]'

# (1) first create: only DDL (or nothing) in state -> CREATE-only pass defers every enabled file.
printf '%s\n' "$DDL" > "$tmp/s1"
out="$(run "restock offers" "$tmp/s1")"
case "$out" in *"tf_apply cloud deferred=[sellable offers demand procurement restock]"*) ;; *) fail "first create must defer all: $out";; esac
case "$out" in *"wait_subjects"*) ;; *) fail "first create must wait for table readiness: $out";; esac
: > "$tmp/s0"
out="$(run "" "$tmp/s0")"
case "$out" in *"tf_apply cloud deferred=[sellable]"*) ;; *) fail "empty state, core only: sellable deferred: $out";; esac

# (2) everything already runs: no deferred pass, only the INSERT pass with nothing deferred.
printf '%s\n%s\n%s\n%s\n' "$DDL" "$S" "$R" 'confluent_flink_statement.dml["offers-1"]' > "$tmp/s2"
out="$(run "restock offers" "$tmp/s2")"
case "$out" in *"skipping the CREATE-only pass (no restart)"*) ;; *) fail "missing skip log line: $out";; esac
[ "$(printf '%s\n' "$out" | grep -c '^tf_apply')" = 1 ] || fail "expected exactly one tf_apply: $out"
case "$out" in *"tf_apply cloud deferred=[]"*) ;; *) fail "INSERT pass must run with nothing deferred: $out";; esac
case "$out" in *wait_subjects*) fail "no readiness wait when nothing is created: $out";; esac
# offers on, running as one statement set
printf '%s\n%s\n%s\n' "$DDL" "$SET" "$R" > "$tmp/s2b"
out="$(run "restock offers" "$tmp/s2b")"
case "$out" in *"skipping the CREATE-only pass"*) ;; *) fail "statement set must count as sellable+offers running: $out";; esac
# core only, sellable running: disabled layers are not pending
printf '%s\n%s\n' "$DDL" "$S" > "$tmp/s2c"
out="$(run "" "$tmp/s2c")"
case "$out" in *"skipping the CREATE-only pass"*) ;; *) fail "disabled layers must not force a pass: $out";; esac

# (3) a layer enabled on a running stack: only its files are deferred.
printf '%s\n%s\n' "$DDL" "$S" > "$tmp/s3"
out="$(run "restock" "$tmp/s3")"
case "$out" in *"tf_apply cloud deferred=[demand procurement restock]"*) ;; *) fail "new restock layer must defer only its files: $out";; esac
case "$out" in *"deferred=[sellable"*) fail "running sellable must not be deferred: $out";; esac
printf '%s\n%s\n%s\n' "$DDL" "$S" "$R" > "$tmp/s3b"
out="$(run "restock offers" "$tmp/s3b")"
case "$out" in *"tf_apply cloud deferred=[offers]"*) ;; *) fail "new offers layer must defer offers only (sellable runs alone): $out";; esac
case "$out" in *"deferred=[offers demand"*|*"restock]"*) fail "running restock must not be deferred: $out";; esac

grep -Fq 'apply_flink_statements "$ALL_FLINK" "$flink_tables"' "$STACK_SH" || fail "stack-up must call apply_flink_statements with all groups"
printf 'test-flink-no-restart: PASS (running DML statements are never deferred; new ones are)\n'
