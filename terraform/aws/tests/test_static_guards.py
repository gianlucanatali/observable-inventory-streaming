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
    'output "alb_listener_arn"', 'output "ecs_services"',
    'variable "image_tags"', 'local.image_uri[each.value.image]', 'on_prem_private_ip',
    'PROJECTOR_KAFKA_API_KEY', 'DEMO_CONTROL_KAFKA_API_KEY', 'COST_METER_API_KEY',
    'KAFKA_BOOTSTRAP', 'SR_URL', 'KAFKA_SECURITY_PROTOCOL', 'STORE_HOSTS',
    'CONFLUENT_ENVIRONMENT_ID', 'KAFKA_CLUSTER_ID', 'FLINK_COMPUTE_POOL_ID',
    'aws_lb_target_group.demo_control',
]
missing = [snippet for snippet in required if snippet not in text]
for app in ['stock-projector', 'inventory-api-100', 'inventory-api-110', 'inventory-api-120', 'storefront', 'offer-worker', 'demo-control', 'cost-meter']:
    if app not in text:
        missing.append(app)

# Canary-first incident: the three inventory-api releases share one task size,
# so p95 by version compares code rather than CPU headroom.
release_sizes = {
    app: re.search(rf'{app}\s*=\s*\{{\s*cpu\s*=\s*(\d+),\s*memory\s*=\s*(\d+)\s*\}}', text)
    for app in ('inventory-api-100', 'inventory-api-110', 'inventory-api-120')
}
sizes = {app: m.groups() if m else None for app, m in release_sizes.items()}
if set(sizes.values()) != {('512', '1024')}:
    missing.append(f'inventory-api releases must all default to cpu=512/memory=1024; found {sizes}')

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
    'local.image_uri["log-router"]',
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

# Only demo-control may change ALB routing, only on this stack's inventory rule, and only demo-control
# reaches Connect REST (8083) on the VM, through a security-group reference (never a CIDR).
routing_policy = re.search(r'data "aws_iam_policy_document" "demo_control_routing" \{(?P<body>.*?)\n\}', text, re.DOTALL)
if routing_policy is None:
    missing.append('demo_control_routing policy document')
else:
    body = routing_policy.group('body')
    if 'actions   = ["elasticloadbalancing:ModifyRule"]\n    resources = [aws_lb_listener_rule.inventory.arn]' not in body:
        missing.append('ModifyRule must be scoped to aws_lb_listener_rule.inventory.arn')
    if 'actions   = ["elasticloadbalancing:DescribeRules"]' not in body:
        missing.append('DescribeRules read-back statement')
    granted = set(re.findall(r'"(elasticloadbalancing:[A-Za-z*]+)"', body))
    if granted != {'elasticloadbalancing:ModifyRule', 'elasticloadbalancing:DescribeRules'}:
        missing.append(f'demo-control ELB actions must be exactly ModifyRule and DescribeRules, found {sorted(granted)}')
if text.count('elasticloadbalancing:') != 2:
    missing.append('elasticloadbalancing permissions must appear only in demo_control_routing')
if 'task_role_arn            = each.key == "demo-control" ? aws_iam_role.demo_control_task.arn : aws_iam_role.task.arn' not in text:
    missing.append('demo-control must use its dedicated task role')
if 'role   = aws_iam_role.demo_control_task.id\n  policy = data.aws_iam_policy_document.demo_control_routing.json' not in text:
    missing.append('routing policy must be attached to the demo-control task role only')
connect_rule = re.search(r'resource "aws_vpc_security_group_ingress_rule" "on_prem_connect_rest" \{(?P<body>.*?)\n\}', text, re.DOTALL)
if connect_rule is None or 'referenced_security_group_id = aws_security_group.demo_control.id' not in connect_rule.group('body') \
        or 'cidr' in connect_rule.group('body'):
    missing.append('Connect REST 8083 must be admitted only from the demo-control security group')
