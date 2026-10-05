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

if missing:
    print("Datadog Terraform static guards failed:", *missing, sep="\n- ")
    sys.exit(1)
print("Datadog Terraform static guards passed")