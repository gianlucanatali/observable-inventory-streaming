#!/usr/bin/env bash
# Usage: alb-routing.sh <w100> <w110> <w120> | alb-routing.sh --rollback | alb-routing.sh --show
# Applies the weighted forward action of the ALB inventory rule. Like nginx/apply-routing.sh,
# it records current and previous accepted weights in overlay/.state/routing-<stack> for rollback.
# Required environment: ALB_INVENTORY_RULE_ARN and ALB_INVENTORY_{100,110,120}_TARGET_GROUP_ARN.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
state_dir="${STATE_DIR:-$here/../../.state}"
state="$state_dir/routing-${STACK:?alb-routing.sh: STACK must be set (the Makefile exports it)}"
mkdir -p "$state_dir"
printf '*\n' > "$state_dir/.gitignore"

require_env() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || { echo "alb-routing.sh: $name must be set from the hybrid Terraform output/env file" >&2; exit 2; }
  done
}

configure_aws() {
  : "${AWS_CONFIG_FILE:=$state_dir/aws-config}"
  : "${AWS_PROFILE:=dd-demo-auto}"
  # ./demo exports the source profile (e.g. dd-demo); the generated config only defines its "-auto" twin.
  case "$AWS_PROFILE" in *-auto) ;; *)
    if [ -f "$AWS_CONFIG_FILE" ] && grep -q "^\[profile ${AWS_PROFILE}-auto\]" "$AWS_CONFIG_FILE"; then AWS_PROFILE="${AWS_PROFILE}-auto"; fi;;
  esac
  : "${AWS_REGION:=$(printf '%s' "$ALB_INVENTORY_RULE_ARN" | cut -d: -f4)}"
  export AWS_CONFIG_FILE AWS_PROFILE AWS_REGION
  [ -n "$AWS_REGION" ] || { echo "alb-routing.sh: could not derive AWS region from $ALB_INVENTORY_RULE_ARN" >&2; exit 2; }
}

validate_weights() {
  local sum=0 w
  [ "$#" -eq 3 ] || { echo "alb-routing.sh: expected 3 weights <w100> <w110> <w120>, got $#" >&2; exit 2; }
  for w in "$@"; do
    case "$w" in ''|*[!0-9]*) echo "alb-routing.sh: weight '$w' is not a non-negative integer" >&2; exit 2;; esac
    [ "$w" -le 100 ] || { echo "alb-routing.sh: weight $w is above 100" >&2; exit 2; }
    sum=$((sum + w))
  done
  [ "$sum" -eq 100 ] || { echo "alb-routing.sh: weights $1/$2/$3 sum to $sum, must be 100" >&2; exit 2; }
}

sync_live() {
  require_env ALB_INVENTORY_RULE_ARN \
    ALB_INVENTORY_100_TARGET_GROUP_ARN \
    ALB_INVENTORY_110_TARGET_GROUP_ARN \
    ALB_INVENTORY_120_TARGET_GROUP_ARN
  configure_aws
  local target_groups weights
  target_groups="$(aws elbv2 describe-rules --rule-arns "$ALB_INVENTORY_RULE_ARN" \
    --query 'Rules[0].Actions[?Type==`forward`].ForwardConfig.TargetGroups | [0]' --output json)" \
    || { echo "alb-routing.sh: could not read the weighted forward action for $ALB_INVENTORY_RULE_ARN" >&2; exit 1; }
  weights="$(ALB_ROUTING_TARGET_GROUPS="$target_groups" \
    ALB_ROUTING_100="$ALB_INVENTORY_100_TARGET_GROUP_ARN" \
    ALB_ROUTING_110="$ALB_INVENTORY_110_TARGET_GROUP_ARN" \
    ALB_ROUTING_120="$ALB_INVENTORY_120_TARGET_GROUP_ARN" python3 - <<'PY'
import json
import os
import sys

try:
    groups = json.loads(os.environ["ALB_ROUTING_TARGET_GROUPS"])
except json.JSONDecodeError as error:
    raise SystemExit(f"alb-routing.sh: AWS returned invalid target group JSON: {error}")
expected = [os.environ[f"ALB_ROUTING_{release}"] for release in ("100", "110", "120")]
if not isinstance(groups, list) or len(groups) != len(expected):
    raise SystemExit("alb-routing.sh: weighted forward action must contain exactly the three inventory target groups")
weights = {}
for group in groups:
    if not isinstance(group, dict) or set(group) != {"TargetGroupArn", "Weight"}:
        raise SystemExit("alb-routing.sh: AWS returned an invalid weighted target group")
    arn, weight = group["TargetGroupArn"], group["Weight"]
    if arn not in expected or arn in weights or not isinstance(weight, int) or isinstance(weight, bool):
        raise SystemExit("alb-routing.sh: AWS returned unexpected target group weights")
    weights[arn] = weight
if set(weights) != set(expected):
    raise SystemExit("alb-routing.sh: AWS did not return every inventory target group")
print(" ".join(str(weights[arn]) for arn in expected))
PY
)" || exit 1
  validate_weights $weights
  echo "alb-routing: live weights 1.0.0/1.1.0/1.2.0 = $weights%"
}