if len(re.findall(r'(?:from_port|to_port)\s*=\s*8083\b', text)) != 2 or connect_rule is None \
        or len(re.findall(r'(?:from_port|to_port)\s*=\s*8083\b', connect_rule.group('body'))) != 2:
    missing.append('port 8083 must appear only in on_prem_connect_rest')
if '[aws_security_group.tasks.id, aws_security_group.demo_control.id]' not in text:
    missing.append('demo-control service must carry the demo_control security group')

# The scenario API (8090) on the VM is admitted only from the demo-control security group, and only
# demo-control receives its URL and bearer token (SSM SecureString, synced by stack.sh from .env.secrets).
api_rule = re.search(r'resource "aws_vpc_security_group_ingress_rule" "on_prem_scenario_api" \{(?P<body>.*?)\n\}', text, re.DOTALL)
if api_rule is None or 'referenced_security_group_id = aws_security_group.demo_control.id' not in api_rule.group('body') \
        or 'cidr' in api_rule.group('body') or 'security_group_id            = var.on_prem_security_group_id' not in api_rule.group('body'):
    missing.append('scenario API 8090 must be admitted on the VM group only from the demo-control security group')
if len(re.findall(r'(?:from_port|to_port)\s*=\s*8090\b', text)) != 2 or api_rule is None \
        or len(re.findall(r'(?:from_port|to_port)\s*=\s*8090\b', api_rule.group('body'))) != 2:
    missing.append('port 8090 must appear only in on_prem_scenario_api')
if text.count('SCENARIO_API_URL') != 1 or '{ name = "SCENARIO_API_URL", value = "http://${var.on_prem_private_ip}:8090" }' not in text:
    missing.append('SCENARIO_API_URL must be set once, for demo-control, to the VM private IP')
demo_secrets = re.search(r'demo-control\s+= \[(?P<keys>[^\]]*)\]', text)
if demo_secrets is None or '"SCENARIO_API_TOKEN"' not in demo_secrets.group('keys'):
    missing.append('SCENARIO_API_TOKEN must be a demo-control task secret')
app_secrets = re.search(r'app_secret_keys = \{(?P<body>.*?)\n  \}', text, re.DOTALL)
if app_secrets is None or app_secrets.group('body').count('SCENARIO_API_TOKEN') != 1:
    missing.append('SCENARIO_API_TOKEN must be granted to demo-control only')
if 'SCENARIO_API_TOKEN|' not in stack_script:
    missing.append('stack.sh ssm_secret_key must allow SCENARIO_API_TOKEN')

for forbidden in [r'agent:latest', r'values = \["/api/\*"\]', r'aws_ssm_parameter', r'data\s+"aws_ssm_parameter"', r'output\s+".*secret']:
    if re.search(forbidden, text):
        missing.append(f"forbidden pattern matched: {forbidden}")
# Every ALB target group drains quickly: requests are short lookups and panel calls, and the 300 s AWS default made
# each ECS rolling deploy wait about ten minutes for old tasks to deregister.
target_groups = list(re.finditer(r'resource "aws_lb_target_group" "(?P<name>[^"]+)" \{(?P<body>.*?)\n\}', text, re.DOTALL))
if {tg.group('name') for tg in target_groups} != {'storefront', 'inventory', 'demo_control'}:
    missing.append(f"expected target groups storefront, inventory, demo_control; found {[tg.group('name') for tg in target_groups]}")
for tg in target_groups:
    if not re.search(r'deregistration_delay\s*=\s*local\.alb_deregistration_delay_s\b', tg.group('body')):
        missing.append(f"aws_lb_target_group.{tg.group('name')} must set deregistration_delay = local.alb_deregistration_delay_s")
delay = re.search(r'alb_deregistration_delay_s\s*=\s*(\d+)', text)
if delay is None or not 5 <= int(delay.group(1)) <= 30:
    missing.append("local.alb_deregistration_delay_s must be set between 5 and 30 seconds")

if missing:
    print("Terraform static guards failed:", *missing, sep="\n- ")
    sys.exit(1)
print("Terraform static guards passed")
