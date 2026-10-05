terraform {
  required_version = ">= 1.6.0"

  required_providers {
    confluent = {
      source  = "confluentinc/confluent"
      version = "~> 2.63"
    }
  }
}

# Credentials come ONLY from the environment: CONFLUENT_CLOUD_API_KEY / CONFLUENT_CLOUD_API_SECRET.
provider "confluent" {}
