# Dashboard and monitors for the dd-demo (plan section 6, contracts section 7).
# Several metric names in README.md still need confirmation against a live account.

locals {
  env = "dd-demo-${var.stack}"
  # Monitors use the literal env. The dashboard uses its template variable $env (prefix env, default = the stack's env).
  scope  = "env:${local.env}"
  svc    = "service:${var.service},${local.scope}"
  dscope = "$env"
  dsvc   = "service:${var.service},$env"
  # Live check (2026-10-06): the integration tags the cluster as resource_id:<lkc-...>; kafka_id is N/A.
  confluent_scope = var.confluent_cluster_id != null ? "resource_id:${var.confluent_cluster_id}" : "*"

  base_tags = ["project:dd-demo", "stack:${var.stack}", local.scope]
  notify    = length(var.notification_handles) == 0 ? "" : "\n${join(" ", var.notification_handles)}"

  # Helper-built widgets keep the JSON readable. Each series: [name, query].
  ts = {
    for k, v in {
      latency = {
        title   = "inventory-api p95 latency by version (s)"
        queries = [["q1", "p95:trace.flask.request{${local.dsvc}} by {version}"]]
        formula = "q1"
      }
      error_rate = {
        title = "inventory-api error rate by version"
        queries = [
          ["q1", "sum:trace.flask.request.errors{${local.dsvc}} by {version}.as_count()"],
          ["q2", "sum:trace.flask.request.hits{${local.dsvc}} by {version}.as_count()"],
        ]
        formula = "q1 / q2"
      }
      hits = {
        title   = "inventory-api requests by version"
        queries = [["q1", "sum:trace.flask.request.hits{${local.dsvc}} by {version}.as_count()"]]
        formula = "q1"
      }
      apply_delay = {
        title   = "stock.freshness.apply_delay p95 (s)"
        queries = [["q1", "p95:stock.freshness.apply_delay{${local.dscope}}"]]
        formula = "q1"
      }
      probe_age = {
        title   = "stock.probe.age per store (s)"
        queries = [["q1", "max:stock.probe.age{${local.dscope}} by {store}"]]
        formula = "q1"
      }
      sellable_age = {
        title   = "stock.sellable.age (s): Flink + Redis sink path behind the newest probe write"
        queries = [["q1", "max:stock.sellable.age{${local.dscope}}"]]
        formula = "q1"
      }
      display_delay = {
        title   = "stock.display.delay p95 (s)"
        queries = [["q1", "p95:stock.display.delay{${local.dscope}}"]]
        formula = "q1"
      }
      records = {
        title   = "stock.projector.records by outcome"
        queries = [["q1", "sum:stock.projector.records{${local.dscope}} by {outcome}.as_count()"]]
        formula = "q1"
      }
      errors = {
        title   = "stock.projector.errors by reason"
        queries = [["q1", "sum:stock.projector.errors{${local.dscope}} by {reason}.as_count()"]]
        formula = "q1"
      }
      connect = {
        title   = "stock.connect.task_running by connector (5 Debezium + sellable-redis)"
        queries = [["q1", "min:stock.connect.task_running{${local.dscope}} by {connector}"]]
        formula = "q1"
      }
      lag = {
        title   = "Confluent Cloud consumer lag (offsets) by group"
        queries = [["q1", "max:confluent_cloud.kafka.consumer_lag_offsets{${local.confluent_scope}} by {consumer_group_id}"]]
        formula = "q1"
      }
      elasticache_memory = {
        title   = "ElastiCache database memory usage (%)"
        queries = [["q1", "max:aws.elasticache.database_memory_usage_percentage{${local.online_scope}} by {cacheclusterid}"]]
        formula = "q1"
      }
      elasticache_cpu = {
        title   = "ElastiCache engine CPU (%)"
        queries = [["q1", "max:aws.elasticache.engine_cpuutilization{${local.online_scope}} by {cacheclusterid}"]]
        formula = "q1"
      }
      cpu = {
        title   = "container.cpu.user by container"
        queries = [["q1", "avg:container.cpu.user{*} by {container_name}"]]
        formula = "q1"
      }
      host_cpu = {
        title   = "system.cpu.user by host"
        queries = [["q1", "avg:system.cpu.user{*} by {host}"]]
        formula = "q1"
      }
      fargate_cpu = {
        title   = "Fargate CPU usage by service/version (nanocores, includes sidecars)"
        queries = [["q1", "sum:ecs.fargate.cpu.usage{${local.online_scope}} by {service,version}"]]
        formula = "q1"
      }
      offer_decision = {
        title   = "offer.decision by route and reason"
        queries = [["q1", "sum:offer.decision{${local.dscope}} by {route,reason}.as_count()"]]
        formula = "q1"
      }
      offer_text = {
        title   = "offer.text by route"
        queries = [["q1", "sum:offer.text{${local.dscope}} by {route}.as_count()"]]
        formula = "q1"
      }
      offer_completed = {
        title   = "offer.completed by offer_type"
        queries = [["q1", "sum:offer.completed{${local.dscope}} by {offer_type}.as_count()"]]
        formula = "q1"
      }
      restock_open = {
        title   = "restock.orders.open (open purchase orders)"
        queries = [["q1", "max:restock.orders.open{${local.dscope}}"]]
        formula = "q1"
      }
      restock_delivered = {
        title   = "restock.orders.delivered by store"
        queries = [["q1", "sum:restock.orders.delivered{${local.dscope}} by {store}.as_count()"]]
        formula = "q1"
      }
      restock_lead_time = {
        title   = "restock.lead_time (supplier lead time, s)"
        queries = [["q1", "avg:restock.lead_time{${local.dscope}}"]]
        formula = "q1"
      }
      } : k => {
      definition = {
        type  = "timeseries"
        title = v.title
        requests = [{
          display_type    = "line"
          response_format = "timeseries"
          formulas        = [{ formula = v.formula }]
          queries = [
            for q in v.queries : {
              data_source = "metrics"
              name        = q[0]
              query       = q[1]
            }
          ]
        }]
      }
    }
  }

  # Demo panel events (contracts section 12): demo-control sends "demo config: <key> = <value>" on every saved
  # parameter (tag demo_event:config) and "demo action: <name> started|succeeded|failed" for panel actions
  # (tag demo_event:action), both tagged project:dd-demo stack:<stack>. Tags only: a free-text title phrase
  # matched nothing in the live account (2026-10-06), and project/stack alone would also match monitor alerts.
  # The stack is a literal here (the dashboard template variable is only env).
  demo_config_query = "project:dd-demo stack:${var.stack} demo_event:config"
  demo_panel_query  = "project:dd-demo stack:${var.stack} (demo_event:config OR demo_event:action)"

  # One row per store, latest feed state, coloured: the timeseries above overlaps when all stores sit at 1.
  # Query table widget and conditional formats (comparator, value, palette):
  # https://docs.datadoghq.com/dashboards/widgets/table/ and the dashboard widget API schema (TableWidgetDefinition).
  # The doc mentions text formatting (value-to-text alias) for tables, but it is a UI setting with no field confirmed
  # in the API schema, so the numbers 1 / 0 / -1 stay as they are and only the colour carries the meaning.
  feed_state_table = {
    definition = {
      type  = "query_table"
      title = "Feed state per store now (green 1 ok, red 0 stale, grey -1 unknown)"
      requests = [{
        response_format = "scalar"
        queries = [{
          data_source = "metrics"
          name        = "q1"
          query       = "min:stock.feed.state{${local.dscope}} by {store}"
          aggregator  = "last"
        }]
        formulas = [{
          formula = "q1"
          alias   = "feed state"
          conditional_formats = [
            { comparator = "=", value = 1, palette = "white_on_green" },
            { comparator = "=", value = 0, palette = "white_on_red" },
            { comparator = "=", value = -1, palette = "white_on_gray" },
          ]
        }]
        sort = {
          count    = 50
          order_by = [{ type = "group", name = "store", order = "asc" }]
        }
      }]
    }
  }

  # History next to the table: "store not live" (1 when the feed state is 0 stale or -1 unknown, else 0), stacked red
  # bars per store, so it is flat 0 when all is fine and the paused store shows as a bar of its own colour.
  # clamp_min(<metric query>, 0) maps -1 to 0 (https://docs.datadoghq.com/dashboards/functions/exclusion/, applies to
  # metric queries); the formula 1 - q1 then gives 0 for state 1 and 1 for states 0 and -1. Stacked bars:
  # display_type bars (https://docs.datadoghq.com/dashboards/widgets/timeseries/).
  feed_not_live = {
    definition = {
      type  = "timeseries"
      title = "Stores not live over time (1 = feed stale or unknown, stacked by store)"
      requests = [{
        display_type    = "bars"
        response_format = "timeseries"
        formulas        = [{ formula = "1 - q1" }]
        queries = [{
          data_source = "metrics"
          name        = "q1"
          query       = "clamp_min(min:stock.feed.state{${local.dscope}} by {store}, 0)"
        }]
        style = { palette = "warm" }
      }]
      yaxis = { min = "0", include_zero = true }
    }
  }

  widgets = merge(local.ts, {
    feed_state_table = local.feed_state_table
    feed_not_live    = local.feed_not_live
    # same chart as ts.restock_open with the config changes overlaid as event markers
    restock_open = {
      definition = merge(local.ts.restock_open.definition, {
        events = [{ q = local.demo_config_query }]
      })
    }
    demo_config_events = {
      definition = {
        type           = "event_stream"
        title          = "Demo panel: config changes and actions"
        query          = local.demo_panel_query
        event_size     = "l"
        tags_execution = "and"
      }
    }
  })

  groups = concat(
    [
      { title = "Service objective", widgets = ["latency", "error_rate", "hits"] },
      { title = "Freshness", widgets = ["apply_delay", "probe_age", "feed_state_table", "feed_not_live", "sellable_age", "display_delay"] },
      { title = "Pipeline", widgets = concat(["records", "errors", "connect"], var.enable_dd_streams ? ["lag"] : []) },
      { title = "ElastiCache and VM host", widgets = ["elasticache_memory", "elasticache_cpu", "cpu", "host_cpu", "fargate_cpu"] },
    ],
    var.enable_restock ? [{ title = "Restock", widgets = ["restock_open", "restock_delivered", "restock_lead_time", "demo_config_events"] }] : [],
    var.enable_offers ? [{ title = "Offers", widgets = ["offer_decision", "offer_text", "offer_completed"] }] : [],
  )
}

