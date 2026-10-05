# Layer dd-streams: Datadog Confluent Cloud integration. The account holds the
# Confluent Cloud API key (service account with MetricsViewer, created by overlay/terraform/cloud);
# the resource enrols the stack's Kafka cluster. Provider arguments checked with
# `terraform providers schema` for the pinned provider; the behaviour (metrics appearing, tags
# applied to metrics); confirm the metrics and tags after the first apply.

resource "datadog_integration_confluent_account" "main" {
  count = var.enable_dd_streams ? 1 : 0

  api_key    = var.confluent_api_key
  api_secret = var.confluent_api_secret
  tags       = ["project:dd-demo", "stack:${var.stack}", "layer:dd-streams"]

  lifecycle {
    precondition {
      condition     = var.confluent_api_key != null && var.confluent_api_secret != null
      error_message = "enable_dd_streams is true but confluent_api_key / confluent_api_secret are not set. Export TF_VAR_confluent_api_key and TF_VAR_confluent_api_secret from the cloud dir outputs datadog_confluent_api_key / datadog_confluent_api_secret (see variables.tf)."
    }
  }
}

resource "datadog_integration_confluent_resource" "cluster" {
  count = var.enable_dd_streams ? 1 : 0

  account_id    = datadog_integration_confluent_account.main[0].id
  resource_id   = var.confluent_cluster_id
  resource_type = "kafka"
  tags          = ["project:dd-demo", "stack:${var.stack}", "layer:dd-streams"]

  lifecycle {
    precondition {
      condition     = var.confluent_cluster_id != null
      error_message = "enable_dd_streams is true but confluent_cluster_id is not set. Export TF_VAR_confluent_cluster_id from the cloud dir output kafka_cluster_id."
    }
  }
}
