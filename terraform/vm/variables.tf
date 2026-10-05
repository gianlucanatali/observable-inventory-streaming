variable "stack" {
  description = "Stack name. Names every resource dd-demo-<stack> and sets the stack tag. Must equal the Terraform workspace name (terraform workspace new <stack>)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.stack))
    error_message = "stack must match ^[a-z][a-z0-9-]{1,15}$ (lowercase letters, digits, dashes; 2 to 16 characters)."
  }
}

variable "aws_profile" {
  description = "AWS CLI profile. Empty: credentials from the environment (AWS_ACCESS_KEY_ID, ...), as stack.sh does."
  type        = string
  default     = "dd-demo"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-west-1"
}

variable "owner" {
  description = "Value of the owner tag."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Default is Graviton arm64 (matches the local arm64 build VM). t3.xlarge (x86_64) is the alternative named in plan section 8. The AMI architecture is derived from this value. Price and architecture fit are being verified: confirm before apply."
  type        = string
  default     = "t4g.xlarge"
}

variable "root_volume_gb" {
  description = "Root gp3 volume size in GB."
  type        = number
  default     = 40
}

variable "presenter_cidr" {
  description = "Presenter public IP as a /32 (curl -s https://checkip.amazonaws.com). Only source allowed on 22, 80 and 443."
  type        = string

  validation {
    condition     = can(cidrhost(var.presenter_cidr, 0)) && endswith(var.presenter_cidr, "/32")
    error_message = "presenter_cidr must be a single address in CIDR form, for example 203.0.113.7/32."
  }
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key file to register as the key pair."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "bedrock_model_arns" {
  description = "Model / inference-profile ARNs the instance role may invoke (bedrock:InvokeModel*). Empty means no Bedrock statement."
  type        = list(string)
  default     = []
}

variable "enable_dd_synthetics" {
  description = "Layer dd-synthetics (contracts section 13b): opens TCP 80 on the security group to Datadog's published Synthetics IPv4 ranges for synthetics_location, and only while this is true. Fetches the ranges from https://ip-ranges.datadoghq.eu/synthetics.json at plan time (needs internet access)."
  type        = bool
  default     = false
}

variable "enable_control_center" {
  description = "Layer control-center: expose Legacy Control Center TCP 9021 only to presenter_cidr."
  type        = bool
  default     = true
}

variable "synthetics_location" {
  description = "Datadog-managed Synthetics location whose source IPs may reach port 80 (a key of prefixes_ipv4_by_location in the ranges file). Must match synthetics_location of overlay/terraform/datadog."
  type        = string
  default     = "aws:eu-central-1"
}