resource "datadog_metric_tag_configuration" "stock_freshness_apply_delay" {
  metric_name         = "stock.freshness.apply_delay"
  metric_type         = "distribution"
  tags                = ["env", "service", "version", "is_probe"]
  include_percentiles = true
}

# stock.display.delay is emitted as a distribution by the storefront beacon; the p95 widget needs percentiles enabled
# (otherwise the widget shows "Percentiles Misconfiguration"). Tags are those the dashboard scope uses.
resource "datadog_metric_tag_configuration" "stock_display_delay" {
  metric_name         = "stock.display.delay"
  metric_type         = "distribution"
  tags                = ["env", "service", "version"]
  include_percentiles = true
}

resource "datadog_dashboard_json" "stock" {
  # Guard: the state in use must be the one of this stack (terraform workspace new <stack>).
  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "terraform.workspace is '${terraform.workspace}' but var.stack is '${var.stack}'. Select the stack's own workspace: terraform workspace select ${var.stack} (create it once with terraform workspace new ${var.stack}). This stops one stack's state being applied to another."
    }
  }

  dashboard = jsonencode({
    title       = "UrbanStreet stock service [${local.env}]"
    description = "Stock lookup objective, feed freshness, pipeline health, Redis/host, restock and offers. project:dd-demo stack:${var.stack}. Managed by overlay/terraform/datadog."
    layout_type = "ordered"
    template_variables = [{
      name             = "env"
      prefix           = "env"
      default          = local.env
      available_values = []
    }]
    widgets = [
      for g in local.groups : {
        definition = {
          type        = "group"
          layout_type = "ordered"
          title       = g.title
          widgets     = [for w in g.widgets : local.widgets[w]]
        }
      }
    ]
  })
}

