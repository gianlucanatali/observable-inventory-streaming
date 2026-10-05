#!/usr/bin/env bash
# Offline guards for W4. These tests inspect deployment code; they never source env files,
# call Docker/AWS/Terraform, or start a deployment.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK="$ROOT/compose/scripts/stack.sh"
LAYER="$ROOT/compose/scripts/layer.sh"
HYBRID="$ROOT/compose/compose.hybrid.yaml"
AWS_MAIN="$ROOT/terraform/aws/main.tf"
AWS_ECS="$ROOT/terraform/aws/ecs.tf"
AWS_VARS="$ROOT/terraform/aws/variables.tf"

fail() { printf 'test-stack-w4: %s\n' "$*" >&2; exit 1; }
contains() { grep -Fq -- "$1" "$STACK" || fail "stack.sh is missing: $1"; }

bash -n "$STACK" || fail "stack.sh is not valid bash"
contains 'hybrid_enabled()'
contains 'docker --context "$CTX" login --username AWS --password-stdin'
contains 'aws ecr get-login-password'
contains 'aws ecr delete-repository --repository-name "$repository" --force'
contains '--type SecureString'
contains '--overwrite'
contains 'aws ecs update-service'
contains '--force-new-deployment'
contains 'aws ecs wait services-stable'
contains 'pushed ARM64 application images to ECR'
contains 'ALB:'
contains 'terraform aws (ECS, ECR, ALB, SSM)'
contains '"-var=presenter_cidr=$cidr"'
contains '"-var=on_prem_security_group_id=$vm_sg"'
contains '"-var=on_prem_private_ip=$vm_private_ip"'
contains '"-var=confluent_environment_id=$cloud_env"'
contains '"-var=kafka_cluster_id=$cloud_cluster"'
contains '"-var=kafka_bootstrap=$cloud_bootstrap"'
contains '"-var=schema_registry_url=$cloud_sr"'
contains '"-var=flink_compute_pool_id=$cloud_flink"'
contains 'remote_vm_cost_inputs()'
contains 'tf_out vm env_file'
contains 'duplicate VM cost input'
contains 'malformed VM cost input'
! grep -Fq 'tf_out vm instance_type' "$STACK" || fail "AWS deploy must not require a VM output only created by VM apply"
! grep -Fq 'tf_out vm root_volume_gb' "$STACK" || fail "AWS deploy must not require a VM output only created by VM apply"
contains '"-var=remote_ec2_instance_type=$vm_instance_type"'
contains '"-var=remote_ebs_gb=$vm_root_ebs_gb"'
contains 'write_hybrid_endpoints()'
contains 'HYBRID_ONLINE_URL='
contains 'ELASTICACHE_REDIS_URL=redis://'
contains 'ALB_INVENTORY_RULE_ARN='
contains 'COST_ECS_CLUSTER_NAME='
contains 'COST_ALB_COUNT=1'
contains 'COST_ELASTICACHE_NODE_TYPE=cache.t4g.small'
contains '"-var=enable_fargate=true"'
contains '"-var=ingress_base_url=$(tf_out aws alb_url)"'
contains '"-var=datadog_synthetics_cidrs=$synthetics_cidrs"'
contains 'offers:on) tf_apply cloud; sync_ssm_secrets; tf_apply aws; tf_apply datadog;;'
contains 'dd-synthetics:on) tf_apply vm; tf_apply aws; tf_apply datadog;;'
contains 'chmod 600 "$ENV_CLOUD"'
contains 'service="dd-demo-$STACK-$service"'
contains '"$OVERLAY/log-router"'
contains 'repository_urls="$(tf_out_json aws ecr_repositories)"'
contains 'detach_cloud_schemas_before_destroy'
contains 'tf cloud state rm "$resource"'
contains 'detach_cloud_schemas_before_destroy; step "destroy terraform cloud" tf_apply cloud destroy'
contains "jq -er 'to_entries[] | .value | sub(\"^[^/]+/\"; \"\")'"
contains 'for repository in $(printf '\''%s'\'' "$repository_urls" | jq -er '\''to_entries[] | .value | sub("^[^/]+/"; "")'\''); do'
contains 'inventory-api-100 inventory-api-110 inventory-api-120 storefront stock-projector offer-worker demo-control cost-meter'
grep -Eq 'force_delete[[:space:]]*=[[:space:]]*true' "$AWS_MAIN" || fail "ECR repositories must be removable with populated images during stack-down"
grep -Fq 'name = "OFFERS_ENABLED", value = tostring(var.enable_offers)' "$AWS_ECS" \
  || fail "Fargate storefront must expose offers when the offers layer is enabled"
grep -Fq 'resource "aws_vpc_security_group_ingress_rule" "alb_http_synthetics"' "$AWS_MAIN" \
  || fail "hybrid ALB must admit the managed Synthetics locations"
grep -Fq 'variable "datadog_synthetics_cidrs"' "$AWS_VARS" \
  || fail "AWS topology must accept Datadog Synthetics CIDRs"
grep -Fq 'COST_REMOTE_EC2_INSTANCE_TYPE' "$AWS_ECS" \
  || fail "Fargate cost-meter must receive the remote VM instance type"
grep -Fq 'COST_REMOTE_EBS_GB' "$AWS_ECS" \
  || fail "Fargate cost-meter must receive the remote VM root EBS size"
grep -Fq 'COST_REMOTE_PUBLIC_IPV4_COUNT' "$AWS_ECS" \
  || fail "Fargate cost-meter must receive the remote VM public IPv4 count"
