# Durable account-wide cost dashboard. This root is intentionally excluded from
# stack-down, so cost history remains available after individual demo stacks are removed.
# Every cost-meter emits a stack tag. The dashboard's stack template variable defaults to * so
# it shows all deployed stacks; selecting a stack narrows estimated and billed stack metrics.
# `org_billed_*` comes from the Confluent Costs API's organisation-wide poll (all environments).

locals {
  cost_scope = "$stack"

  cost_value = {
    rate_now = {
      title = "Burn now (estimate), all vendors (USD/h, pre-tax)"
      query = "max:dd_demo.cost.aggregate_usd_per_hour{${local.cost_scope},vendor:all}"
    }
    spent = {
      title = "Spent since start, all vendors (USD, pre-tax estimate)"
      query = "max:dd_demo.cost.aggregate_usd_total{${local.cost_scope},vendor:all}"
    }
    billed = {
      title = "Confluent billed so far (USD, pre-tax actual bill after credits, about 1 h behind)"
      query = "max:dd_demo.cost.aggregate_billed_usd_total{${local.cost_scope}}"
    }
    org_billed = {
      title = "Confluent trial credit used, all stacks (USD, gross billed last 30 days, pre-tax, about 1 h behind)"
      query = "max:dd_demo.cost.aggregate_org_billed_list_usd_total{${local.cost_scope}}"
    }
    billed_list = {
      title = "Confluent billed at list price (USD, pre-tax actual before credits)"
      query = "max:dd_demo.cost.aggregate_billed_list_usd_total{${local.cost_scope}}"
    }
  }

  # Trial credit of the Confluent Cloud organisation (USD), not read from any API: the Costs API has no promo balance.
  confluent_trial_credit_usd = 400
  # Org-wide, so not narrowed by the stack variable; emitted by every running cost-meter.
  confluent_credit_used_query = "max:dd_demo.cost.aggregate_org_billed_list_usd_total{project:dd-demo}"

  cost_widgets = concat(
    [for k, v in local.cost_value : {
      definition = {
        type        = "query_value"
        title       = v.title
        precision   = 2
        autoscale   = false
        custom_unit = "$"
        requests = [{
          response_format = "scalar"
          queries         = [{ data_source = "metrics", name = "q1", query = v.query, aggregator = "last" }]
          formulas        = [{ formula = "q1" }]
        }]
      }
    }],
    [
      {
        definition = {
          type        = "query_value"
          title       = "Confluent credit left (USD, ${local.confluent_trial_credit_usd} trial credit minus gross billed, about 1 h behind)"
          precision   = 2
          autoscale   = false
          custom_unit = "$"
          requests = [{
            response_format = "scalar"
            queries         = [{ data_source = "metrics", name = "used", query = local.confluent_credit_used_query, aggregator = "last" }]
            formulas        = [{ formula = "${local.confluent_trial_credit_usd} - used" }]
          }]
        }
      },
      {
        definition = {
          type  = "timeseries"
          title = "Spent this month, cumulative (USD, pre-tax actual bills: AWS whole account via CCM, 24-72 h behind; Confluent trial credit used, about 1 h behind)"
          # Fixed to the calendar month whatever the dashboard time picker says; cumsum restarts at the window start.
          time = { live_span = "month_to_date" }
          requests = [{
            display_type    = "line"
            response_format = "timeseries"
            queries = [
              {
                data_source = "cloud_cost"
                name        = "aws"
                query       = "sum:aws.cost.unblended{!aws_cost_type:Tax}.rollup(sum, 86400)"
              },
              {
                data_source = "metrics"
                name        = "confluent"
                query       = "${local.confluent_credit_used_query}.rollup(max, 86400)"
              },
            ]
            formulas = [
              { formula = "cumsum(aws)", alias = "AWS billed, month to date (CCM actual, 24-72 h behind)" },
              { formula = "confluent", alias = "Confluent trial credit used (gross billed, already cumulative)" },
              { formula = "cumsum(aws) + confluent", alias = "Total (AWS + Confluent)" },
            ]
          }]
        }
      },
      {
        definition = {
          type  = "timeseries"
          title = "Cost rate by vendor (USD/h, pre-tax estimate)"
          requests = [{
            display_type    = "area"
            response_format = "timeseries"
            queries         = [{ data_source = "metrics", name = "q1", query = "max:dd_demo.cost.aggregate_usd_per_hour{${local.cost_scope},!vendor:all} by {vendor}" }]
            formulas        = [{ formula = "q1" }]
          }]
        }
      },
      {
        definition = {
          type  = "timeseries"
          title = "Spent by vendor (USD, pre-tax estimate) and Confluent billed (pre-tax actual)"
          requests = [
            {
              display_type    = "line"
              response_format = "timeseries"
              queries         = [{ data_source = "metrics", name = "q1", query = "max:dd_demo.cost.aggregate_usd_total{${local.cost_scope},!vendor:all} by {vendor}" }]
              formulas        = [{ formula = "q1" }]
            },
            {
              display_type    = "line"
              response_format = "timeseries"
              queries         = [{ data_source = "metrics", name = "q2", query = "max:dd_demo.cost.aggregate_billed_usd_total{${local.cost_scope}}" }]
              formulas        = [{ formula = "q2", alias = "Confluent billed (pre-tax actual)" }]
            },
          ]
        }
      },
      {
        definition = {
          type  = "toplist"
          title = "Spent by item (USD, pre-tax estimate)"
          requests = [{
            response_format = "scalar"
            queries         = [{ data_source = "metrics", name = "q1", query = "max:dd_demo.cost.usd_total{${local.cost_scope}} by {vendor,item}", aggregator = "last" }]
            formulas        = [{ formula = "q1", limit = { count = 20, order = "desc" } }]
          }]
        }
      },
      {
        definition = {
          type  = "toplist"
          title = "Confluent bill by line type (USD, pre-tax actual)"
          requests = [{
            response_format = "scalar"
            queries         = [{ data_source = "metrics", name = "q1", query = "max:dd_demo.cost.billed_usd_total{${local.cost_scope}} by {line_type}", aggregator = "last" }]
            formulas        = [{ formula = "q1", limit = { count = 20, order = "desc" } }]
          }]
        }
      },
      {
        definition = {
          type  = "timeseries"
          title = "AWS cost per hour: live estimate vs CCM actual billed (USD/h, pre-tax)"
          requests = [{
            display_type    = "line"
            response_format = "timeseries"
            queries = [
              {
                data_source = "metrics"
                name        = "estimate"
                query       = "monotonic_diff(max:dd_demo.cost.aggregate_usd_total{project:dd-demo,${local.cost_scope},vendor:aws}.rollup(max, 3600))"
              },
              {
                data_source = "cloud_cost"
                name        = "actual"
                query       = "sum:aws.cost.unblended{project:dd-demo,${local.cost_scope},!aws_cost_type:Tax}.rollup(sum, 3600)"
              },
            ]
            formulas = [
              { formula = "estimate", alias = "AWS hourly estimate (pre-tax estimate)" },
              { formula = "actual", alias = "AWS CCM hourly actual billed (pre-tax, delayed)" },
            ]
          }]
        }
      },
      {
        definition = {
          type             = "note"
          background_color = "gray"
          font_size        = "14"
          content          = <<-EOT
            **How these pre-tax numbers are made.** *Estimate* = live usage x list price, updated every 15 s: Confluent Flink and traffic from the Metrics API (about 1 min behind), Confluent eCKU from its bill (the eCKU metric is not the billing unit), AWS from host uptime (EC2, EBS, public IPv4) and an upper bound for CPU credits. Optional online lines use observed running Fargate task allocations including Agent sidecars, configured ALB count and Redis cache.t4g.small node count. Those **sampled_estimate totals start at cost-meter process start and reset on restart**, not stack creation; they exclude image-pull time, tasks missed between polls, ALB LCUs, additional public IPv4, transfer, ECR and extended support. ALB/Redis partial-hour rounding is not modeled. Do not present the aggregate as a complete AWS bill. The AWS comparison applies an hourly `max` rollup before `monotonic_diff`, so it shows positive estimate increments in the selected dashboard period instead of a lifetime meter value. *Actual billed* = Confluent's own bill from its Costs API, about 1 h behind, credits applied. The AWS hourly actual uses Datadog CCM `aws.cost.unblended` with hourly `sum` rollup, excluding `aws_cost_type:Tax`; CCM data lags 24–72 hours overall, and AWS CUR data specifically takes 48–72 hours after a complete report. https://docs.datadoghq.com/cloud_cost_management/setup/aws/ Datadog subscription charges are not included; check the current plan or trial before relying on a zero-cost assumption. Prices and primary-source URLs: cost-meter/prices.json.
          EOT
        }
      },
    ],
  )
}

resource "datadog_dashboard_json" "cost" {
  dashboard = jsonencode({
    title       = "dd-demo cost [all stacks]"
    description = "Durable account-wide pre-tax estimates and actual billed costs per vendor and item, including a selected-stack AWS estimate versus CCM actual-billed comparison. Managed by terraform/account."
    layout_type = "ordered"
    # Datadog returns "notify_list": null on every read; the provider only normalises id/url/author/time
    # fields, so declaring it keeps the stored JSON equal to this config (no perpetual in-place update).
    notify_list = null
    template_variables = [{
      name    = "stack"
      prefix  = "stack"
      default = "*"
    }]
    widgets = local.cost_widgets
  })
}
