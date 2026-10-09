variable "stack" {
  description = "Stack name. Scopes every query to env:dd-demo-<stack> and tags every resource stack:<stack>. Must equal the Terraform workspace name (terraform workspace new <stack>)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.stack))
    error_message = "stack must match ^[a-z][a-z0-9-]{1,15}$ (lowercase letters, digits, dashes; 2 to 16 characters)."
  }
}

variable "datadog_api_url" {
  description = "Datadog API URL for the account's site. The demo account is on the EU site."
  type        = string
  default     = "https://api.datadoghq.eu/"
}

variable "service" {
  description = "APM service name of the stock lookup API."
  type        = string
  default     = "inventory-api"
}

variable "p95_threshold_seconds" {
  description = "inventory-api p95 latency above which the latency monitor alerts (seconds)."
  type        = number
  default     = 0.2
}

variable "notification_handles" {
  description = "Notification handles appended to every monitor message, for example [\"@you@example.com\"]. Empty means monitors notify nobody."
  type        = list(string)
  default     = []
}

variable "sellable_age_threshold_seconds" {
  description = "stock.sellable.age above which the aggregate-path monitor alerts (seconds). Flink update latency is unmeasured: calibrate on the first cloud run."
  type        = number
  default     = 20
}

# ---------------------------------------------------------------------------------------------
# Layer toggles. `core` is always on.
# ---------------------------------------------------------------------------------------------
variable "enable_releases" {
  description = "Layer releases: the by-version latency (canary) monitor."
  type        = bool
  default     = false
}

variable "enable_restock" {
  description = "Layer restock: restock monitors and the dashboard group Restock."
  type        = bool
  default     = false
}

variable "enable_offers" {
  description = "Layer offers: the dashboard group Offers."
  type        = bool
  default     = false
}

variable "enable_dd_streams" {
  description = "Layer dd-streams: Confluent Cloud integration (account and cluster resource) and the consumer lag widget. Needs confluent_api_key, confluent_api_secret and confluent_cluster_id."
  type        = bool
  default     = false
}

variable "enable_dd_synthetics" {
  description = "Layer dd-synthetics: API test and browser test from a Datadog-managed public location against ingress_base_url."
  type        = bool
  default     = false
}

variable "enable_dd_rum" {
  description = "Layer dd-rum: RUM browser application (linked to traces by the shop SDK config)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------------------------
# Inputs of the dd-streams layer. They come from overlay/terraform/cloud outputs, via TF_VAR_*:
#   export TF_VAR_confluent_api_key=$(terraform -chdir=../cloud output -raw datadog_confluent_api_key)
#   export TF_VAR_confluent_api_secret=$(terraform -chdir=../cloud output -raw datadog_confluent_api_secret)
#   export TF_VAR_confluent_cluster_id=$(terraform -chdir=../cloud output -raw kafka_cluster_id)
# (the cloud workspace must be the same stack). Never put the values in a file or on a command line.
# ---------------------------------------------------------------------------------------------
variable "confluent_api_key" {
  description = "Confluent Cloud resource management API key (service account with MetricsViewer), from the cloud dir output datadog_confluent_api_key."
  type        = string
  sensitive   = true
  default     = null
}

variable "confluent_api_secret" {
  description = "Secret of confluent_api_key, from the cloud dir output datadog_confluent_api_secret."
  type        = string
  sensitive   = true
  default     = null
}

variable "confluent_cluster_id" {
  description = "Kafka cluster ID (lkc-...) to monitor, from the cloud dir output kafka_cluster_id."
  type        = string
  default     = null
}

# Why 250: a demo reset re-seeds stock with randint(0,40) units per position against a reorder point of 6, so about 17 % of
# the 1000 positions (roughly 170) qualify for a purchase order after every reset, with a new revision each time
# (flink/restock.sql). Re-orders after a reset are expected, and a threshold of 5 alerted on every one of them.
# Only a surge well above the reset burst (stores selling out faster than the supplier delivers) should alert.
variable "restock_open_growth_threshold" {
  description = "Increase of restock.orders.open over 15 minutes above which the restock monitor alerts. Must stay above the burst of about 170 re-orders that follows every demo reset."
  type        = number
  default     = 250
}

variable "synthetics_latency_budget_ms" {
  description = "Response time budget of the Synthetics API test on /api/availability (milliseconds)."
  type        = number
  default     = 800
}

variable "ingress_base_url" {
  description = "Public base URL of the demo VM, no trailing slash, for example http://203.0.113.7 (output ingress_base_url of overlay/terraform/vm). Required while enable_dd_synthetics is true."
  type        = string
  default     = null

  validation {
    condition     = var.ingress_base_url == null || can(regex("^https?://[^/]+$", var.ingress_base_url))
    error_message = "ingress_base_url must look like http://host (scheme and host, no path and no trailing slash)."
  }
}

variable "synthetics_location" {
  description = "Datadog-managed Synthetics location id for the tests. Must equal synthetics_location of overlay/terraform/vm (the security group admits that location's source IPs). Id format as in the provider docs, for example aws:eu-central-1."
  type        = string
  default     = "aws:eu-central-1"
}

variable "shop_url" {
  description = "Public base URL of the stack's online shop (the ALB, or the VM when not hybrid), no path, for example http://example-alb.eu-central-1.elb.amazonaws.com. The demo home links the shop, the control panel (/control/) and a product page from it. Empty renders the links as a hint to run ./demo links."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^(https?://[^/]+/?)?$", var.shop_url))
    error_message = "shop_url must be empty or look like http://host (scheme and host, no path)."
  }
}

variable "incident_window" {
  description = "Optional fixed time window of a recorded all-traffic 1.1.0 incident on this stack (Unix ms, from_ms < to_ms) and a short label for the demo home note, for example \"2026-10-09 09:09-09:38 UTC\". Null (default): the Chapter 3 note explains how to record one. Set it in an untracked *.auto.tfvars file next to this module (gitignored), since the window is only meaningful for your own account's data."
  type = object({
    from_ms = number
    to_ms   = number
    label   = string
  })
  default = null

  validation {
    condition     = var.incident_window == null || try(var.incident_window.from_ms < var.incident_window.to_ms, false)
    error_message = "incident_window.from_ms must be before incident_window.to_ms."
  }
}
