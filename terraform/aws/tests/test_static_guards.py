#!/usr/bin/env python3
"""Offline static guards for Terraform; no AWS calls or secrets."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
text = "\n".join(path.read_text() for path in ROOT.glob("*.tf"))
router_config = (ROOT.parents[1] / "log-router" / "extra.conf").read_text()
stack_script = (ROOT.parents[1] / "compose" / "scripts" / "stack.sh").read_text()

required = [
    'node_type            = "cache.t4g.small"', 'num_cache_nodes      = 1',
    'cpu_architecture        = "ARM64"',
    'gcr.io/datadoghq/agent:7.83.3@sha256:c5bf5ec9be0c51d3d2d47bdadbb82680b5c83ceb9cae8457fe44aa736a1784ad',
    'values = ["/api/availability/*"]', 'values = ["/control", "/control/*"]',
    'output "alb_listener_arn"', 'output "ecr_repositories"', 'output "ecs_services"',
    'image_tag', 'default     = "dev"', 'on_prem_private_ip',
    'PROJECTOR_KAFKA_API_KEY', 'DEMO_CONTROL_KAFKA_API_KEY', 'COST_METER_API_KEY',
    'KAFKA_BOOTSTRAP', 'SR_URL', 'KAFKA_SECURITY_PROTOCOL', 'STORE_HOSTS',
    'CONFLUENT_ENVIRONMENT_ID', 'KAFKA_CLUSTER_ID', 'FLINK_COMPUTE_POOL_ID',
    'aws_lb_target_group.demo_control',
]
missing = [snippet for snippet in required if snippet not in text]
for app in ['stock-projector', 'inventory-api-100', 'inventory-api-110', 'inventory-api-120', 'storefront', 'offer-worker', 'demo-control', 'cost-meter']:
    if app not in text:
        missing.append(app)

# W4 DSM/log-correlation contract: only traffic-producing core applications
# receive the tracer flags. The declaration is data-driven so every generated
# ECS app environment has one auditable selection point.
match = re.search(r'datadog_instrumented_apps\s*=\s*toset\(\[(?P<apps>.*?)\]\)', text, re.DOTALL)
expected_instrumented_apps = {
    'stock-projector', 'offer-worker', 'storefront',
    'inventory-api-100', 'inventory-api-110', 'inventory-api-120',
}
if match is None:
    missing.append('datadog_instrumented_apps declaration')
else:
    actual_instrumented_apps = set(re.findall(r'"([^"]+)"', match.group('apps')))
    if actual_instrumented_apps != expected_instrumented_apps:
        missing.append(
            'datadog_instrumented_apps must contain exactly '
            f'{sorted(expected_instrumented_apps)}; found {sorted(actual_instrumented_apps)}'
        )

required_dsm_instrumentation = (
    'contains(local.datadog_instrumented_apps, name) ? '
    '[{ name = "DD_DATA_STREAMS_ENABLED", value = "true" }] : []'
)
if required_dsm_instrumentation not in text:
    missing.append('generated DSM environment flag')
if '[{ name = "DD_LOGS_INJECTION", value = "true" }],' not in text:
    missing.append('generated application-wide log-injection environment flag')

# Q8: application stdout is duplicated by FireLens to Datadog and the existing
# CloudWatch group.  The API key must remain a task-definition secret option,
# while the injected Python correlation fields remain enabled for every app.
required_q8_logging = [
    '"log-router"',
    'aws_ecr_repository.app["log-router"].repository_url',
    'logDriver = "awsfirelens"',
    'Name           = "datadog"',
    'Host           = "http-intake.logs.datadoghq.eu"',
    'dd_service     = each.value.service',
    'dd_source      = "python"',
    'dd_tags        = "env:dd-demo-${var.stack},service:${each.value.service},version:${each.value.version},project:dd-demo,stack:${var.stack}"',
    'secretOptions = [{ name = "apikey", valueFrom = local.secret_parameter_arns.DD_API_KEY }]',
    'firelensConfiguration = {',
    'config-file-value       = "/extra.conf"',
    '{ name = "DD_LOGS_INJECTION", value = "true" }',
]
for snippet in required_q8_logging:
    if snippet not in text:
        missing.append(f'Q8 Fargate log forwarding missing: {snippet}')

if 'apikey"' in text and 'secretOptions = [{ name = "apikey", valueFrom = local.secret_parameter_arns.DD_API_KEY }]' not in text:
    missing.append('Q8 Datadog API key must be supplied only through FireLens secretOptions')

for snippet in ('Name cloudwatch_logs', 'Match *', 'region ${AWS_REGION}', 'log_group_name ${LOG_GROUP_NAME}', 'auto_create_group false'):
    if snippet not in router_config:
        missing.append(f'Q8 CloudWatch duplication missing from log router: {snippet}')
for snippet in ('log_router_image()', 'build --platform linux/arm64', '"$OVERLAY/log-router"'):
    if snippet not in stack_script:
        missing.append(f'Q8 log-router build/push path missing: {snippet}')

required_offer_llmobs = (
    'name == "offer-worker" ? concat(var.enable_llmobs ? '
    '[{ name = "DD_LLMOBS_ENABLED", value = "1" }, '
    '{ name = "DD_LLMOBS_ML_APP", value = "urbanstreet-offers" }] : [],'
)
if required_offer_llmobs not in text:
    missing.append('offer-worker LLM Observability environment flags')

if not re.search(r'variable "enable_llmobs" \{.*?type\s*=\s*bool.*?default\s*=\s*true', text, re.DOTALL):
    missing.append('enable_llmobs boolean variable defaulting to true')
if 'var.enable_llmobs ? [{ name = "DD_LLMOBS_ENABLED"' not in text:
    missing.append('enable_llmobs gate on the offer-worker environment')
required_trace_tags = ('{ name = "DD_ENV", value = "dd-demo-${var.stack}" }',
                       '{ name = "DD_SERVICE", value = "" }',
                       '{ name = "DD_VERSION", value = "" }',
                       '{ name = "DD_TAGS", value = "project:dd-demo stack:${var.stack}" }')
if not all(tag in text for tag in required_trace_tags):
    missing.append('Datadog environment, service, version, and service tags')

# Hybrid Fargate metrics and the Fargate Agent check need the same service,
# version, and layer identity as the task's application/logging contract.
required_h02_telemetry = [
    'dockerLabels = {',
    '"com.datadoghq.tags.env"     = "dd-demo-${var.stack}"',
    '"com.datadoghq.tags.service" = each.value.service',
    '"com.datadoghq.tags.version" = each.value.version',
    '"com.datadoghq.tags.layer"   = each.value.layer',
    '"DD_TAGS", value = "project:dd-demo stack:${var.stack} service:${each.value.service} version:${each.value.version} layer:${each.value.layer}"',
]
for snippet in required_h02_telemetry:
    if snippet not in text:
        missing.append(f'H-02 Fargate telemetry identity missing: {snippet}')

for forbidden in [r'agent:latest', r'values = \["/api/\*"\]', r'aws_ssm_parameter', r'data\s+"aws_ssm_parameter"', r'output\s+".*secret']:
    if re.search(forbidden, text):
        missing.append(f"forbidden pattern matched: {forbidden}")
if missing:
    print("Terraform static guards failed:", *missing, sep="\n- ")
    sys.exit(1)
print("Terraform static guards passed")
