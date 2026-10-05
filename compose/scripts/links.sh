#!/usr/bin/env bash
# Print only non-sensitive presenter URLs from existing Terraform state.
set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly OVERLAY="$(cd "$HERE/../.." && pwd)"
readonly TF="$OVERLAY/terraform"
readonly SITE="https://app.datadoghq.eu"
readonly APM_SERVICE_BASE="https://app.datadoghq.eu/apm/services/inventory-api"
readonly DSM_BASE="https://app.datadoghq.eu/data-streams"
readonly TOPOLOGY="${TOPOLOGY:-hybrid}"

die() { echo "links: $*" >&2; exit 1; }
[ -n "${STACK:-}" ] || die "STACK is required (make MODE=cloud STACK=<name> links)"

select_workspace() {
  local dir="$1" workspace="$2"
  terraform -chdir="$TF/$dir" workspace select "$workspace" >/dev/null \
    || die "Terraform workspace '$workspace' is unavailable in terraform/$dir; state is required first"
}

select_workspace datadog "$STACK"
stock_path="$(terraform -chdir="$TF/datadog" output -raw dashboard_url)" \
  || die "terraform/datadog dashboard_url is absent from state"
online_path="$(terraform -chdir="$TF/datadog" output -raw online_dashboard_url)" \
  || die "terraform/datadog online_dashboard_url is absent; enable/apply Fargate first"
select_workspace account account
cost_path="$(terraform -chdir="$TF/account" output -raw cost_dashboard_url)" \
  || die "terraform/account cost_dashboard_url is absent; account cost dashboard is not in state"
case "$stock_path" in /*) ;; *) die "terraform/datadog dashboard_url is not a dashboard path";; esac
case "$online_path" in /*) ;; *) die "terraform/datadog online_dashboard_url is not a dashboard path";; esac
case "$cost_path" in /*) ;; *) die "terraform/account cost_dashboard_url is not a dashboard path";; esac

env="dd-demo-$STACK"
printf '%s\n' \
  "stock dashboard: $SITE$stock_path" \
  "online dashboard: $SITE$online_path" \
  "account-cost dashboard: $SITE$cost_path" \
  "APM inventory-api 1.1.0: $APM_SERVICE_BASE?env=$env&version=1.1.0" \
  "APM latency comparison: $APM_SERVICE_BASE?env=$env&compare_to=previous" \
  "DSM map: $DSM_BASE?env=$env"
if [ "$TOPOLOGY" = hybrid ] && grep -qx control-center "$OVERLAY/.state/stack-$STACK.layers" 2>/dev/null; then
  select_workspace vm "$STACK"
  control_center_ip="$(terraform -chdir="$TF/vm" output -raw public_ip)" \
    || die "terraform/vm public_ip is absent; VM state is required for Control Center"
  printf '%s\n' "control-center: http://$control_center_ip:9021"
fi