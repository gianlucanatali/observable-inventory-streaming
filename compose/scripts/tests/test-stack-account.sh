#!/usr/bin/env bash
# Offline guards for account-first orchestration and secret handling.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
ACCOUNT="$ROOT/terraform/account"
fail() { printf 'test-stack-account: %s\n' "$*" >&2; exit 1; }
bash -n "$STACK" || fail "stack.sh is not valid bash"
python3 - "$STACK" "$ACCOUNT" <<'PY'
from pathlib import Path
import re
import sys
stack = Path(sys.argv[1]).read_text()
account = "\n".join(p.read_text() for p in Path(sys.argv[2]).glob("*.tf"))

def need(s):
    if s not in stack:
        raise SystemExit(f"missing stack.sh contract: {s}")
for s in [
    'TOPOLOGY="${TOPOLOGY:-hybrid}"',
    'hybrid_enabled() { [ "$TOPOLOGY" = hybrid ]',
    '[ "$1" = account ] && workspace=account',
    'step "terraform account (cost-meter identity and AWS CCM)" tf_apply account',
    'AWS_SOURCE_PROFILE="${AWS_PROFILE:-dd-demo}"',
    'AWS_CONFIG_FILE="$STATE_DIR/aws-config"',
    'credential_process = env -u AWS_CONFIG_FILE -u AWS_PROFILE aws configure export-credentials --profile %s --format process',
    'export AWS_CONFIG_FILE AWS_PROFILE',
    'unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_CREDENTIAL_EXPIRATION',
    'aws sts get-caller-identity --profile "$AWS_PROFILE"',
    'AWS login session unavailable; run: aws login --profile $AWS_SOURCE_PROFILE',
    '[ "${CONFIRM:-}" = yes ] && { echo "   (CONFIRM=yes: $1 -> yes)"; return 0; }',
    'tf_out account cost_meter_confluent_api_key',
    'tf_out account cost_meter_confluent_api_secret',
    'COST_METER_API_KEY=',
    'COST_METER_API_SECRET=',
    'stale .env.secrets lines must not override',
    'cost dashboard: $(tf_out account cost_dashboard_url)',
    'local tf_dirs="cloud vm datadog"',
]:
    need(s)
for forbidden in [
    'eval "$creds"',
    '--format env',
    'AWS session expires in less than 60 minutes',
]:
    if forbidden in stack:
        raise SystemExit(f"stale static AWS credential contract remains: {forbidden}")
account_step = stack.index('step "terraform account (cost-meter identity and AWS CCM)" tf_apply account')
cloud_step = stack.index('step "terraform cloud bootstrap (new-stack CRNs)" tf_bootstrap_cloud')
if account_step >= cloud_step:
    raise SystemExit("account Terraform must be applied before cloud bootstrap")
for s in [
    'resource "confluent_service_account" "cost_meter"',
    'resource "confluent_role_binding" "cost_meter_billing_admin"',
    'resource "confluent_role_binding" "cost_meter_metrics_viewer"',
    'resource "confluent_api_key" "cost_meter"',
    'resource "aws_bcmdataexports_export" "cur"',
    'resource "datadog_integration_aws_account_ccm_config" "main"',
    'report_type   = "CUR2.0"',
    'to       = confluent_service_account.cost_meter',
    'var.cost_meter_service_account_import_id',
    'to       = confluent_role_binding.cost_meter_billing_admin',
    'var.cost_meter_billing_admin_import_id',
    'to       = confluent_role_binding.cost_meter_metrics_viewer',
    'var.cost_meter_metrics_viewer_import_id',
    'to       = datadog_integration_confluent_account.cost_meter',
    'var.cost_meter_datadog_integration_import_id',
    'tags       = ["project:dd-demo", "purpose:cost"]',
    'output "cost_meter_confluent_api_key"',
    'output "cost_meter_confluent_api_secret"',
    'sensitive   = true',
]:
    if s not in account:
        raise SystemExit(f"missing account Terraform contract: {s}")
for pattern in (r'sa-[A-Za-z0-9]{6,}', r'rb-[A-Za-z0-9]{6,}', r'\b[0-9a-f]{32}\b'):
    if re.search(pattern, account):
        raise SystemExit(f"presenter-shaped import ID remains: {pattern}")
for forbidden in [
    'resource "aws_cur_report_definition" "cur"',
    'resource "datadog_aws_cur_config" "main"',
]:
    if forbidden in account:
        raise SystemExit(f"legacy CUR contract remains: {forbidden}")
cost = (Path(sys.argv[2]) / "cost.tf").read_text()
datadog_cost = Path(sys.argv[1]).parents[2] / "terraform" / "datadog" / "cost.tf"
if datadog_cost.exists():
    raise SystemExit("cost dashboard must be account-owned, not stack-owned")
for s in [
    'resource "datadog_dashboard_json" "cost"',
    'output "cost_dashboard_url"',
    'name    = "stack"',
    'prefix  = "stack"',
    'default = "*"',
    'max:dd_demo.cost.aggregate_usd_per_hour{${local.cost_scope},vendor:all}',
    'max:dd_demo.cost.aggregate_org_billed_list_usd_total{${local.cost_scope}}',
    'max:dd_demo.cost.usd_total{${local.cost_scope}} by {vendor,item}',
    'title       = "dd-demo cost [all stacks]"',
]:
    if s not in cost and s not in account:
        raise SystemExit(f"missing durable account cost dashboard contract: {s}")
if 'var.stack' in cost or 'terraform.workspace' in cost:
    raise SystemExit("account cost dashboard must not be bound to a stack workspace")
if 'echo "$value"' in stack or 'printf "$value"' in stack:
    raise SystemExit("secret value is printed")
print("test-stack-account: PASS (offline static guards)")
PY
