variable "stack" {
  description = "Stack name. It must equal this stack's Terraform workspace."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.stack))
    error_message = "stack must match ^[a-z][a-z0-9-]{1,15}$ (lowercase letters, digits, dashes; 2 to 16 characters)."
  }
}

variable "owner" {
  description = "Value of the owner resource tag."
  type        = string
}

variable "region" {
  description = "AWS region for the AWS-native online side."
  type        = string
  default     = "eu-west-1"
}

variable "aws_profile" {
  description = "AWS CLI profile. Empty uses environment credentials."
  type        = string
  default     = "dd-demo"
}

variable "presenter_cidr" {
  description = "Presenter public IPv4 /32 allowed to use the public ALB."
  type        = string

  validation {
    condition     = can(cidrhost(var.presenter_cidr, 0)) && endswith(var.presenter_cidr, "/32")
    error_message = "presenter_cidr must be one IPv4 address in CIDR /32 form."
  }
}

variable "datadog_synthetics_cidrs" {
  description = "Datadog managed Synthetics IPv4 ranges allowed to test the public ALB."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.datadog_synthetics_cidrs : can(cidrhost(cidr, 0))])
    error_message = "datadog_synthetics_cidrs must contain valid IPv4 CIDRs."
  }
}

variable "on_prem_security_group_id" {
  description = "Security group of the stack's on-prem VM. It alone may connect to ElastiCache."
  type        = string
}

variable "on_prem_private_ip" {
  description = "Private IPv4 address of this stack's on-prem VM; demo-control uses its published database ports."
  type        = string
}

variable "on_prem_public_ip" {
  description = "Public IPv4 address of this stack's VM, allowed to reach the internet-facing ALB for smoke/scenario tools."
  type        = string
}

variable "kafka_bootstrap" {
  description = "Non-secret Kafka bootstrap endpoint supplied by stack.sh."
  type        = string
}
variable "schema_registry_url" {
  description = "Non-secret Schema Registry URL supplied by stack.sh."
  type        = string
}
variable "kafka_security_protocol" {
  description = "Kafka protocol used by cloud services."
  type        = string
  default     = "SASL_SSL"
}
variable "dd_site" {
  description = "Datadog site used by storefront."
  type        = string
  default     = "datadoghq.eu"
}
variable "confluent_environment_id" {
  description = "Non-secret Confluent environment ID for cost-meter."
  type        = string
}
variable "kafka_cluster_id" {
  description = "Non-secret Confluent Kafka cluster ID for cost-meter."
  type        = string
}
variable "flink_compute_pool_id" {
  description = "Non-secret Confluent Flink compute pool ID for cost-meter."
  type        = string
}

variable "remote_ec2_instance_type" {
  description = "Non-secret on-prem VM instance type, passed to Fargate for the remote static-cost estimate."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*\\.[a-z0-9]+$", var.remote_ec2_instance_type))
    error_message = "remote_ec2_instance_type must be an EC2 instance type such as t4g.xlarge."
  }
}

variable "remote_ebs_gb" {
  description = "Non-secret on-prem VM root EBS size in GB, passed to Fargate for the remote static-cost estimate."
  type        = number

  validation {
    condition     = var.remote_ebs_gb > 0
    error_message = "remote_ebs_gb must be greater than zero."
  }
}

variable "enable_offers" {
  description = "Run the optional offer-worker only when the offers layer and its ACLs are enabled."
  type        = bool
  default     = false
}

variable "enable_jev" {
  description = "Inject the optional Jev API key into the offer-worker. False preserves rule-default offers."
  type        = bool
  default     = false
}

variable "enable_dd_rum" {
  description = "Inject RUM settings into the storefront only when the dd-rum layer has produced them."
  type        = bool
  default     = false
}

variable "rum_application_id" {
  description = "Non-secret Datadog RUM application ID from the generated stack contract; null when dd-rum is off."
  type        = string
  default     = null
  nullable    = true
}

variable "enable_llmobs" {
  description = "Send the offer-worker's manual Jev spans to Datadog LLM Observability."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Immutable image tag that stack.sh pushes to each ECR repository."
  type        = string
  default     = "dev"
}

variable "inventory_weights" {
  description = "ALB weighted canary split for inventory releases 1.0.0, 1.1.0 and 1.2.0."
  type        = map(number)
  default = {
    "100" = 100
    "110" = 0
    "120" = 0
  }

  validation {
    condition     = length(setsubtract(toset(keys(var.inventory_weights)), toset(["100", "110", "120"]))) == 0 && length(setsubtract(toset(["100", "110", "120"]), toset(keys(var.inventory_weights)))) == 0 && sum(values(var.inventory_weights)) == 100 && alltrue([for weight in values(var.inventory_weights) : weight >= 0 && weight <= 100])
    error_message = "inventory_weights must contain only 100, 110 and 120, each 0..100, totaling 100."
  }
}

variable "enable_releases" {
  description = "Run inventory-api 1.1.0 and 1.2.0 for the releases layer."
  type        = bool
  default     = true
}

variable "service_sizing" {
  description = "Fargate CPU units and MiB per cloud app. Make exports TF_VAR_service_sizing from compose/calibration.fargate-<CPU>-<MiB>.env selected by FARGATE_SIZE; these defaults preserve the initial 2-vCPU regression sizing for direct validation."
  type = map(object({
    cpu    = number
    memory = number
  }))
  default = {
    stock-projector   = { cpu = 512, memory = 1024 }
    inventory-api-100 = { cpu = 512, memory = 1024 }
    inventory-api-110 = { cpu = 2048, memory = 4096 }
    inventory-api-120 = { cpu = 512, memory = 1024 }
    storefront        = { cpu = 512, memory = 1024 }
    offer-worker      = { cpu = 512, memory = 1024 }
    demo-control      = { cpu = 256, memory = 512 }
    cost-meter        = { cpu = 256, memory = 512 }
  }

  validation {
    condition     = length(setsubtract(toset(keys(var.service_sizing)), toset(["stock-projector", "inventory-api-100", "inventory-api-110", "inventory-api-120", "storefront", "offer-worker", "demo-control", "cost-meter"]))) == 0 && length(setsubtract(toset(["stock-projector", "inventory-api-100", "inventory-api-110", "inventory-api-120", "storefront", "offer-worker", "demo-control", "cost-meter"]), toset(keys(var.service_sizing)))) == 0
    error_message = "service_sizing must define every cloud application exactly once."
  }
}


variable "ssm_kms_key_arn" {
  description = "Optional customer-managed KMS key ARN used for the SecureString parameters. Null assumes the AWS-managed SSM key."
  type        = string
  default     = null
}
