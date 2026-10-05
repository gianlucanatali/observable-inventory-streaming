terraform {
  required_version = ">= 1.6.0"

  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 3.0"
    }
  }
}

# Keys come ONLY from the environment: DD_API_KEY and DD_APP_KEY (never in files or variables).
provider "datadog" {
  api_url = var.datadog_api_url
}