current=""
previous=""
if [ -f "$state" ]; then
  current="$(sed -n 's/^current=//p' "$state")"
  previous="$(sed -n 's/^previous=//p' "$state")"
fi

case "${1:-}" in
  --show)
    echo "current=${current:-unknown} previous=${previous:-none}"
    exit 0
    ;;
  --rollback)
    [ -n "$previous" ] || { echo "alb-routing.sh: no previous routing recorded in $state" >&2; exit 1; }
    set -- $previous
    ;;
  --sync)
    sync_live
    exit 0
    ;;
esac

validate_weights "$@"
require_env ALB_INVENTORY_RULE_ARN \
  ALB_INVENTORY_100_TARGET_GROUP_ARN \
  ALB_INVENTORY_110_TARGET_GROUP_ARN \
  ALB_INVENTORY_120_TARGET_GROUP_ARN
configure_aws

new="$1 $2 $3"
actions="$(mktemp "$state_dir/alb-routing.XXXXXX")"
trap 'rm -f "$actions"' EXIT
printf '%s' '[{"Type":"forward","ForwardConfig":{"TargetGroups":[' > "$actions"
printf '%s' "{\"TargetGroupArn\":\"${ALB_INVENTORY_100_TARGET_GROUP_ARN}\",\"Weight\":$1}," >> "$actions"
printf '%s' "{\"TargetGroupArn\":\"${ALB_INVENTORY_110_TARGET_GROUP_ARN}\",\"Weight\":$2}," >> "$actions"
printf '%s' "{\"TargetGroupArn\":\"${ALB_INVENTORY_120_TARGET_GROUP_ARN}\",\"Weight\":$3}" >> "$actions"
printf '%s\n' ']},"Order":1}]' >> "$actions"

aws_err="$(mktemp "$state_dir/alb-routing-err.XXXXXX")"
trap 'rm -f "$actions" "$aws_err"' EXIT
if ! aws elbv2 modify-rule --rule-arn "$ALB_INVENTORY_RULE_ARN" --actions "file://$actions" 2> "$aws_err"; then
  cat "$aws_err" >&2
  echo "alb-routing.sh: AWS rejected the weighted forward action for $ALB_INVENTORY_RULE_ARN" >&2
  if grep -qiE 'expired|ExpiredToken|login|sso|refresh token|credentials' "$aws_err"; then
    echo "alb-routing.sh: the AWS session looks expired; run: aws login --profile ${AWS_PROFILE:-dd-demo}, then retry" >&2
  fi
  exit 1
fi
if [ "$new" != "$current" ]; then
  printf 'current=%s\nprevious=%s\n' "$new" "$current" > "$state"
fi
echo "alb-routing: weights 1.0.0/1.1.0/1.2.0 = $new%"