# H-04/H-11: the ALB rule is a live routing boundary and must only admit ready inventory tasks.
grep -A26 '^resource "aws_lb_listener_rule" "inventory" {' "$AWS_MAIN" | grep -Fq 'ignore_changes = [action]' \
  || fail "Terraform must ignore out-of-band ALB inventory routing action changes"
grep -A12 '^resource "aws_lb_target_group" "inventory" {' "$AWS_MAIN" | grep -Fq 'health_check { path = "/readyz" }' \
  || fail "inventory ALB target groups must use /readyz"
grep -A28 '^resource "aws_ecs_service" "app" {' "$AWS_ECS" | grep -Fq 'health_check_grace_period_seconds' \
  || fail "inventory ECS services need an ALB health-check grace period"
# RUM parity: when dd-rum is enabled, only the ECS storefront receives the
# application ID and its client token must stay on the stack-local SSM path.
contains 'if has dd-rum && [ -f "$DD_APPLIED" ]; then'
contains 'rum_application_id="$(tf_out datadog rum_application_id)"'
contains '"-var=enable_dd_rum=$rum_enabled"'
contains '"-var=rum_application_id=$rum_application_id"'
grep -Fq 'variable "enable_dd_rum"' "$AWS_VARS" \
  || fail "AWS Terraform must gate storefront RUM on dd-rum"
grep -Fq 'var.enable_dd_rum ? [{ name = "DD_RUM_APPLICATION_ID", value = var.rum_application_id }] : []' "$AWS_ECS" \
  || fail "Fargate storefront must receive the RUM application ID when dd-rum is enabled"
grep -Fq 'var.enable_dd_rum ? ["DD_RUM_CLIENT_TOKEN"] : []' "$AWS_MAIN" \
  || fail "RUM client token must be in the stack-local secret parameter allowlist"
grep -Fq 'storefront        = concat(' "$AWS_MAIN" \
  || fail "only the storefront may receive the RUM client token"
grep -Fq 'resources = values(local.secret_parameter_arns)' "$AWS_ECS" \
  || fail "ECS execution role must remain limited to stack-local secret parameter ARNs"
grep -Fq 'secrets     = local.app_secrets[each.key]' "$AWS_ECS" \
  || fail "Fargate task definitions must use the generated app secret contract"
grep -Fq 'valueFrom = local.secret_parameter_arns[key]' "$AWS_ECS" \
  || fail "Fargate RUM client token must resolve from a stack-local SSM parameter ARN"
! grep -Eq 'name[[:space:]]*=[[:space:]]*"DD_RUM_CLIENT_TOKEN"[^}]*value[[:space:]]*=' "$AWS_ECS" \
  || fail "RUM client token must not be a plaintext ECS environment value"
contains '|DD_RUM_CLIENT_TOKEN) return 0;;'
python3 - "$STACK" <<'PY' || fail "RUM apply ordering must update the ECS storefront task definition"
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()

def function_body(start, end):
    return text[text.index(start):text.index(end, text.index(start))]

def require_order(body, snippets):
    position = -1
    for snippet in snippets:
        next_position = body.find(snippet, position + 1)
        if next_position < 0:
            raise SystemExit(f"missing or out-of-order: {snippet}")
        position = next_position

require_order(function_body("up() {", "layer_tf() {"), [
    'step "terraform datadog (dashboard, monitors, layers)" tf_apply datadog',
    'step "env file (with RUM ids)" write_env_file',
    'if has dd-rum && hybrid_enabled; then',
    'step "sync RUM client token to SSM SecureString" sync_ssm_secrets',
    'step "terraform aws (storefront RUM configuration)" tf_apply aws',
])
require_order(function_body("layer_tf() {", "layer_post() {"), [
    'releases:*|dd-rum:*) tf_apply datadog;;',
    'write_env_file',
    'if [ "$layer" = dd-rum ] && hybrid_enabled; then',
    'if [ "$st" = on ]; then sync_ssm_secrets; fi',
    'tf_apply aws',
])
PY
grep -Fq 'hybrid_ecs_layer "$layer"' "$LAYER" \
  || fail "hybrid layer toggles must not start duplicate VM release/offer containers"
grep -A1 '^  datadog-agent:' "$HYBRID" | grep -Fq 'profiles: !reset []' \
  || fail "hybrid VM must run its Datadog Agent for watchdog/host metrics"
grep -A4 '^  datadog-agent:' "$HYBRID" | grep -Fq 'configs: !override' \
  || fail "hybrid VM Agent must override dead Redis/nginx checks"
! grep -A8 '^  datadog-agent:' "$HYBRID" | grep -Eq 'dd_redisdb|dd_nginx' \
  || fail "hybrid VM Agent must not mount dead Redis/nginx checks"
grep -A8 '^  datadog-agent:' "$HYBRID" | grep -Fq 'source: dd_postgres' \
  || fail "hybrid VM Agent must retain its valid PostgreSQL check"
grep -A9 '^  supplier-sim:' "$HYBRID" | grep -Fq 'REDIS_URL: ${ELASTICACHE_REDIS_URL:' \
  || fail "hybrid supplier-sim must use ElastiCache rather than the disabled local Redis service"


# Secret-handling guards: values may only flow through --value and are never printed.
contains 'ssm_secret_key "$key" || continue'
contains '>/dev/null || die "could not sync SSM parameter'
! grep -Eq 'echo .*\$value|printf .*\$value' "$STACK" || fail "secret value is printed"
! grep -Eq 'set -x|aws configure list' "$STACK" || fail "credential tracing was added"

printf 'test-stack-w4: PASS (offline static guards)\n'