# ---------------------------------------------------------------------------------------------
# Monitors. All tagged project:dd-demo, stack:<stack>, env:dd-demo-<stack>, layer:<layer>. "Missing data is not healthy": the freshness-probe gauges alert on
# no-data, not only on bad values.
# ---------------------------------------------------------------------------------------------
resource "datadog_monitor" "p95_latency" {
  count   = var.enable_releases ? 1 : 0
  name    = "[${local.env}] inventory-api p95 latency above ${var.p95_threshold_seconds}s on {{version.name}}"
  type    = "query alert"
  message = "inventory-api p95 latency is above the objective for version {{version.name}}.${local.notify}"
  query   = "percentile(last_5m):p95:trace.flask.request{${local.svc}} by {version} > ${var.p95_threshold_seconds}"

  monitor_thresholds {
    critical = var.p95_threshold_seconds
  }

  notify_no_data      = false
  include_tags        = true
  require_full_window = false
  tags                = concat(local.base_tags, ["service:${var.service}", "layer:releases"])
}

resource "datadog_monitor" "probe_age" {
  name    = "[${local.env}] stock.probe.age above 15 s on store {{store.name}}"
  type    = "query alert"
  message = "The latest probe write of store {{store.name}} is not yet visible in Redis after 15 s: its stock data is stale.${local.notify}"
  query   = "avg(last_1m):avg:stock.probe.age{${local.scope}} by {store} > 15"

  monitor_thresholds {
    critical = 15
  }

  notify_no_data      = false
  require_full_window = false
  tags                = concat(local.base_tags, ["layer:core"])
}

resource "datadog_monitor" "probe_no_data" {
  name    = "[${local.env}] stock.probe.age: no data for 2 minutes"
  type    = "query alert"
  message = "No stock.probe.age data. Missing data is not healthy: the freshness probe or the Agent may be down.${local.notify}"
  # The threshold can never be crossed by a real value (age is >= 0): this monitor fires on no-data only.
  query = "avg(last_2m):avg:stock.probe.age{${local.scope}} < 0"

  monitor_thresholds {
    critical = 0
  }

  notify_no_data      = true
  no_data_timeframe   = 2
  require_full_window = true
  tags                = concat(local.base_tags, ["layer:core"])
}

