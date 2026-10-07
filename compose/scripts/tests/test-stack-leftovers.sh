#!/usr/bin/env bash
# Offline guard for the stack-down leftover check: the Tagging API is only the candidate list.
# Stubs `aws` and `confluent` on PATH; nothing reaches a real account.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-stack-leftovers: %s\n' "$*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/state"

# Fake aws: the tagging answer comes from $TAGGED; service answers from files named after the resource id.
cat >"$TMP/bin/aws" <<'SH'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"resourcegroupstaggingapi get-resources"*) cat "$TAGGED"; exit 0;;
  *"--profile"*|*"configure export-credentials"*) exit 0;;
esac
[ -n "${AWS_FAIL:-}" ] && case "$args" in *"$AWS_FAIL"*) echo "An error occurred (AccessDenied) when calling X: denied" >&2; exit 254;; esac
for id in $(ls "$LIVE"); do
  case "$args" in *"$id"*) cat "$LIVE/$id"; exit 0;; esac
done
case "$args" in
  *"ecs describe-services"*"--cluster gone-cluster"*) echo "An error occurred (ClusterNotFoundException) when calling DescribeServices: Cluster not found." >&2; exit 254;;
  *"elbv2 describe-load-balancers"*) echo "An error occurred (LoadBalancerNotFound) when calling DescribeLoadBalancers: not found" >&2; exit 254;;
esac
echo 0
SH
cat >"$TMP/bin/confluent" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "${CONFLUENT_JSON:-[]}"
SH
chmod +x "$TMP/bin/aws" "$TMP/bin/confluent"
export TAGGED="$TMP/tagged" LIVE="$TMP/live"
mkdir -p "$LIVE"

run() { # prints combined output, returns the status of leftover_check
  PATH="$TMP/bin:$PATH" STACK=hybrid OVERLAY="$ROOT" ENV_DIR="$TMP" STACK_SOURCE_ONLY=1 \
    bash -c '. "$OVERLAY/compose/scripts/stack.sh"; STATE_DIR="'"$TMP/state"'"; leftover_check' 2>&1
}
A=arn:aws:ecs:eu-west-1:000000000000
E=arn:aws:ec2:eu-west-1:000000000000

# 1. Lagging Tagging API: task definitions, deleted services/volumes/rules, a missing cluster, a deleted ALB.
printf '%s\t%s\n' \
  "$A:task-definition/dd-demo-hybrid-storefront:7" hybrid \
  "$A:service/dd-demo-hybrid/dd-demo-hybrid-storefront" hybrid \
  "$A:service/gone-cluster/svc" hybrid \
  "$A:cluster/dd-demo-hybrid" hybrid \
  "$E:volume/vol-0aaa" hybrid \
  "$E:security-group-rule/sgr-0bbb" hybrid \
  "arn:aws:elasticloadbalancing:eu-west-1:000000000000:loadbalancer/app/dd-demo-hybrid/123" hybrid \
  "arn:aws:s3:::dd-demo-cur-000000000000" account >"$TAGGED"
echo 1 >"$LIVE/dd-demo-cur-000000000000"
out="$(run)" || fail "clean account reported leftovers: $out"
case "$out" in *"no billable AWS leftovers for stack hybrid"*) ;; *) fail "missing clean line: $out";; esac
case "$out" in *"1 ECS task-definition revisions skipped"*"6 already deleted"*) ;; *) fail "wrong counts: $out";; esac
case "$out" in *"account-owned, kept on purpose"*dd-demo-cur*) ;; *) fail "account bucket not reported as account-owned: $out";; esac
case "$out" in *"no dd-demo-* Confluent environment"*) ;; *) fail "missing Confluent line: $out";; esac
case "$out" in *WARNING*) fail "clean run printed a warning: $out";; esac

# 2. A volume of this stack really exists, another stack's instance too: fail for ours only.
echo 1 >"$LIVE/vol-0aaa"
printf '%s\t%s\n' "$E:instance/i-0ccc" other >>"$TAGGED"
echo 1 >"$LIVE/i-0ccc"
out="$(run)" && fail "existing volume passed: $out"
case "$out" in *"WARNING: 1 resource(s) of stack hybrid still exist"*"vol-0aaa"*) ;; *) fail "volume not listed: $out";; esac
case "$out" in *"other stack, not part of this teardown"*"i-0ccc"*) ;; *) fail "other stack not separated: $out";; esac
rm "$LIVE/vol-0aaa"

# 3. An API error is fatal, not a silent pass.
out="$(AWS_FAIL=describe-security-group-rules run)" && fail "API error passed: $out"
case "$out" in *"AccessDenied"*"could not confirm"*sgr-0bbb*) ;; *) fail "API error not reported: $out";; esac

# 4. This stack's Confluent environment still exists: fail.
out="$(CONFLUENT_JSON='[{"name":"dd-demo-hybrid","id":"env-x"}]' run)" && fail "Confluent leftover passed: $out"
case "$out" in *"Confluent environment still exists"*dd-demo-hybrid*) ;; *) fail "Confluent leftover not reported: $out";; esac

# 6. Image repositories of this stack: a leftover by default, reported as kept with keep_images true.
printf '%s\t%s\n' "arn:aws:ecr:eu-west-1:000000000000:repository/dd-demo-hybrid/smoke" hybrid >>"$TAGGED"
echo 2147483648 >"$LIVE/smoke"   # the fake answers both the existence and the size query with this number
out="$(run)" && fail "existing ECR repository passed with keep_images false: $out"
case "$out" in *"WARNING: 1 resource(s) of stack hybrid still exist"*"repository/dd-demo-hybrid/smoke"*) ;; *) fail "repository not listed as leftover: $out";; esac
out="$(KEEP_IMAGES=true run)" || fail "kept ECR repository failed with keep_images true: $out"
case "$out" in *"kept on purpose (keep_images: true): 1 ECR repositories of stack hybrid, 2.00 GB stored, about \$0.20 per month at \$0.10 per GB-month"*) ;; *) fail "kept report: $out";; esac
case "$out" in *WARNING*) fail "kept repository printed a warning: $out";; esac
out="$(KEEP_IMAGES=yes run)" && fail "KEEP_IMAGES=yes passed: $out"
case "$out" in *"KEEP_IMAGES must be true or false"*) ;; *) fail "invalid KEEP_IMAGES message: $out";; esac
rm "$LIVE/smoke"

# 5. stack-down calls the check on every outcome (also after a failed layer) and fails when it fails.
[ "$(grep -c 'leftover_report || left=\$?' "$ROOT/compose/scripts/stack.sh")" -ge 3 ] \
  || fail "the leftover check does not run after a failed destroy, a clean destroy and a failed create"
grep -Fq '[ "$left" = 0 ] || { echo "stack.sh: stack $STACK: the leftover check did not pass' "$ROOT/compose/scripts/stack.sh" \
  || fail "down() does not fail on leftovers"
grep -Fq '    leftovers) leftover_check;;' "$ROOT/compose/scripts/stack.sh" || fail "no standalone leftovers command"
printf 'test-stack-leftovers: PASS (task defs skipped, lagging ARNs confirmed, API errors fatal)\n'
