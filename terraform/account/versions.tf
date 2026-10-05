terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.63"
    }
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 4.11"
    }
  }
}

# Credentials as in terraform/vm: stack.sh's generated credential_process profile, never keys in files.
provider "aws" {
  region  = var.region
  profile = var.aws_profile == "" ? null : var.aws_profile

  default_tags {
    tags = {
      project = "dd-demo"
      stack   = "account"
      owner   = var.owner
    }
  }
}

# Data Exports is a global service served from us-east-1.
provider "aws" {
  alias   = "us_east_1"
  region  = "us-east-1"
  profile = var.aws_profile == "" ? null : var.aws_profile

  default_tags {
    tags = {
      project = "dd-demo"
      stack   = "account"
      owner   = var.owner
    }
  }
}

# Keys only from the environment: DD_API_KEY and DD_APP_KEY.
provider "datadog" {
  api_url = var.datadog_api_url
}

# Credentials come ONLY from CONFLUENT_CLOUD_API_KEY / CONFLUENT_CLOUD_API_SECRET.
provider "confluent" {}