resource "datadog_monitor" "feed_state" {
  name    = "[${local.env}] stock.feed.state below 1 on store {{store.name}}"
  type    = "query alert"
  message = "The stock feed of store {{store.name}} is not ok (0 stale, -1 unknown): sellable stock may be understated.${local.notify}"
  query   = "min(last_1m):min:stock.feed.state{${local.scope}} by {store} < 1"

  monitor_thresholds {
    critical = 1
  }

  notify_no_data      = false
  require_full_window = false
  tags                = concat(local.base_tags, ["layer:core"])
}

resource "datadog_monitor" "connect_task" {
  name    = "[${local.env}] Debezium connector task not running: {{connector.name}}"
  type    = "query alert"
  message = "stock.connect.task_running is below 1 for {{connector.name}}: the CDC connector task is not RUNNING.${local.notify}"
  query   = "min(last_2m):min:stock.connect.task_running{${local.scope} AND connector:inventory-*} by {connector} < 1"

  monitor_thresholds {
    critical = 1
  }

  notify_no_data      = false
  require_full_window = false
  tags                = concat(local.base_tags, ["layer:core"])
}

resource "datadog_monitor" "sellable_age" {
  name    = "[${local.env}] stock.sellable.age above ${var.sellable_age_threshold_seconds} s"
  type    = "query alert"
  message = "The aggregate path (Flink job and Redis sink) is behind: the newest probe write has not reached sellable:__probe__ after ${var.sellable_age_threshold_seconds} s. Sellable stock shown in the shop is stale.${local.notify}"
  query   = "avg(last_1m):avg:stock.sellable.age{${local.scope}} > ${var.sellable_age_threshold_seconds}"

  monitor_thresholds {
    critical = var.sellable_age_threshold_seconds
  }

  notify_no_data      = false
  require_full_window = false
  tags                = concat(local.base_tags, ["layer:core"])
}

resource "datadog_monitor" "sink_task" {
  name    = "[${local.env}] Redis sink connector sellable-redis task not running"
  type    = "query alert"
  message = "stock.connect.task_running{connector:sellable-redis} is below 1: sellable stock no longer reaches Redis.${local.notify}"
  query   = "min(last_2m):min:stock.connect.task_running{${local.scope} AND connector:sellable-redis} < 1"

  monitor_thresholds {
    critical = 1
  }

  notify_no_data      = true
  no_data_timeframe   = 3
  require_full_window = false
  tags                = concat(local.base_tags, ["layer:core"])
}

resource "datadog_monitor" "host_no_data" {
  name    = "[${local.env}] Agent on the demo VM not reporting"
  type    = "service check"
  message = "The Datadog Agent check datadog.agent.up is missing or critical on {{host.name}}.${local.notify}"
  query   = "\"datadog.agent.up\".over(\"${local.scope}\").by(\"host\").last(2).count_by_status()"

  monitor_thresholds {
    ok       = 1
    warning  = 1
    critical = 1
  }

  notify_no_data    = true
  no_data_timeframe = 2
  tags              = concat(local.base_tags, ["layer:core"])
}

# ---------------------------------------------------------------------------------------------
# Layer restock monitors (contracts section 10)
# ---------------------------------------------------------------------------------------------
resource "datadog_monitor" "restock_open_growth" {
  count   = var.enable_restock ? 1 : 0
  name    = "[${local.env}] restock.orders.open growing: purchase orders are piling up"
  type    = "query alert"
  message = "Open purchase orders grew by more than ${var.restock_open_growth_threshold} in 15 minutes: stores sell out faster than the supplier delivers (lead time) or the supplier simulator is stuck.${local.notify}"
  query   = "change(avg(last_15m),last_15m):avg:restock.orders.open{${local.scope}} > ${var.restock_open_growth_threshold}"

  monitor_thresholds {
    critical = var.restock_open_growth_threshold
  }

  notify_no_data      = false
  require_full_window = false
  tags                = concat(local.base_tags, ["layer:restock"])
}

resource "datadog_monitor" "supplier_sim_no_data" {
  count   = var.enable_restock ? 1 : 0
  name    = "[${local.env}] supplier simulator: no restock.lead_time data for 5 minutes"
  type    = "query alert"
  message = "No restock.lead_time data. Missing data is not healthy: the supplier simulator is down, purchase orders will never be delivered.${local.notify}"
  # A lead time is never negative: this monitor fires on no-data only.
  query = "avg(last_5m):avg:restock.lead_time{${local.scope}} < 0"

  monitor_thresholds {
    critical = 0
  }

  notify_no_data      = true
  no_data_timeframe   = 5
  require_full_window = true
  tags                = concat(local.base_tags, ["layer:restock"])
}
