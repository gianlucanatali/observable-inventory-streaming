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
  description = "AWS region of the stack (the ECS tasks and the VM pull from here, so in-region pulls cost nothing)."
  type        = string
  default     = "eu-west-1"
}

variable "aws_profile" {
  description = "AWS CLI profile. Empty uses environment credentials."
  type        = string
  default     = "dd-demo"
}

variable "kept_tags_per_image" {
  description = "Content tags (c-*) kept per repository by the lifecycle policy; older ones expire."
  type        = number
  default     = 5

  validation {
    condition     = var.kept_tags_per_image >= 1 && var.kept_tags_per_image <= 100 && floor(var.kept_tags_per_image) == var.kept_tags_per_image
    error_message = "kept_tags_per_image must be a whole number from 1 to 100."
  }
}
