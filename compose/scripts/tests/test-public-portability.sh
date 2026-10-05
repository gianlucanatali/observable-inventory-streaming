#!/usr/bin/env bash
# Offline contracts for public-repo account/lifecycle portability. No cloud calls.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
MAKEFILE="$ROOT/Makefile"
STACK="$ROOT/compose/scripts/stack.sh"
SECRETS="$ROOT/compose/scripts/gen-secrets.sh"
ACCOUNT="$ROOT/terraform/account"
IMPORTS="$ACCOUNT/imports.tf"
MAIN="$ACCOUNT/main.tf"
VARS="$ACCOUNT/variables.tf"

fail() { printf 'test-public-portability: %s\n' "$*" >&2; exit 1; }
for script in "$STACK" "$SECRETS"; do bash -n "$script" || fail "$script is not valid bash"; done

# Execute the ENV_DIR contract in isolated layouts. STACK_SOURCE_ONLY keeps the
# production dispatcher out of these tests while exercising the real resolver.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
copy_overlay() {
  local repo="$1/repo"
  mkdir -p "$repo/overlay/compose/scripts"
  cp "$MAKEFILE" "$repo/overlay/Makefile"
  cp "$STACK" "$repo/overlay/compose/scripts/stack.sh"
  cp "$SECRETS" "$repo/overlay/compose/scripts/gen-secrets.sh"
}
resolved_stack_env_dir() {
  local repo="$1" override="${2:-}"
  if [ -n "$override" ]; then
    ENV_DIR="$override" STACK=test OVERLAY="$repo/overlay" STACK_SOURCE_ONLY=1 bash -c '. "$OVERLAY/compose/scripts/stack.sh"; printf "%s\n" "$ENV_DIR"'
  else
    env -u ENV_DIR STACK=test OVERLAY="$repo/overlay" STACK_SOURCE_ONLY=1 bash -c '. "$OVERLAY/compose/scripts/stack.sh"; printf "%s\n" "$ENV_DIR"'
  fi
}
resolved_make_env_dir() {
  local repo="$1" override="${2:-}"
  if [ -n "$override" ]; then
    ENV_DIR="$override" make -C "$repo/overlay" --no-print-directory -pn help | awk '/^ENV_DIR = / { sub(/^ENV_DIR = /, ""); print; exit }'
  else
    make -C "$repo/overlay" --no-print-directory -pn help | awk '/^ENV_DIR := / { sub(/^ENV_DIR := /, ""); print; exit }'
  fi
}
assert_eq() { [ "$1" = "$2" ] || fail "$3: got '$1', expected '$2'"; }
assert_dir_eq() {
  local actual expected
  actual="$(cd "$1" && pwd -P)"; expected="$(cd "$2" && pwd -P)"
  assert_eq "$actual" "$expected" "$3"
}

for layout in override overlay parent default; do
  base="$TMP/$layout"; mkdir -p "$base"; copy_overlay "$base"; repo="$base/repo"; overlay="$repo/overlay"; override=""
  case "$layout" in
    override) mkdir "$base/explicit"; override="$base/explicit"; expected="$override"; actual="$(resolved_stack_env_dir "$repo" "$expected")";;
    overlay) : > "$overlay/.env"; expected="$overlay"; actual="$(resolved_stack_env_dir "$repo")";;
    parent) : > "$repo/.env"; expected="$repo"; actual="$(resolved_stack_env_dir "$repo")";;
    default) expected="$overlay"; actual="$(resolved_stack_env_dir "$repo")";;
  esac
  assert_dir_eq "$actual" "$expected" "stack ENV_DIR $layout"
  assert_dir_eq "$(resolved_make_env_dir "$repo" "${override:-}")" "$expected" "Make ENV_DIR $layout"
done

# Mock AWS only at the CLI boundary. The cleanup function receives names from
# describe-parameters and must neither fetch values nor widen the stack prefix.
MOCK_BIN="$TMP/mock-bin"; mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/aws" <<'AWS'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$AWS_LOG"
case "$1 $2" in
  'ssm describe-parameters')
    [ "${AWS_LIST_FAIL:-0}" = 0 ] || exit 23
    printf '%s\n' "${AWS_MOCK_NAMES:-None}";;
  'ssm delete-parameters')
    [ "${AWS_DELETE_FAIL:-0}" = 0 ] || exit 24;;
  *) exit 99;;
esac
AWS
chmod +x "$MOCK_BIN/aws"
run_ssm_cleanup() {
  local names="$1" log="$2"
  PATH="$MOCK_BIN:$PATH" AWS_LOG="$log" AWS_MOCK_NAMES="$names" STACK=demo OVERLAY="$ROOT" STACK_SOURCE_ONLY=1 \
    bash -c '. "$OVERLAY/compose/scripts/stack.sh"; delete_stack_ssm_parameters'
}
ssm_log="$TMP/ssm.log"; : > "$ssm_log"
run_ssm_cleanup None "$ssm_log"
[ "$(grep -c '^ssm describe-parameters ' "$ssm_log")" = 1 ] || fail "empty SSM cleanup did not enumerate once"
[ "$(grep -c '^ssm delete-parameters ' "$ssm_log" || true)" = 0 ] || fail "empty SSM cleanup called AWS delete"
names=""; for n in $(seq 1 11); do names="$names /dd-demo/demo/P$n"; done
: > "$ssm_log"; run_ssm_cleanup "$names" "$ssm_log"
grep -Fq 'ssm describe-parameters --parameter-filters Key=Path,Option=Recursive,Values=/dd-demo/demo/' "$ssm_log" \
  || fail "SSM enumeration did not use the exact recursive Path filter"
