# Keep every slow lookup span searchable for the Chapter 3 "Slowest lookups" list. Without it only the default filters
# apply (errors plus a small diversity sample), and the trace stream widget lists a fraction of the slow lookups
# (https://docs.datadoghq.com/tracing/trace_pipeline/trace_retention/).
# https://registry.terraform.io/providers/DataDog/datadog/latest/docs/resources/apm_retention_filter
resource "datadog_apm_retention_filter" "slow_lookups" {
  name        = "dd-demo ${var.stack}: slow lookups (over 1 s)"
  rate        = "1.0"
  enabled     = true
  filter_type = "spans-sampling-processor"

  filter {
    query = "service:${var.service} env:${local.env} operation_name:flask.request @duration:>1s"
  }
}
