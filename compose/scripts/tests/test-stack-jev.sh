#!/usr/bin/env bash
# Offline branch guard for optional Jev injection. It never sources env files or calls AWS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
AWS="$ROOT/terraform/aws"
fail() { printf 'test-stack-jev: %s\n' "$*" >&2; exit 1; }

bash -n "$STACK" || fail "stack.sh is not valid bash"
python3 - "$STACK" <<'PY'
from pathlib import Path
import sys

stack = Path(sys.argv[1]).read_text()
source = '  . "$ENV_DIR/.env"; set +a\n'
derivation = '  ENABLE_JEV=false\n  [ -n "${JEV_API_KEY:-}" ] && ENABLE_JEV=true\n'
if source + derivation not in stack:
    raise SystemExit("Jev enablement must be derived from non-empty JEV_API_KEY after env loading")
if '"-var=enable_jev=$ENABLE_JEV"' not in stack:
    raise SystemExit("terraform/aws must receive explicit enable_jev")
PY

terraform -chdir="$AWS" init -backend=false -input=false >/dev/null
base_args=(
  -var='stack=demo' -var='owner=test' -var='presenter_cidr=127.0.0.1/32'
  -var='on_prem_security_group_id=sg-test' -var='on_prem_private_ip=10.0.0.1' -var='on_prem_public_ip=127.0.0.1'
  -var='kafka_bootstrap=broker:9092' -var='schema_registry_url=https://schema.example.test'
  -var='confluent_environment_id=env-test' -var='kafka_cluster_id=lkc-test' -var='flink_compute_pool_id=pool-test'
)
secret_keys() {
  printf '%s\n' 'local.app_secret_keys["offer-worker"]' | terraform -chdir="$AWS" console "${base_args[@]}" "$1"
}

disabled="$(secret_keys -var='enable_jev=false')"
enabled="$(secret_keys -var='enable_jev=true')"
! grep -Fq '"JEV_API_KEY"' <<<"$disabled" || fail "false enable_jev must preserve the rule-default worker secrets"
grep -Fq '"JEV_API_KEY"' <<<"$enabled" || fail "true enable_jev must inject JEV_API_KEY into offer-worker"
grep -Eq '^    JEV_API_KEY[[:space:]]*=[[:space:]]*"JEV_API_KEY"$' "$AWS/main.tf" \
  || fail "JEV_API_KEY must retain its container environment name"
grep -Fq 'parameter/dd-demo/${var.stack}/${name}' "$AWS/main.tf" \
  || fail "ECS secrets must use stack-local SSM parameter ARNs"

printf 'test-stack-jev: PASS (Jev disabled and enabled branches)\n'
