import re
"""Offline source contract checks; provider schema validation runs separately."""
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "terraform"


def test_account_collects_only_online_namespaces_and_keeps_ccm():
    text = (TF / "account/main.tf").read_text()
    for namespace in ("AWS/ECS", "AWS/ElastiCache", "AWS/ApplicationELB"):
        assert f'"{namespace}"' in text
    for action in ("cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "tag:GetResources",
                   "ecs:DescribeServices", "elasticache:ListTagsForResource", "elasticloadbalancing:DescribeTags"):
        assert f'"{action}"' in text
    assert 'resource "datadog_integration_aws_account_ccm_config" "main"' in text


def test_fargate_cost_meter_uses_task_role_and_runtime_inputs():
    ecs = (TF / "aws/ecs.tf").read_text()
    variables = (TF / "aws/variables.tf").read_text()
    vm_outputs = (TF / "vm/outputs.tf").read_text()
    stack = (TF.parent / "compose/scripts/stack.sh").read_text()

    assert 'actions   = ["ecs:ListTasks", "ecs:DescribeTasks"]' in ecs
    # Every task except demo-control (own role) keeps the shared role holding the ECS read permissions.
    assert 'task_role_arn            = each.key == "demo-control" ? aws_iam_role.demo_control_task.arn : aws_iam_role.task.arn' in ecs
    assert '{ name = "COST_ECS_CLUSTER", value = aws_ecs_cluster.main.name }' in ecs
    assert '{ name = "COST_ELASTICACHE_NODES", value = "1" }' in ecs
    assert '{ name = "COST_REMOTE_EC2_INSTANCE_TYPE", value = var.remote_ec2_instance_type }' in ecs
    assert '{ name = "COST_REMOTE_EBS_GB", value = tostring(var.remote_ebs_gb) }' in ecs
    assert '{ name = "COST_REMOTE_PUBLIC_IPV4_COUNT", value = "1" }' in ecs
    assert 'variable "remote_ec2_instance_type"' in variables
    assert 'variable "remote_ebs_gb"' in variables
    assert 'output "instance_type"' not in vm_outputs
    assert 'output "root_volume_gb"' not in vm_outputs
    assert 'remote_vm_cost_inputs()' in stack
    assert 'tf_out vm env_file' in stack
    assert 'COST_EC2_INSTANCE_TYPE' in stack
    assert 'COST_EBS_GB' in stack
    assert 'duplicate VM cost input' in stack
    assert 'malformed VM cost input' in stack
    assert 'vm_instance_type="$(tf_out vm instance_type)"' not in stack
    assert 'vm_root_ebs_gb="$(tf_out vm root_volume_gb)"' not in stack
    assert '"-var=remote_ec2_instance_type=$vm_instance_type"' in stack
    assert '"-var=remote_ebs_gb=$vm_root_ebs_gb"' in stack
    assert "COST_ECS_CLUSTER_NAME" not in ecs


def test_account_cost_dashboard_is_durable_and_uses_org_gross_billed():
    cost = (TF / "account/cost.tf").read_text()

    assert 'resource "datadog_dashboard_json" "cost"' in cost
    assert 'default = "*"' in cost
    assert 'max:dd_demo.cost.aggregate_org_billed_list_usd_total{${local.cost_scope}}' in cost
    assert 'max:dd_demo.cost.aggregate_org_billed_usd_total{${local.cost_scope}}' not in cost
    for query in (
        'max:dd_demo.cost.aggregate_usd_per_hour{${local.cost_scope},vendor:all}',
        'max:dd_demo.cost.aggregate_usd_total{${local.cost_scope},vendor:all}',
        'max:dd_demo.cost.aggregate_billed_usd_total{${local.cost_scope}}',
        'max:dd_demo.cost.aggregate_billed_list_usd_total{${local.cost_scope}}',
        'max:dd_demo.cost.aggregate_usd_per_hour{${local.cost_scope},!vendor:all} by {vendor}',
        'max:dd_demo.cost.aggregate_usd_total{${local.cost_scope},!vendor:all} by {vendor}',
        'max:dd_demo.cost.usd_total{${local.cost_scope}} by {vendor,item}',
    ):
        assert query in cost


