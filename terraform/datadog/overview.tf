# Demo home (formerly the overview dashboard):
# one dashboard per stack that the presenter opens first and a workshop reader uses for their own stack. A "Start
# here" note, then one group per Chapter: a story line, links to the deeper views, and 2-3 key widgets.
# Queries are reused from the stock dashboard (main.tf local.ts), the online dashboard (fargate.tf), the account cost
# dashboard (overlay/terraform/account/cost.tf) or the monitors in this directory; the Chapter 3 saturation queries were
# checked with data for the all-traffic 1.1.0 run of 2026-10-08 10:10-10:30 UTC.
# Widgets of a Layer that is off stay on the dashboard and show no data: they query tags, or reference monitors that
# always exist, so the apply never depends on a Layer.
#
# Links: the Note widget promises template variable interpolation only in the URL path
# (https://docs.datadoghq.com/dashboards/widgets/note/), and every deep link needs the env in the query string. The
# dashboard is created per stack, so links carry literal per-stack values (local.env, var.stack). They are relative
# paths, so they work on any Datadog site.

locals {
  # The night of the incident: a recorded all-traffic 1.1.0 run of this stack (var.incident_window, from_ts/to_ts with
  # live=false: https://docs.datadoghq.com/dashboards/guide/custom_time_frames/). Null: the notes say how to record one.
  home_night_md = var.incident_window == null ? "The night of the incident: no window recorded for this stack yet (route all traffic to 1.1.0 under load for a few minutes, roll back, then set `incident_window`)" : "[The night of the incident](?tpl_var_env=${local.env}&from_ts=${var.incident_window.from_ms}&to_ts=${var.incident_window.to_ms}&live=false) (${var.incident_window.label})"

  # Amazon ECS service events of this stack (task started, unhealthy, stopped, steady state). The integration tags them
  # source:amazon_ecs plus the AWS resource tags project/stack (Events Explorer, 2026-10-07).
  ecs_events_query = "source:amazon_ecs project:dd-demo stack:${var.stack}"

  home_link = {
    # Same dashboard, fixed range, this stack's env. A query-only relative link keeps the current dashboard path.
    stock        = "${datadog_dashboard_json.stock.url}?tpl_var_env=${local.env}"
    online       = var.enable_fargate ? one(datadog_dashboard_json.online[*].url) : null
    feed_monitor = "/monitors/${datadog_monitor.feed_state.id}"
    monitors     = "/monitors/manage?q=tag%3A%22stack%3A${var.stack}%22"
    dsm          = "/data-streams/map?env=${local.env}"
    apm          = "/apm/entity/service%3A${var.service}?env=${local.env}&operationName=flask.request&spanKind=server"
    trace_110    = "/apm/traces?query=service%3A${var.service}%20env%3A${local.env}%20version%3A1.1.0%20operation_name%3Acatalogue.prepare"
    ecs_events   = "/event/explorer?query=source%3Aamazon_ecs%20project%3Add-demo%20stack%3A${var.stack}"
    llmobs       = "/llm/traces?query=%40ml_app%3Aurbanstreet-offers%20%40event_type%3Aspan%20%40is_root_span%3Atrue"
    offer_worker = "/apm/entity/service%3Aoffer-worker?env=${local.env}"
    synthetics   = "/synthetics/tests?query=tag%3A%22stack%3A${var.stack}%22"
    cost         = "/dashboard/lists?q=dd-demo%20cost"
  }
  # The stack's own shop and control panel (the ALB, or the VM in the non-hybrid mode), passed by stack.sh as shop_url.
  # Empty (local or other callers): the note still renders, without a broken link.
  shop_base       = trimsuffix(var.shop_url, "/")
  home_shop_md    = local.shop_base != "" ? "[Online shop](${local.shop_base}/)" : "Online shop (run `./demo links`)"
  home_panel_md   = local.shop_base != "" ? "[Control panel](${local.shop_base}/control/)" : "Control panel (run `./demo links`)"
  home_product_md = local.shop_base != "" ? "[Product page P0042](${local.shop_base}/#/product/P0042)" : "Product page P0042 (run `./demo links`)"

  home_online_md = local.home_link.online != null ? "[AWS online dashboard](${local.home_link.online})" : "AWS online dashboard (needs `enable_fargate`)"

  # Timeseries helper: each series is [name, query]; events are overlay queries in Event Explorer syntax; markers are
  # horizontal lines.
  overview_chart = {
    for k, v in {
      p95 = {
        title   = "inventory-api lookup p95 by version (s), panel actions and ECS events as markers"
        queries = [["q1", "p95:trace.flask.request{${local.dsvc},${local.lookup}} by {version}"]]
        events  = [{ q = local.demo_panel_query }, { q = local.ecs_events_query }]
        markers = [{ value = "y = ${var.p95_threshold_seconds}", display_type = "error dashed", label = "p95 objective (${var.p95_threshold_seconds} s)" }]
      }
      ecs_cpu_max = {
        title   = "ECS service CPU, max (%): 100 = the task's whole vCPU"
        queries = [["q1", "max:aws.ecs.service.cpuutilization.maximum{${local.online_scope}} by {servicename}"]]
        events  = [{ q = local.ecs_events_query }]
        markers = [{ value = "y = 100", display_type = "error dashed", label = "task CPU limit (100%)" }]
      }
      alb_unhealthy = {
        title   = "ALB unhealthy targets by target group (1 = the release's only task failed its health check)"
        queries = [["q1", "max:aws.applicationelb.un_healthy_host_count{${local.online_scope}} by {targetgroup}"]]
        events  = [{ q = local.ecs_events_query }]
        markers = []
      }
      alb_wait = {
        title   = "Slowest response the load balancer waited for, by release target group (s)"
        queries = [["q1", "max:aws.applicationelb.target_response_time.maximum{${local.online_scope}} by {targetgroup}"]]
        events  = [{ q = local.demo_panel_query }]
        markers = [{ value = "y = ${var.p95_threshold_seconds}", display_type = "error dashed", label = "p95 objective (${var.p95_threshold_seconds} s)" }]
      }
      traffic_share = {
        title   = "Share of lookups per version (%), panel actions as markers"
        queries = [["q1", "sum:trace.flask.request.hits{${local.dsvc},${local.lookup}} by {version}.as_count()"], ["q2", "sum:trace.flask.request.hits{${local.dsvc},${local.lookup}}.as_count()"]]
        formula = "100 * q1 / q2"
        events  = [{ q = local.demo_panel_query }]
        markers = []
      }
      hits = {
        title   = "inventory-api lookups by version, panel actions as markers"
        queries = [["q1", "sum:trace.flask.request.hits{${local.dsvc},${local.lookup}} by {version}.as_count()"]]
        events  = [{ q = local.demo_panel_query }]
        markers = []
      }
      cost_by_vendor = {
        title   = "Cost per hour by vendor (USD/h, pre-tax estimate)"
        queries = [["q1", "max:dd_demo.cost.aggregate_usd_per_hour{stack:${var.stack},!vendor:all} by {vendor}"]]
        events  = []
        markers = []
      }
      } : k => {
      definition = merge({
        type  = "timeseries"
        title = v.title
        requests = [{
          display_type    = "line"
          response_format = "timeseries"
          formulas        = [{ formula = lookup(v, "formula", "q1") }]
          queries         = [for q in v.queries : { data_source = "metrics", name = q[0], query = q[1] }]
        }]
        },
        length(v.events) > 0 ? { events = v.events } : {},
        length(v.markers) > 0 ? { markers = v.markers } : {},
      )
    }
  }

  # Chapter 3 evidence that led to the code fix. Both read APM spans, so they show what the account indexed
  # (https://docs.datadoghq.com/tracing/trace_pipeline/trace_retention/). The trace metrics of the p95 chart above are
  # kept longer. The Spans list source supports a search query and no sort option
  # (https://docs.datadoghq.com/dashboards/widgets/list/), so it lists only lookups slower than one second.
  overview_spans = {
    breakdown = {
      definition = {
        type  = "timeseries"
        title = "Where a lookup spends its time, by version (s): the catalogue.prepare span against the whole lookup"
        requests = [{
          display_type    = "bars"
          response_format = "timeseries"
          formulas = [
            { formula = "q1 / 1000000000", alias = "catalogue.prepare" },
            { formula = "q2 / 1000000000", alias = "whole lookup" },
          ]
          queries = [
            {
              data_source = "spans"
              name        = "q1"
              compute     = { aggregation = "avg", metric = "@duration" }
              search      = { query = "service:${var.service} $env operation_name:catalogue.prepare" }
              group_by    = [{ facet = "version", limit = 10, sort = { aggregation = "count", order = "desc" } }]
              indexes     = []
            },
            {
              data_source = "spans"
              name        = "q2"
              compute     = { aggregation = "avg", metric = "@duration" }
              search      = { query = "service:${var.service} $env operation_name:flask.request ${local.lookup_span}" }
              group_by    = [{ facet = "version", limit = 10, sort = { aggregation = "count", order = "desc" } }]
              indexes     = []
            },
          ]
        }]
      }
    }
    slow_lookups = {
      definition = {
        type = "list_stream"
        # The Spans list stream has no sort option: "These data sources support a search query but do not provide a sort
        # option" (https://docs.datadoghq.com/dashboards/widgets/list/). The API schema has a generic query.sort, but the docs do
        # not say it applies to trace_stream, so the title says what is shown: the newest matching spans.
        title = "Lookups over 1 s in the window, newest first (50 shown): version, duration, resource; click a row to open the trace"
        requests = [{
          response_format = "event_list"
          columns = [
            { field = "timestamp", width = "auto" },
            { field = "@duration", width = "auto" },
            { field = "version", width = "auto" },
            { field = "resource_name", width = "full" },
          ]
          query = {
            data_source  = "trace_stream"
            query_string = "service:${var.service} $env operation_name:flask.request ${local.lookup_span} @duration:>1000000000"
            indexes      = []
          }
        }]
      }
    }
  }

  # Chapter 3: the events the p95 chart overlays (Amazon ECS events and control-panel actions), listed with time and
  # text. One OR query made of the chart's two overlay queries. list_stream with the event_stream data source:
  # https://docs.datadoghq.com/dashboards/widgets/list/
  overview_events = {
    list = {
      definition = {
        type  = "list_stream"
        title = "Events behind the markers: Amazon ECS events and control-panel actions"
        requests = [{
          response_format = "event_list"
          columns = [
            { field = "timestamp", width = "auto" },
            { field = "title", width = "full" },
          ]
          query = {
            data_source  = "event_stream"
            query_string = "(${local.ecs_events_query}) OR (${local.demo_panel_query})"
            event_size   = "s"
            indexes      = []
          }
        }]
      }
    }
  }

  # Chapter 5: the Offer delay emitted live by the offer-worker (distributions; read with max, which needs no
  # metric tag configuration and exists only once the worker has emitted). One value per sell-out, so max is the delay.
  # Parts are maxima of their own, so they do not add up exactly.
  overview_offer_delay = {
    chart = {
      definition = {
        type  = "timeseries"
        title = "Offer delay: sell-out to offer card (s), max: total, upstream (CDC + Flink), worker (AI + publish)"
        requests = [{
          display_type    = "line"
          response_format = "timeseries"
          formulas = [
            { formula = "q1", alias = "total" },
            { formula = "q2", alias = "upstream (CDC + Flink)" },
            { formula = "q3", alias = "worker (AI + publish)" },
          ]
          queries = [
            { data_source = "metrics", name = "q1", query = "max:offer.delay{${local.dscope}}" },
            { data_source = "metrics", name = "q2", query = "max:offer.delay.upstream{${local.dscope}}" },
            { data_source = "metrics", name = "q3", query = "max:offer.delay.worker{${local.dscope}}" },
          ]
        }]
      }
    }
    last_by_product = {
      definition = {
        type  = "toplist"
        title = "Offer delay by product, last value (s)"
        requests = [{
          response_format = "scalar"
          formulas        = [{ formula = "q1", limit = { count = 10, order = "desc" } }]
          queries = [{
            data_source = "metrics"
            name        = "q1"
            query       = "max:offer.delay{${local.dscope}} by {product_id}"
            aggregator  = "last"
          }]
        }]
      }
    }
  }

  overview_note = {
    for k, v in {
      start = join("\n", [
        "## Start here: the demo home of ${local.env}",
        "One group per Chapter, the same six as the talk and the workshop. Each group says what you are seeing, links to the deeper views (already filtered to `${local.env}`), and shows 2-3 key charts. Time range: top right; the template variable `env` reads **$env.value**.",
        "",
        "**Your stack:** ${local.home_shop_md} · ${local.home_panel_md} · ${local.home_product_md} · [Stock dashboard](${local.home_link.stock}) · [APM inventory-api](${local.home_link.apm})",
        "",
        "1. **One honest number**: how old is the stock a shopper sees, per store.",
        "2. **Unknown is not zero**: a quiet store is named, never counted as zero.",
        "3. **The incident**: release 1.1.0 is slow; the canary gate stops it. ${local.home_night_md}: what 100% would have done.",
        "4. **Canary the fix**: 1.2.0 takes traffic step by step, next to 1.0.0.",
        "5. **The AI offer**: the AI picks the alternative only when it is sure, and its \"no good substitute\" always stands; if it is unsure or does not answer, the safe rule decides.",
        "6. **Datadog on top**: tests from outside, Confluent lag, monitors, cost.",
        "",
        "Links open in this tab; use the browser's Back to return. Charts of a Layer that is off stay empty.",
      ])
      c1 = join("\n", [
        "**Chapter 1: one honest number.** What you are seeing: the probe age per store, the time from a probe write at the store database to the moment Redis shows it. Flat and low means the online number follows the stores within seconds.",
        "",
        "Deeper: [stock dashboard, Freshness group](${local.home_link.stock}) · [Data Streams Monitoring map](${local.home_link.dsm}) · ${local.home_panel_md}",
      ])
      c2 = join("\n", [
        "**Chapter 2: unknown is not zero.** What you are seeing: `stock.feed.state` per store (1 ok, 0 stale, -1 unknown) as a status table, the stores not live over time and the store-feed monitor. When a store goes quiet its row turns red, a bar of its own colour appears and the monitor names it; the shop says \"at least\" instead of a wrong number.",
        "",
        "Deeper: [store-feed monitor](${local.home_link.feed_monitor}) · [stock dashboard](${local.home_link.stock}) · [all monitors of this stack](${local.home_link.monitors}) · ${local.home_panel_md}",
      ])
      c3 = join("\n", [
        "**Chapter 3: the incident.** Live view: p95 per release, the CPU of each ECS service against its whole vCPU, and unhealthy targets; markers are ECS events and control-panel actions, listed beside the p95 chart. ${local.home_night_md}: all traffic on 1.1.0, CPU flat at 100%, p95 far above the objective, the target unhealthy; then the rollback and 1.2.0 through its canary. Two span views show where the time goes: `catalogue.prepare` against the whole lookup by version (only 1.1.0 prepares the catalogue on every request) and the newest lookups over 1 s, each opening its trace. At 10% the gate stops 1.1.0 while it is only slow.",
        "",
        "Deeper: [APM inventory-api, compare versions (Deployments)](${local.home_link.apm}) · [1.1.0 spans of `catalogue.prepare`](${local.home_link.trace_110}) · [Amazon ECS events](${local.home_link.ecs_events}) · ${local.home_online_md} · ${local.home_panel_md}",
      ])
      c4 = join("\n", [
        "**Chapter 4: canary the fix.** What you are seeing: requests per release as traffic moves to 1.2.0, and the error rate per release. Read it with the p95 chart above: 1.2.0 sits next to 1.0.0, far below 1.1.0, and its CPU stays low.",
        "",
        "Deeper: [APM inventory-api, Deployments](${local.home_link.apm}) · [stock dashboard, Service objective](${local.home_link.stock}) · ${local.home_panel_md}",
      ])
      c5 = join("\n", [
        "**Chapter 5: the AI offer.** What you are seeing: every offer decision by route (AI or the safe rule) and reason, and the offers completed. The Offer delay chart shows how long a shopper waits from the sell-out to the offer card (the slowest sell-out in each interval), split into upstream (CDC plus Flink, until the worker receives the at-risk event) and worker (decision, AI, publish); the Flink share is about upstream minus the CDC time (about 0.5 to 0.9 s). The list shows the last delay per product. Empty without the offers layer.",
        "",
        "Deeper: [LLM Observability, `urbanstreet-offers` root spans](${local.home_link.llmobs}) (the application is shared by all stacks) · [APM offer-worker](${local.home_link.offer_worker}) · ${local.home_panel_md}",
      ])
      c6 = join("\n", [
        "**Chapter 6: Datadog on top.** What you are seeing: Synthetics tests that call the shop from outside (layer `dd-synthetics`), Confluent Cloud consumer lag (layer `dd-streams`) and what the stack costs per hour (cost meter).",
        "",
        "Deeper: [Synthetics tests](${local.home_link.synthetics}) · [monitors](${local.home_link.monitors}) · ${local.home_online_md} · [cost dashboard, all stacks](${local.home_link.cost}) · [stock dashboard](${local.home_link.stock}) · ${local.home_panel_md}",
      ])
      } : k => {
      definition = {
        type             = "note"
        content          = v
        background_color = k == "start" ? "purple" : "white"
        font_size        = "14"
        text_align       = "left"
        show_tick        = false
        tick_edge        = "left"
        tick_pos         = "50%"
      }
    }
  }

  overview_groups = [
    {
      id    = 7000000000000001
      title = "1. One honest number"
      widgets = [
        local.overview_note.c1,
        local.ts.probe_age,
        local.ts.apply_delay,
      ]
    },
    {
      id    = 7000000000000002
      title = "2. Unknown is not zero"
      widgets = [
        local.overview_note.c2,
        local.feed_state_table,
        local.feed_not_live,
        {
          definition = {
            type       = "alert_graph"
            title      = "Store-feed monitor: stock.feed.state below 1"
            alert_id   = tostring(datadog_monitor.feed_state.id)
            viz_type   = "timeseries"
            title_size = "16"
          }
        },
      ]
    },
    {
      id    = 7000000000000003
      title = "3. The incident"
      widgets = [
        local.overview_note.c3,
        local.overview_chart.p95,
        local.overview_events.list,
        local.overview_spans.breakdown,
        local.overview_spans.slow_lookups,
        local.overview_chart.ecs_cpu_max,
        local.overview_chart.alb_wait,
        local.overview_chart.alb_unhealthy,
      ]
    },
    {
      id    = 7000000000000004
      title = "4. Canary the fix"
      widgets = [
        local.overview_note.c4,
        local.overview_chart.traffic_share,
        local.overview_chart.hits,
        local.ts.error_rate,
      ]
    },
    {
      id    = 7000000000000005
      title = "5. The AI offer"
      widgets = [
        local.overview_note.c5,
        local.ts.offer_decision,
        local.ts.offer_completed,
        local.overview_offer_delay.chart,
        local.overview_offer_delay.last_by_product,
      ]
    },
    {
      id    = 7000000000000006
      title = "6. Datadog on top"
      widgets = [
        local.overview_note.c6,
        {
          definition = {
            type                = "manage_status"
            title               = "Synthetics tests (layer dd-synthetics)"
            display_format      = "countsAndList"
            color_preference    = "text"
            hide_zero_counts    = false
            show_last_triggered = false
            summary_type        = "monitors"
            sort                = "status,asc"
            query               = "tag:\"layer:dd-synthetics\" tag:\"stack:${var.stack}\""
          }
        },
        local.ts.lag,
        local.overview_chart.cost_by_vendor,
      ]
    },
  ]
}