[ "$(grep -c '^ssm delete-parameters ' "$ssm_log")" = 2 ] || fail "11 SSM names were not deleted in 10+1 batches"
grep '^ssm delete-parameters ' "$ssm_log" | head -n1 | grep -Fq '/dd-demo/demo/P10' || fail "first SSM delete batch did not contain ten names"
grep '^ssm delete-parameters ' "$ssm_log" | tail -n1 | grep -Fq '/dd-demo/demo/P11' || fail "second SSM delete batch did not contain final name"
if run_ssm_cleanup '/dd-demo/other/P1' "$ssm_log" >/dev/null 2>&1; then fail "out-of-scope SSM name was accepted"; fi
if PATH="$MOCK_BIN:$PATH" AWS_LOG="$ssm_log" AWS_LIST_FAIL=1 STACK=demo OVERLAY="$ROOT" STACK_SOURCE_ONLY=1 bash -c '. "$OVERLAY/compose/scripts/stack.sh"; delete_stack_ssm_parameters' >/dev/null 2>&1; then fail "SSM list failure was accepted"; fi
if PATH="$MOCK_BIN:$PATH" AWS_LOG="$ssm_log" AWS_MOCK_NAMES=/dd-demo/demo/P1 AWS_DELETE_FAIL=1 STACK=demo OVERLAY="$ROOT" STACK_SOURCE_ONLY=1 bash -c '. "$OVERLAY/compose/scripts/stack.sh"; delete_stack_ssm_parameters' >/dev/null 2>&1; then fail "SSM delete failure was accepted"; fi

# Source-only account-down runs prove gates, override scope/mode, and EXIT trap
# cleanup without a Terraform or cloud invocation.
account_overlay="$TMP/account-overlay"; cp -R "$ROOT/terraform" "$account_overlay-terraform"; mkdir -p "$account_overlay/terraform"; mv "$account_overlay-terraform/account" "$account_overlay/terraform/account"; mkdir -p "$account_overlay/compose/scripts"; cp "$STACK" "$account_overlay/compose/scripts/stack.sh"
run_account_down() {
  local result="$1" capture="$2"
  CONFIRM=yes ACCOUNT_DOWN_DESTROY=yes STACK=account OVERLAY="$account_overlay" STACK_SOURCE_ONLY=1 TF_RESULT="$result" TF_CAPTURE="$capture" bash -c '
    . "$OVERLAY/compose/scripts/stack.sh"
    tf_apply() { cp "$ACCOUNT_DOWN_OVERRIDE" "$TF_CAPTURE"; [ "$TF_RESULT" = success ]; }
    account_down'
}
for flag in missing wrong; do
  if CONFIRM=yes ACCOUNT_DOWN_DESTROY="${flag/missing/}" STACK=account OVERLAY="$account_overlay" STACK_SOURCE_ONLY=1 bash -c '. "$OVERLAY/compose/scripts/stack.sh"; account_down' >/dev/null 2>&1; then fail "account-down accepted $flag destructive flag"; fi
  [ ! -e "$account_overlay/terraform/account/account-down_override.tf.json" ] || fail "account-down wrote override before $flag gate refusal"
done
success_capture="$TMP/account-success.json"; run_account_down success "$success_capture"
[ "$(stat -f '%Lp' "$success_capture")" = 600 ] || fail "account-down override was not mode 600"
python3 - "$success_capture" <<'PY' || fail "account-down override content/scope is incomplete"
import json
import sys

actual = json.load(open(sys.argv[1]))
expected = {
    "resource": {
        "confluent_service_account": {"cost_meter": {"lifecycle": {"prevent_destroy": False}}},
        "confluent_role_binding": {
            "cost_meter_billing_admin": {"lifecycle": {"prevent_destroy": False}},
            "cost_meter_metrics_viewer": {"lifecycle": {"prevent_destroy": False}},
        },
        "aws_s3_bucket": {"cur": {"force_destroy": True}},
    }
}
if actual != expected:
    raise SystemExit(f"unexpected override scope: {actual!r}")
PY
[ ! -e "$account_overlay/terraform/account/account-down_override.tf.json" ] || fail "account-down success left override behind"
failure_capture="$TMP/account-failure.json"
if run_account_down failure "$failure_capture" >/dev/null 2>&1; then fail "simulated account-down failure succeeded"; fi
[ -f "$failure_capture" ] || fail "account-down failure did not reach simulated Terraform"
[ ! -e "$account_overlay/terraform/account/account-down_override.tf.json" ] || fail "account-down failure left override behind"

