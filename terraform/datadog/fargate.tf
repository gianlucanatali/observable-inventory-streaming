# Metric names from official Datadog integrations (2026-10-04):
# https://docs.datadoghq.com/integrations/aws-fargate/
# https://docs.datadoghq.com/integrations/amazon-elasticache/
# https://docs.datadoghq.com/integrations/amazon-elb/
# Runtime tag propagation and monitor queries still require a live smoke check.
variable "enable_fargate" {
  description = "Enable the AWS-native online dashboard and monitors; false preserves VM-only stacks."
  type        = bool
  default     = false
}

locals {
  # AWS resources carry project/stack tags, not necessarily the APM env tag.
  online_scope = "project:dd-demo,stack:${var.stack}"
  online_charts = {
    cpu = {
      title = "Fargate CPU usage by service/version (nanocores, includes sidecars)"
      query = "sum:ecs.fargate.cpu.usage{${local.online_scope}} by {service,version}"
    }
    memory = {
      title = "Fargate memory usage by service/version (bytes, includes sidecars)"
      query = "sum:ecs.fargate.mem.usage{${local.online_scope}} by {service,version}"
    }
    latency = {
      title = "inventory-api p95 by version (same identity on Fargate)"
      query = "p95:trace.flask.request{${local.svc}} by {version}"
    }
    ecs_cpu = {
      title = "ECS service CPU utilization (%)"
      query = "avg:aws.ecs.cpuutilization{${local.online_scope}} by {servicename}"
    }
    cache_memory = {
      title = "ElastiCache database memory usage (%)"
      query = "max:aws.elasticache.database_memory_usage_percentage{${local.online_scope}} by {cacheclusterid}"
    }
    cache_cpu = {
      title = "ElastiCache engine CPU (%)"
      query = "max:aws.elasticache.engine_cpuutilization{${local.online_scope}} by {cacheclusterid}"
    }
    alb_unhealthy = {
      title = "ALB unhealthy targets (max across AZs; not summed)"
      query = "max:aws.applicationelb.un_healthy_host_count{${local.online_scope}} by {targetgroup}"
    }
    alb_lcus = {
      title = "ALB consumed LCUs (not included in cost base fee)"
      query = "max:aws.applicationelb.consumed_lcus{${local.online_scope}}"
    }
  }
  online_alerts = {
    cache_memory = {
      name      = "ElastiCache memory above 85%"
      query     = "avg(last_10m):max:aws.elasticache.database_memory_usage_percentage{${local.online_scope}} by {cacheclusterid} > 85"
      threshold = 85
      message   = "Serving cache memory is above 85%; check evictions and capacity before trusting availability."
    }
    alb_unhealthy = {
      name      = "ALB unhealthy targets"
      query     = "min(last_5m):max:aws.applicationelb.un_healthy_host_count{${local.online_scope}} by {targetgroup} > 0"
      threshold = 0
      message   = "An ALB target remains unhealthy; check the task health and target-group port/path."
    }
    cost_error = {
      name      = "ECS cost sampling failed"
      query     = "max(last_5m):max:dd_demo.cost.meter_error{${local.online_scope},source:ecs} > 0"
      threshold = 0
      message   = "Online cost data is missing or a sample failed; do not present the aggregate as a complete bill."
    }
  }
}

resource "datadog_dashboard_json" "online" {
  count = var.enable_fargate ? 1 : 0
  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "Select the stack's own Terraform workspace before applying."
    }
  }
  dashboard = jsonencode({
    title       = "UrbanStreet AWS-native online [${local.env}]"
    description = "Fargate sidecars and AWS integration metrics; scoped to project/stack. APM service/version unchanged."
    layout_type = "ordered"
    widgets = [for chart in local.online_charts : {
      definition = {
        type  = "timeseries"
        title = chart.title
        requests = [{
          display_type    = "line"
          response_format = "timeseries"
          queries         = [{ data_source = "metrics", name = "q1", query = chart.query }]
          formulas        = [{ formula = "q1" }]
        }]
      }
    }]
  })
}

resource "datadog_monitor" "online" {
  for_each = var.enable_fargate ? local.online_alerts : {}
  name     = "[${local.env}] ${each.value.name}"
  type     = "query alert"
  query    = each.value.query
  message  = "${each.value.message}${local.notify}"
  monitor_thresholds {
    critical = each.value.threshold
  }
  evaluation_delay    = 300
  require_full_window = false
  notify_no_data      = true
  no_data_timeframe   = 20
  tags                = concat(local.base_tags, ["layer:core", "platform:fargate"])
}

# A service check does not depend on traffic reaching a particular canary version.
resource "datadog_monitor" "fargate_agent" {
  count   = var.enable_fargate ? 1 : 0
  name    = "[${local.env}] Fargate Agent check by service/version"
  type    = "service check"
  query   = "\"fargate_check\".over(\"project:dd-demo\",\"stack:${var.stack}\").by(\"service\",\"version\").last(2).count_by_status()"
  message = "A Fargate sidecar cannot read task metadata or stopped reporting; verify the affected service/version.${local.notify}"
  monitor_thresholds {
    ok       = 1
    warning  = 1
    critical = 1
  }
  notify_no_data    = true
  no_data_timeframe = 5
  tags              = concat(local.base_tags, ["layer:core", "platform:fargate"])
}
