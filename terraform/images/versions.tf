terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Credentials come from the selected AWS profile or environment, never from Terraform variables.
provider "aws" {
  region  = var.region
  profile = var.aws_profile == "" ? null : var.aws_profile

  default_tags {
    tags = {
      project = "dd-demo"
      stack   = var.stack
      owner   = var.owner
    }
  }
}