# Resource address kept from the overview dashboard, so the change is an in-place update (no destroy/create) and the
# output overview_dashboard_url, the Links card tile overview-dashboard and the guide's stack links keep their URL.
resource "datadog_dashboard_json" "overview" {
  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "terraform.workspace is '${terraform.workspace}' but var.stack is '${var.stack}'. Select the stack's own workspace: terraform workspace select ${var.stack}."
    }
  }

  dashboard = jsonencode({
    title       = "UrbanStreet demo home [${local.env}]"
    description = "Start here: the six Chapters, each with its story line, links to the deeper views and its key charts. project:dd-demo stack:${var.stack}. Managed by overlay/terraform/datadog."
    layout_type = "ordered"
    template_variables = [{
      name             = "env"
      prefix           = "env"
      default          = local.env
      available_values = []
    }]
    widgets = concat(
      [local.overview_note.start],
      [
        # Explicit widget ids on the six Chapter groups, so a deep link ?tile_focus=<id> (an undocumented Datadog
        # dashboard URL parameter that scrolls to and highlights a group) keeps working after a rebuild. Not verified:
        # whether Datadog keeps client-supplied widget ids. The provider docs example for datadog_dashboard_json sets
        # explicit widget ids: https://github.com/DataDog/terraform-provider-datadog/blob/master/docs/resources/dashboard_json.md
        # Check the live id after the first apply before publishing a link.
        for g in local.overview_groups : {
          id = g.id
          definition = {
            type        = "group"
            layout_type = "ordered"
            title       = g.title
            widgets     = g.widgets
          }
        }
      ],
    )
  })
}
