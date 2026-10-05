variable "stack" {
  description = "Stack name. Prefixes every Confluent display name (dd-demo-<stack>-...). Must equal the Terraform workspace name (terraform workspace new <stack>)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.stack))
    error_message = "stack must match ^[a-z][a-z0-9-]{1,15}$ (lowercase letters, digits, dashes; 2 to 16 characters)."
  }
}

variable "region" {
  description = "AWS region for the Kafka cluster, Schema Registry and Flink pool: eu-west-1."
  type        = string
  default     = "eu-west-1"
}

variable "kafka_availability" {
  description = "Basic clusters support SINGLE_ZONE only."
  type        = string
  default     = "SINGLE_ZONE"
}

variable "flink_max_cfu" {
  description = "Maximum CFUs of the Flink compute pool (caps Flink spend)."
  type        = number
  default     = 5 # smallest accepted value: 5, 10, 20, 30, 40, 50 (confluent_flink_compute_pool docs)
}

variable "connect_group_id" {
  description = "group.id of the self-managed Kafka Connect worker (distributed mode). Must match the worker config."
  type        = string
  default     = "dd-demo-connect"
}

variable "projector_group_id" {
  description = "Consumer group of stock-projector (contract section 3)."
  type        = string
  default     = "stock-projector"
}

variable "sellable_sink_group_id" {
  description = "Consumer group of the Redis sink connector `sellable-redis` (Connect default: connect-<connector name>)."
  type        = string
  default     = "connect-sellable-redis"
}

variable "restock_sink_group_id" {
  description = "Consumer group of the JDBC sink connector `restock-procurement` (Connect default: connect-<connector name>)."
  type        = string
  default     = "connect-restock-procurement"
}

# ---------------------------------------------------------------------------------------------
# Layer toggles. `core` is always on. `releases` and `dd-synthetics` and `dd-rum`
# create nothing on Confluent, so they have no variable here.
# ---------------------------------------------------------------------------------------------
variable "enable_restock" {
  description = "Layer restock: topic restock.requests, Flink statements of overlay/flink/restock.sql (needs the file), Connect read access for the JDBC sink."
  type        = bool
  default     = true
}

variable "enable_offers" {
  description = "Layer offers: topics offers and carts.at-risk, offer-worker/storefront access, Flink statements of overlay/flink/cart_at_risk.sql (needs the file)."
  type        = bool
  default     = true
}

variable "enable_dd_streams" {
  description = "Layer dd-streams: read-only (MetricsViewer) service account and Cloud resource management API key for the Datadog Confluent Cloud integration. Exposed as sensitive outputs."
  type        = bool
  default     = true
}

variable "enable_control_center" {
  description = "Layer control-center: dedicated Legacy Control Center identity, keys, ACLs, and Basic-cluster internal topics."
  type        = bool
  default     = true
}

variable "enable_catalog_tags" {
  description = "Create Stream Catalog tags (project, stack, layer) and bind them to topics. Keep false until the ESSENTIALS package and the required roles are confirmed in the account."
  type        = bool
  default     = false
}

variable "flink_deferred" {
  description = "Flink statement files (sellable, offers, demand, procurement, restock) NOT to run yet, because an input topic has no schema yet. Set by overlay/compose/scripts/stack.sh; [] runs everything the enabled layers need."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for f in var.flink_deferred : contains(["sellable", "offers", "demand", "procurement", "restock"], f)])
    error_message = "flink_deferred accepts only: sellable, offers, demand, procurement, restock."
  }
}

variable "flink_dml_deferred" {
  description = "Flink statement files whose CREATE TABLE runs now but whose INSERT waits for stack.sh readiness polling."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for f in var.flink_dml_deferred : contains(["sellable", "offers", "demand", "procurement", "restock"], f)])
    error_message = "flink_dml_deferred accepts only: sellable, offers, demand, procurement, restock."
  }
}
