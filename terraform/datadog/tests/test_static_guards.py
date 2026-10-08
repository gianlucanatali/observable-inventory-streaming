#!/usr/bin/env python3
"""Offline static contracts for the managed Datadog dashboard; no API calls or keys."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
text = "\n".join(path.read_text() for path in ROOT.glob("*.tf"))

required = [
    'resource "datadog_metric_tag_configuration" "stock_freshness_apply_delay"',
    'include_percentiles = true',
    'p95:stock.freshness.apply_delay{${local.dscope}}',
    'title   = "ElastiCache database memory usage (%)"',
    'max:aws.elasticache.database_memory_usage_percentage{${local.online_scope}} by {cacheclusterid}',
    'title   = "ElastiCache engine CPU (%)"',
    'max:aws.elasticache.engine_cpuutilization{${local.online_scope}} by {cacheclusterid}',
    'title   = "Fargate CPU usage by service/version (nanocores, includes sidecars)"',
    'sum:ecs.fargate.cpu.usage{${local.online_scope}} by {service,version}',
    'resource "datadog_dashboard_json" "overview"',
    'output "overview_dashboard_url"',
    # demo home: Chapter 3 saturation with metrics that exist, ECS events overlay, 100% marker,
    # the fixed "night of the incident" range
    'title       = "UrbanStreet demo home [${local.env}]"',
    'max:aws.ecs.service.cpuutilization.maximum{${local.online_scope}} by {servicename}',
    'markers = [{ value = "y = 100", display_type = "error dashed", label = "task CPU limit (100%)" }]',
    'max:aws.applicationelb.un_healthy_host_count{${local.online_scope}} by {targetgroup}',
    'ecs_events_query = "source:amazon_ecs project:dd-demo stack:${var.stack}"',
    'events  = [{ q = local.demo_panel_query }, { q = local.ecs_events_query }]',
    'home_incident_from_ms = 1791310200000',
    'home_incident_to_ms   = 1791311400000',
    'night        = "?tpl_var_env=${local.env}&from_ts=${local.home_incident_from_ms}&to_ts=${local.home_incident_to_ms}&live=false"',
    'operation_name%3Acatalogue.prepare',
    '[local.overview_note.start]',
    # demo home Chapter 3: span breakdown (catalogue.prepare vs the whole lookup, by version), the slow-lookup traces list
    # (list_stream over trace_stream) and the p95 objective marker
    'operation_name:catalogue.prepare"',
    'data_source = "spans"',
    'data_source  = "trace_stream"',
    'type        = "list_stream"',
    '@duration:>1000000000',
    'label = "p95 objective (${var.p95_threshold_seconds} s)"',
    'local.overview_spans.breakdown,',
    'local.overview_spans.slow_lookups,',
    # demo home Chapter 5: Offer delay (live from the offer-worker) and Chapter 3 event list beside the overlay chart
    'resource "datadog_metric_tag_configuration" "offer_delay"',
    'for_each            = toset(["offer.delay", "offer.delay.upstream", "offer.delay.worker"])',
    'p95:offer.delay{${local.dscope}}',
    'p95:offer.delay.upstream{${local.dscope}}',
    'p95:offer.delay.worker{${local.dscope}}',
    'title = "Offer delay: sell-out to offer card (s), p95: total, upstream (CDC + Flink), worker (AI + publish)"',
    'avg:offer.delay{${local.dscope}} by {product_id}',
    'aggregator  = "last"',
    'title = "Offer delay by product, last value (s)"',
    'local.overview_offer_delay.chart,',
    'local.overview_offer_delay.last_by_product,',
    'data_source  = "event_stream"',
    'query_string = "(${local.ecs_events_query}) OR (${local.demo_panel_query})"',
    'local.overview_events.list,',
    'Flink share is about upstream minus the CDC time',
    'Markers: ECS events and control-panel actions, listed in the event list beside the p95 chart',
    # demo home: "Your stack" links come from var.shop_url, never a hard-coded host
    'variable "shop_url"',
    '"**Your stack:** ${local.home_shop_md} · ${local.home_panel_md} · ${local.home_product_md}',
    '[Control panel](${local.shop_base}/control/)',
    '[Product page P0042](${local.shop_base}/#/product/P0042)',
    'Online shop (run `./demo links`)',
    'max:dd_demo.cost.aggregate_usd_per_hour{stack:${var.stack},!vendor:all} by {vendor}',
    # demo-control event tags (contracts section 12); a free-text title phrase matched nothing live
    'demo_config_query = "project:dd-demo stack:${var.stack} demo_event:config"',
    'demo_panel_query  = "project:dd-demo stack:${var.stack} (demo_event:config OR demo_event:action)"',
]
missing = [snippet for snippet in required if snippet not in text]

for removed in ('redis.mem.used', 'redis.clients.blocked'):
    if removed in text:
        missing.append(f'H-08 dead VM Redis metric must not remain: {removed}')

metric_configuration = re.search(
    r'resource\s+"datadog_metric_tag_configuration"\s+"stock_freshness_apply_delay"\s*\{(?P<body>.*?)\n\}',
    text,
    re.DOTALL,
)
overview = (ROOT / "overview.tf").read_text()
# ecs.fargate.cpu.percent is computed against the container limit, not the 0.5 vCPU task: it reads about 51 % at
# saturation (2026-10-06 all-traffic 1.1.0 run). Never use it, nor an avg: rollup, for saturation on the home.
for misleading in ('ecs.fargate.cpu.percent', 'avg:aws.ecs.cpuutilization'):
    if misleading in overview:
        missing.append(f'demo home must not chart {misleading} (misleading for saturation)')
# Note links carry literal per-stack values: Note widgets interpolate template variables only in the URL path.
home_links = re.search(r'home_link = \{(?P<body>.*?)\n  \}', overview, re.DOTALL)
if home_links is None:
    missing.append('demo home link table (local.home_link)')
elif '$env' in home_links.group('body').replace('${', ''):
    missing.append('demo home links must use local.env, not the $env template variable')
for chapter in ('1. One honest number', '2. Unknown is not zero', '3. The incident', '4. Canary the fix',
                '5. The AI offer', '6. Datadog on top'):
    if f'title = "{chapter}"' not in overview:
        missing.append(f'demo home group missing: {chapter}')
# Stable group ids for ?tile_focus deep links: six distinct explicit ids, applied to the group widgets.
group_ids = re.findall(r'^\s+id\s+=\s+(7\d{15})\s*$', overview, re.MULTILINE)
if sorted(group_ids) != [f'700000000000000{i}' for i in range(1, 7)]:
    missing.append(f'demo home needs six unique group ids 7000000000000001..6, found {group_ids}')
if 'id = g.id' not in overview:
    missing.append('demo home group widgets must set id = g.id')

if metric_configuration is None:
    missing.append('managed stock.freshness.apply_delay metric tag configuration')
else:
    body = metric_configuration.group('body')
    for pattern, label in [
        (r'metric_name\s*=\s*"stock\.freshness\.apply_delay"', 'stock.freshness.apply_delay metric name'),
        (r'metric_type\s*=\s*"distribution"', 'distribution metric type'),
        (r'tags\s*=\s*\["env",\s*"service",\s*"version",\s*"is_probe"\]', 'all submitted metric tags'),
        (r'include_percentiles\s*=\s*true', 'enabled percentile aggregation'),
    ]:
        if re.search(pattern, body) is None:
            missing.append(label)

ftable = re.search(r'feed_state_table = \{.*?\n  \}\n', text, re.DOTALL)
if ftable is None or 'type  = "query_table"' not in ftable.group(0):
    missing.append('feed_state_table query_table widget')
else:
    for v, pal in [("1", "white_on_green"), ("0", "white_on_red"), ("-1", "white_on_gray")]:
        if f'comparator = "=", value = {v}, palette = "{pal}"' not in ftable.group(0):
            missing.append(f'feed_state_table conditional format {v} -> {pal}')
    for needle in ('aggregator  = "last"', 'by {store}', 'stock.feed.state'):
        if needle not in ftable.group(0):
            missing.append(f'feed_state_table: {needle}')
if '"feed_state_table", "feed_not_live"' not in text:
    missing.append('stock dashboard Freshness group must list feed_state_table before feed_not_live')
if not re.search(r'local\.overview_note\.c2,\s*local\.feed_state_table,\s*local\.feed_not_live', text):
    missing.append('demo home Chapter 2 must start with feed_state_table, then feed_not_live')

for needle in ('clamp_min(min:stock.feed.state{${local.dscope}} by {store}, 0)', 'formula = "1 - q1"', 'display_type    = "bars"'):
    if needle not in text:
        missing.append(f'feed_not_live: {needle}')
if 'local.ts.feed_state' in text:
    missing.append('overlapping per-store feed_state lines must be gone')

if missing:
    print("Datadog Terraform static guards failed:", *missing, sep="\n- ")
    sys.exit(1)
for chapter_note in ("c1", "c2", "c3", "c4", "c5", "c6"):
    m = re.search(rf'{chapter_note} = join.*?\n      \]\)', overview, re.DOTALL)
    if not m or "${local.home_panel_md}" not in m.group(0):
        sys.exit(f"overview.tf: note {chapter_note} must link the control panel (local.home_panel_md)")
if re.search(r'(?m)^[^#\n]*http://[^$\n"]*(elb\.amazonaws|amazonaws\.com)', overview):
    sys.exit("overview.tf: hard-coded AWS host; the shop links must come from var.shop_url")
print("Datadog Terraform static guards passed")
