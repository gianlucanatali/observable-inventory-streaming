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

# Local mode (MODE=dev): shop and panel on this laptop, APM and DSM by env (they need no state), and the
# Datadog dashboards only when terraform/datadog was applied for this stack. Reads state with TF_WORKSPACE, so the
# selected workspace of the cloud stack is never changed.
if [ "${MODE:-cloud}" = dev ]; then
  base="${LOCAL_BASE_URL:?links: LOCAL_BASE_URL must be set in local mode (the Makefile sets it from compose/dev.env)}"
  base="${base%/}"
  env="dd-demo-$STACK"
  printf '%s\n' "shop: $base/#/product/P0042" "control panel: $base/control/"
  note="Datadog dashboards for stack $STACK: not created"
  outputs=""
  if ! command -v terraform >/dev/null; then
    echo "links: $note (terraform is not installed). Optional: LOCAL.md, Datadog dashboards locally." >&2
  elif [ ! -d "$TF/datadog/.terraform" ]; then
    echo "links: $note (terraform/datadog is not initialised). Optional: LOCAL.md, Datadog dashboards locally." >&2
  else
    workspaces="$(terraform -chdir="$TF/datadog" workspace list)" || die "terraform/datadog workspace list failed"
    if ! printf '%s\n' "$workspaces" | sed 's/^[* ] *//' | grep -qx "$STACK"; then
      echo "links: $note (terraform/datadog has no workspace '$STACK'). Optional: LOCAL.md, Datadog dashboards locally." >&2
    else
      outputs="$(TF_WORKSPACE="$STACK" terraform -chdir="$TF/datadog" output -json)" \
        || die "terraform/datadog output failed for workspace $STACK"
    fi
  fi
  if [ -n "$outputs" ]; then
    DD_SITE_URL="$SITE" OUTPUTS="$outputs" python3 - <<'PY' || die "terraform/datadog outputs for workspace $STACK are not usable"
import json, os, sys
out = json.loads(os.environ["OUTPUTS"] or "{}")
if not out:
    print("links: Datadog dashboards: the terraform/datadog workspace has no outputs (not applied yet)", file=sys.stderr)
    sys.exit(0)
for label, key in (("overview dashboard", "overview_dashboard_url"), ("stock dashboard", "dashboard_url")):
    path = (out.get(key) or {}).get("value")
    if not isinstance(path, str) or not path.startswith("/"):
        sys.exit(f"links: terraform/datadog output {key} is absent or not a dashboard path; apply terraform/datadog again")
    print(f"{label}: {os.environ['DD_SITE_URL']}{path}")
PY
  fi
  printf '%s\n' \
    "APM inventory-api 1.1.0: $APM_SERVICE_BASE?env=$env&version=1.1.0" \
    "APM latency comparison: $APM_SERVICE_BASE?env=$env&compare_to=previous" \
    "DSM map: $DSM_BASE?env=$env"
  exit 0
fi

select_workspace() {
  local dir="$1" workspace="$2"
  terraform -chdir="$TF/$dir" workspace select "$workspace" >/dev/null \
    || die "Terraform workspace '$workspace' is unavailable in terraform/$dir; state is required first"
}

select_workspace datadog "$STACK"
overview_path="$(terraform -chdir="$TF/datadog" output -raw overview_dashboard_url)" \
  || die "terraform/datadog overview_dashboard_url is absent from state; run the datadog layer apply again"
stock_path="$(terraform -chdir="$TF/datadog" output -raw dashboard_url)" \
  || die "terraform/datadog dashboard_url is absent from state"
online_path="$(terraform -chdir="$TF/datadog" output -raw online_dashboard_url)" \
  || die "terraform/datadog online_dashboard_url is absent; enable/apply Fargate first"
select_workspace account account
cost_path="$(terraform -chdir="$TF/account" output -raw cost_dashboard_url)" \
  || die "terraform/account cost_dashboard_url is absent; account cost dashboard is not in state"
case "$overview_path" in /*) ;; *) die "terraform/datadog overview_dashboard_url is not a dashboard path";; esac
case "$stock_path" in /*) ;; *) die "terraform/datadog dashboard_url is not a dashboard path";; esac
case "$online_path" in /*) ;; *) die "terraform/datadog online_dashboard_url is not a dashboard path";; esac
case "$cost_path" in /*) ;; *) die "terraform/account cost_dashboard_url is not a dashboard path";; esac

env="dd-demo-$STACK"
printf '%s\n' \
  "overview dashboard: $SITE$overview_path" \
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