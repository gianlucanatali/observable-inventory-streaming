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
    # Datadog stores a normalised query: AND between terms, hyphens in values backslash-escaped, @duration in nanoseconds.
    # Writing any other form makes the provider fail with "inconsistent result after apply" (new value differs from plan).
    # So the query is written exactly as the API returns it. "\\-" in HCL is a backslash plus a hyphen.
    query = "service:${replace(var.service, "-", "\\-")} AND env:${replace(local.env, "-", "\\-")} AND operation_name:flask.request AND @duration:>1000000000"
  }
}
