#!/usr/bin/env bash
# Deterministic unit tests for ALB weighted routing. The aws CLI is a local mock.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
script="$repo_root/compose/scripts/alb-routing.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/state"

cat > "$tmp/bin/aws" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$MOCK_AWS_LOG"
case "$*" in
  *"modify-listener"*)
    echo "mock aws: modify-listener must never be used for inventory routing" >&2
    exit 89
    ;;
  *"modify-rule"*)
    args=("$@")
    action=""
    for ((i = 0; i < ${#args[@]}; i++)); do
      if [ "${args[$i]}" = "--actions" ]; then action="${args[$((i + 1))]}"; break; fi
    done
    case "$action" in file://*) cp "${action#file://}" "$MOCK_ACTIONS";; *) echo "mock aws: expected actions file, got $action" >&2; exit 90;; esac
    ;;
  *"describe-rules"*)
    printf '%s\n' '[{"TargetGroupArn":"tg-100","Weight":0},{"TargetGroupArn":"tg-110","Weight":90},{"TargetGroupArn":"tg-120","Weight":10}]'
    ;;
  *) echo "mock aws: unsupported arguments: $*" >&2; exit 91;;
esac
MOCK
chmod +x "$tmp/bin/aws"

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_contains() { grep -Fqx "$2" "$1" || fail "expected '$2' in $1"; }
assert_equals() { [ "$1" = "$2" ] || fail "expected '$2', got '$1'"; }
grep -Fq 'AWS_CONFIG_FILE:=$state_dir/aws-config' "$script" || fail "routing must use the generated refreshable AWS profile"
grep -Fq 'AWS_PROFILE:=dd-demo-auto' "$script" || fail "routing must select the generated refreshable AWS profile"
grep -Fq 'AWS_REGION:=$(printf' "$script" || fail "routing must derive its region from the ALB rule ARN"
run_route() {
  PATH="$tmp/bin:$PATH" \
  MOCK_AWS_LOG="$tmp/aws.log" MOCK_ACTIONS="$tmp/actions.json" \
  STACK=hybrid STATE_DIR="$tmp/state" AWS_REGION=eu-west-1 \
  ALB_INVENTORY_LISTENER_ARN=public-listener-arn \
  ALB_INVENTORY_RULE_ARN=inventory-rule-arn \
  ALB_INVENTORY_100_TARGET_GROUP_ARN=tg-100 \
  ALB_INVENTORY_110_TARGET_GROUP_ARN=tg-110 \
  ALB_INVENTORY_120_TARGET_GROUP_ARN=tg-120 \
  "$script" "$@"
}

# Initial apply writes all target groups, including zero-weight releases, and remembers state.
run_route 0 90 10
grep -Fq 'elbv2 modify-rule --rule-arn inventory-rule-arn --actions file://' "$tmp/aws.log" || fail "modify-rule was not invoked with the inventory rule ARN"
grep -Fq 'elbv2 describe-rules --rule-arns inventory-rule-arn' "$tmp/aws.log" && fail "apply unexpectedly read the rule"
grep -Fq 'modify-listener' "$tmp/aws.log" && fail "storefront default listener was modified"
grep -Fq 'public-listener-arn' "$tmp/aws.log" && fail "public listener ARN was passed to AWS"
grep -Fq '"TargetGroupArn":"tg-100","Weight":0' "$tmp/actions.json" || fail "missing 1.0.0 zero weight"
grep -Fq '"TargetGroupArn":"tg-110","Weight":90' "$tmp/actions.json" || fail "missing 1.1.0 weight"
grep -Fq '"TargetGroupArn":"tg-120","Weight":10' "$tmp/actions.json" || fail "missing 1.2.0 weight"
assert_equals "$(cat "$tmp/state/routing-hybrid")" $'current=0 90 10\nprevious='

# A rollback restores the last accepted weights and swaps current/previous for a safe repeat rollback.
run_route 0 50 50
run_route --rollback
assert_equals "$(cat "$tmp/state/routing-hybrid")" $'current=0 90 10\nprevious=0 50 50'
grep -Fq '"TargetGroupArn":"tg-110","Weight":90' "$tmp/actions.json" || fail "rollback did not restore 1.1.0"
assert_equals "$(run_route --show)" "current=0 90 10 previous=0 50 50"

# Sync reads ALB listener state without applying a new routing action.
assert_equals "$(run_route --sync)" "alb-routing: live weights 1.0.0/1.1.0/1.2.0 = 0 90 10%"
grep -Fq 'elbv2 describe-rules --rule-arns inventory-rule-arn' "$tmp/aws.log" || fail "sync did not read the inventory rule ARN"
grep -Fq 'describe-listeners' "$tmp/aws.log" && fail "sync read the public listener default"

# Input validation fails before invoking aws.
if run_route 0 91 10 >/dev/null 2>&1; then fail "invalid weights unexpectedly succeeded"; fi
assert_equals "$(wc -l < "$tmp/aws.log" | tr -d ' ')" "4"

# Make sources the generated routing contract for direct canary targets.
cat > "$tmp/routing.env" <<'ENV'
ALB_INVENTORY_RULE_ARN=inventory-rule-arn
ALB_INVENTORY_100_TARGET_GROUP_ARN=tg-100
ALB_INVENTORY_110_TARGET_GROUP_ARN=tg-110
ALB_INVENTORY_120_TARGET_GROUP_ARN=tg-120
ENV
PATH="$tmp/bin:$PATH" MOCK_AWS_LOG="$tmp/aws.log" MOCK_ACTIONS="$tmp/actions.json" \
  STATE_DIR="$tmp/state" make -C "$repo_root" --no-print-directory MODE=cloud STACK=hybrid \
  ROUTING_ENV="$tmp/routing.env" route-baseline >/dev/null

# layer.sh receives the same explicit env-file path and sources it before syncing rule state.
cat > "$tmp/bin/dc" <<'MOCK'
#!/usr/bin/env bash
printf 'OK\n'
MOCK
chmod +x "$tmp/bin/dc"
PATH="$tmp/bin:$PATH" MOCK_AWS_LOG="$tmp/aws.log" MOCK_ACTIONS="$tmp/actions.json" \
  STATE_DIR="$tmp/state" DC="$tmp/bin/dc" MODE=cloud STACK=hybrid TOPOLOGY=hybrid OVERLAY="$repo_root" \
  ROUTING="$script" ROUTING_ENV="$tmp/routing.env" "$repo_root/compose/scripts/layer.sh" sync >/dev/null
grep -Fq 'elbv2 modify-rule --rule-arn inventory-rule-arn --actions file://' "$tmp/aws.log" || fail "Make did not propagate the rule ARN"
grep -Fq 'elbv2 describe-rules --rule-arns inventory-rule-arn' "$tmp/aws.log" || fail "layer sync did not propagate the rule ARN"
grep -Fq 'modify-listener' "$tmp/aws.log" && fail "Make or layer modified the storefront default listener"

echo "PASS: alb-routing apply, rollback, show, validation, and env propagation"
