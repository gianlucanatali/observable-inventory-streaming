output "cur_bucket" {
  description = "S3 bucket holding the CUR 2.0 export."
  value       = aws_s3_bucket.cur.id
}

output "datadog_role_arn" {
  description = "IAM role Datadog assumes for Cloud Cost Management."
  value       = aws_iam_role.datadog.arn
}

output "cost_dashboard_url" {
  description = "Path of the durable account-wide cost dashboard; prefix with your Datadog site."
  value       = datadog_dashboard_json.cost.url
}

output "cost_meter_confluent_api_key" {
  description = "Confluent Cloud API key ID for the account-wide cost meter and Datadog integration."
  value       = confluent_api_key.cost_meter.id
  sensitive   = true
}

output "cost_meter_confluent_api_secret" {
  description = "Confluent Cloud API key secret for the account-wide cost meter and Datadog integration."
  value       = confluent_api_key.cost_meter.secret
  sensitive   = true
}