def test_account_cost_dashboard_makes_pre_tax_estimates_and_ccm_actuals_unambiguous():
    cost = (TF / "account/cost.tf").read_text()

    for title in (
        "Burn now (estimate), all vendors (USD/h, pre-tax)",
        "Spent since start, all vendors (USD, pre-tax estimate)",
        "Confluent billed so far (USD, pre-tax actual bill after credits, about 1 h behind)",
        "Confluent trial credit used, all stacks (USD, gross billed last 30 days, pre-tax, about 1 h behind)",
        "Confluent billed at list price (USD, pre-tax actual before credits)",
        "Cost rate by vendor (USD/h, pre-tax estimate)",
        "Spent by vendor (USD, pre-tax estimate) and Confluent billed (pre-tax actual)",
        "Spent by item (USD, pre-tax estimate)",
        "Confluent bill by line type (USD, pre-tax actual)",
        "AWS cost per hour: live estimate vs CCM actual billed (USD/h, pre-tax)",
    ):
        assert f'title = "{title}"' in cost or f'title       = "{title}"' in cost

    assert 'alias = "Confluent billed (pre-tax actual)"' in cost
    assert 'alias = "AWS hourly estimate (pre-tax estimate)"' in cost
    assert 'alias = "AWS CCM hourly actual billed (pre-tax, delayed)"' in cost
    assert (
        'monotonic_diff(max:dd_demo.cost.aggregate_usd_total{project:dd-demo,${local.cost_scope},vendor:aws}.rollup(max, 3600))'
        in cost
    )
    assert 'max:dd_demo.cost.usd_total{project:dd-demo,${local.cost_scope},vendor:aws}' not in cost
    assert 'data_source = "cloud_cost"' in cost
    assert 'sum:aws.cost.unblended{project:dd-demo,${local.cost_scope},!aws_cost_type:Tax}.rollup(sum, 3600)' in cost
    assert "positive estimate increments in the selected dashboard period" in cost
    assert "CCM data lags 24–72 hours overall" in cost
    assert "AWS CUR data specifically takes 48–72 hours after a complete report" in cost
    assert "https://docs.datadoghq.com/cloud_cost_management/setup/aws/" in cost


def test_account_cost_dashboard_shows_month_to_date_spend_and_credit_left():
    cost = (TF / "account/cost.tf").read_text()

    assert "confluent_trial_credit_usd = 400" in cost
    assert 'confluent_credit_used_query = "max:dd_demo.cost.aggregate_org_billed_list_usd_total{project:dd-demo}"' in cost
    assert 'title       = "Confluent credit left (USD, ${local.confluent_trial_credit_usd} trial credit minus gross billed' in cost
    assert 'formulas        = [{ formula = "${local.confluent_trial_credit_usd} - used" }]' in cost
    # Month-to-date window: a documented widget live_span value of the Datadog provider.
    assert 'time = { live_span = "month_to_date" }' in cost
    assert 'query       = "sum:aws.cost.unblended{!aws_cost_type:Tax}.rollup(sum, 86400)"' in cost
    assert 'query       = "${local.confluent_credit_used_query}.rollup(max, 86400)"' in cost
    for formula in ('formula = "cumsum(aws)"', 'formula = "confluent"', 'formula = "cumsum(aws) + confluent"'):
        assert formula in cost


def test_account_adopts_existing_cost_meter_identity_without_new_role_grants():
    main = (TF / "account/main.tf").read_text()
    versions = (TF / "account/versions.tf").read_text()
    outputs = (TF / "account/outputs.tf").read_text()

    assert 'source  = "confluentinc/confluent"' in versions
    assert 'resource "confluent_service_account" "cost_meter"' in main
    assert 'display_name = "dd-demo-sa-cost-meter"' in main
    assert 'description  = "Read-only cost-meter"' in main
    for binding, role in (
        ("cost_meter_billing_admin", "BillingAdmin"),
        ("cost_meter_metrics_viewer", "MetricsViewer"),
    ):
        assert f'resource "confluent_role_binding" "{binding}"' in main
        assert f'role_name   = "{role}"' in main
        assert 'crn_pattern = data.confluent_organization.main.resource_name' in main

    assert 'resource "confluent_api_key" "cost_meter"' in main
    assert 'resource "datadog_integration_confluent_account" "cost_meter"' in main
    assert 'api_key    = confluent_api_key.cost_meter.id' in main
    assert 'api_secret = confluent_api_key.cost_meter.secret' in main
    for output in ("cost_meter_confluent_api_key", "cost_meter_confluent_api_secret"):
        assert f'output "{output}"' in outputs
    assert outputs.count('sensitive   = true') >= 2


def test_account_imports_are_opt_in_and_contain_no_presenter_ids():
    imports = (TF / "account/imports.tf").read_text()
    variables = (TF / "account/variables.tf").read_text()
    import_variables = (
        "cost_meter_service_account_import_id",
        "cost_meter_billing_admin_import_id",
        "cost_meter_metrics_viewer_import_id",
        "cost_meter_datadog_integration_import_id",
    )
    for name in import_variables:
        assert f'variable "{name}"' in variables
        assert 'default     = ""' in variables
        assert f'var.{name} == "" ? {{}}' in imports
    for pattern in (r'sa-[A-Za-z0-9]{6,}', r'rb-[A-Za-z0-9]{6,}', r'[0-9a-f]{32}'):
        assert not re.search(pattern, imports)


def test_fargate_surface_preserves_application_identity_and_stack_scope():
    path = TF / "datadog/fargate.tf"
    assert path.exists(), "Fargate dashboard and monitors are missing"
    text = path.read_text()
    for metric in ("ecs.fargate.cpu.usage", "ecs.fargate.mem.usage", "aws.elasticache.database_memory_usage_percentage",
                   "aws.applicationelb.un_healthy_host_count"):
        assert metric in text
    assert 'by {service,version}' in text
    assert 'project:dd-demo,stack:${var.stack}' in text
    assert 'notify_no_data' in text
    main = (TF / "datadog/main.tf").read_text()
    assert 'trace.flask.request{${local.svc},${local.lookup}} by {version}' in main