python3 - "$MAKEFILE" "$STACK" "$SECRETS" "$IMPORTS" "$MAIN" "$VARS" <<'PY'
from pathlib import Path
import re
import sys
make, stack, secrets, imports, main, vars_ = (Path(path).read_text() for path in sys.argv[1:])

def require(text, snippet, name):
    if snippet not in text:
        raise SystemExit(f"{name} missing: {snippet}")

# One ENV_DIR contract: explicit override, overlay .env, parent .env, overlay creation root.
for text, name in ((make, "Makefile"), (stack, "stack.sh"), (secrets, "gen-secrets.sh")):
    require(text, "ENV_DIR", name)
for snippet in ('[ -n "${ENV_DIR:-}" ]', '[ -f "$OVERLAY/.env" ]', '[ -f "$ROOT/.env" ]', 'ENV_DIR="$OVERLAY"'):
    require(stack, snippet, "stack.sh resolver")
for snippet in ('ENV_DIR ?=', 'ENV_DIR := $(CURDIR)', 'ENV_DIR := $(abspath $(CURDIR)/..)', 'export ENV_DIR', '$(ENV_DIR)/.env', '$(ENV_DIR)/.env.secrets', '$(ENV_DIR)/.env.cloud-$(STACK)'):
    require(make, snippet, "Makefile ENV_DIR")
require(secrets, '[ -n "${ENV_DIR:-}" ]', "gen-secrets override")
require(secrets, 'target="$ENV_DIR/.env.secrets"', "gen-secrets target")
for snippet in ('ENV_CLOUD="$ENV_DIR/.env.cloud-$STACK"', '"$ENV_DIR/.env.secrets"', '"$ENV_DIR/.env"'):
    require(stack, snippet, "stack.sh ENV_DIR consumers")
for snippet in ('[ -f "$ENV_DIR/.env" ]', 'grep -q "^$n=." "$ENV_DIR/.env"'):
    require(stack, snippet, "stack.sh preflight ENV_DIR")

# Imports must be independent opt-ins, not presenter identifiers.
for name in ('cost_meter_service_account_import_id', 'cost_meter_billing_admin_import_id', 'cost_meter_metrics_viewer_import_id', 'cost_meter_datadog_integration_import_id'):
    require(vars_, f'variable "{name}"', "account import variable")
    require(vars_, 'default     = ""', "empty import default")
    require(imports, f'var.{name}', "conditional import")
forbidden_shapes = (r'sa-[A-Za-z0-9]{6,}', r'rb-[A-Za-z0-9]{6,}', r'\b[0-9a-f]{32}\b')
for pattern in forbidden_shapes:
    if re.search(pattern, imports):
        raise SystemExit(f"presenter-shaped identifier remains in public config: {pattern}")
require(main, 'display_name = "dd-demo-sa-cost-meter"', "stack-neutral service account")

# SSM cleanup follows successful AWS destroy and stays under precisely one stack prefix.
require(stack, 'delete_stack_ssm_parameters()', "SSM cleanup function")
require(stack, 'aws ssm describe-parameters --parameter-filters "Key=Path,Option=Recursive,Values=$path"', "SSM enumeration")
require(stack, 'path="/dd-demo/$STACK/"', "SSM scope")
require(stack, 'aws ssm delete-parameters --names', "SSM batch delete")
require(stack, 'batch_size=10', "SSM bounded batch")
aws_destroy = stack.index('step "destroy terraform aws" tf_apply aws destroy')
cleanup = stack.index('delete_stack_ssm_parameters', aws_destroy)
if cleanup <= aws_destroy:
    raise SystemExit("SSM cleanup must run after successful AWS destroy")
if 'get-parameters-by-path --' in stack or 'get-parameters --' in stack or 'get-parameter --' in stack:
    raise SystemExit("SSM cleanup must not read parameter values")

# Account-down has two approvals, a narrow temporary override, cleanup trap, and saved plan flow.
require(stack, 'ACCOUNT_DOWN_DESTROY', "second account-down flag")
require(stack, 'write_account_down_override()', "override writer")
require(stack, 'rm -f "$ACCOUNT_DOWN_OVERRIDE"', "override cleanup")
require(stack, 'trap', "override cleanup trap")
for snippet in ('"confluent_service_account"', '"cost_meter_billing_admin"', '"cost_meter_metrics_viewer"', '"aws_s3_bucket"', '"prevent_destroy": false', '"force_destroy": true', 'chmod 600 "$tmp"'):
    require(stack, snippet, "account-down override")
account_down = stack[stack.index('account_down() {'):stack.index('\ndispatch_command()', stack.index('account_down() {'))]
if '[ "${CONFIRM:-}" = yes ]' not in account_down or '[ "${ACCOUNT_DOWN_DESTROY:-}" = yes ]' not in account_down:
    raise SystemExit("account-down must require normal and destructive confirmations")
if 'tf_apply account destroy' not in account_down:
    raise SystemExit("account-down must retain the saved-plan flow")
require(make, 'ACCOUNT_DOWN_DESTROY=yes', "Make help flag")
print("test-public-portability: PASS (offline portability contracts)")
PY
