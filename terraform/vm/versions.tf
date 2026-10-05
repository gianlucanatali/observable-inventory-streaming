terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}

# Credentials: the AWS CLI profile, never keys in files.
provider "aws" {
  region = var.region
  # Empty = use stack.sh's generated credential_process profile, which refreshes the short-lived
  # credentials from the presenter's longer `aws login` session during Terraform operations.
  profile = var.aws_profile == "" ? null : var.aws_profile

  default_tags {
    tags = {
      project = "dd-demo"
      stack   = var.stack
      owner   = var.owner
    }
  }
}
