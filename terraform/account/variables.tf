variable "region" {
  description = "Region of the export bucket (same as the stacks)."
  type        = string
  default     = "eu-west-1"
}

variable "aws_profile" {
  description = "AWS CLI profile; empty = environment credentials (stack.sh exports them)."
  type        = string
  default     = ""
}

variable "owner" {
  description = "owner tag."
  type        = string
}

variable "datadog_api_url" {
  description = "Datadog API URL of the org's site."
  type        = string
  default     = "https://api.datadoghq.eu/"
}

variable "datadog_aws_account_id" {
  description = "Datadog's AWS account that assumes the integration role: 464622532012 for app.datadoghq.com, us3, us5 and app.datadoghq.eu (docs.datadoghq.com/integrations/guide/aws-terraform-setup/, read 2026-10-04)."
  type        = string
  default     = "464622532012"
}

variable "export_name" {
  description = "Name of the CUR 2.0 export (also its S3 path segment)."
  type        = string
  default     = "dd-demo-cur2"
}

variable "export_prefix" {
  description = "S3 prefix of the export: no leading or trailing slash (Datadog CCM requirement)."
  type        = string
  default     = "cur"
}

variable "cost_meter_service_account_import_id" {
  description = "Optional existing Confluent service-account ID to adopt; empty creates a new account."
  type        = string
  default     = ""
}

variable "cost_meter_billing_admin_import_id" {
  description = "Optional existing BillingAdmin role-binding ID to adopt; empty creates a new binding."
  type        = string
  default     = ""
}

variable "cost_meter_metrics_viewer_import_id" {
  description = "Optional existing MetricsViewer role-binding ID to adopt; empty creates a new binding."
  type        = string
  default     = ""
}

variable "cost_meter_datadog_integration_import_id" {
  description = "Optional existing Datadog Confluent integration ID to adopt; empty creates a new integration."
  type        = string
  default     = ""
}
