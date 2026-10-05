# Layer dd-synthetics (contracts section 13b): two tests run from a Datadog-managed public
# location against the VM's public address (var.ingress_base_url). No private location, no container.
# The VM security group admits only Datadog's published Synthetics ranges on port 80 (overlay/terraform/vm).

locals {
  synthetics_tags = ["project:dd-demo", "stack:${var.stack}", "layer:dd-synthetics", local.scope]
}

resource "datadog_synthetics_test" "availability_api" {
  count = var.enable_dd_synthetics ? 1 : 0

  name      = "[${local.env}] availability API P0042"
  type      = "api"
  subtype   = "http"
  status    = "live"
  message   = "GET /api/availability/P0042 failed its assertions (status 200, JSON status field, response time below ${var.synthetics_latency_budget_ms} ms).${local.notify}"
  locations = [var.synthetics_location]
  tags      = local.synthetics_tags

  depends_on = [terraform_data.synthetics_guard]

  request_definition {
    method = "GET"
    url    = "${var.ingress_base_url}/api/availability/P0042"
  }

  assertion {
    type     = "statusCode"
    operator = "is"
    target   = "200"
  }

  assertion {
    type     = "responseTime"
    operator = "lessThan"
    target   = tostring(var.synthetics_latency_budget_ms)
  }

  # contracts section 5: status is one of available, out_of_stock, unknown
  assertion {
    type     = "body"
    operator = "validatesJSONPath"

    targetjsonpath {
      jsonpath    = "$.status"
      operator    = "matches"
      targetvalue = "^(available|out_of_stock|unknown)$"
    }
  }

  options_list {
    tick_every = 60
  }
}

resource "datadog_synthetics_test" "shop_browser" {
  count = var.enable_dd_synthetics ? 1 : 0

  name       = "[${local.env}] shop product page P0042"
  type       = "browser"
  status     = "live"
  message    = "The product page /#/product/P0042 did not show its online availability text.${local.notify}"
  locations  = [var.synthetics_location]
  device_ids = ["chrome.laptop_large"]
  tags       = local.synthetics_tags

  depends_on = [terraform_data.synthetics_guard]

  request_definition {
    method = "GET"
    url    = "${var.ingress_base_url}/#/product/P0042"
  }

  # Let the single page app fetch the availability before asserting.
  browser_step {
    name = "Wait for the availability call"
    type = "wait"

    params {
      value = "3"
    }
  }

  # Both outcomes the shop can show for P0042 contain "online": "available online" and "Out of stock online".
  # A step takes one string, so the assertion is the shared word; confirm the shop wording during the first live run.
  browser_step {
    name = "Page shows the online availability text"
    type = "assertPageContains"

    params {
      value = "online"
    }
  }

  options_list {
    tick_every = 300
  }
}

# Fails the plan loudly when the layer is on but there is no target address.
resource "terraform_data" "synthetics_guard" {
  count = var.enable_dd_synthetics ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.ingress_base_url != null
      error_message = "enable_dd_synthetics is true but ingress_base_url is not set. Pass -var ingress_base_url=$(terraform -chdir=../vm output -raw ingress_base_url) (same stack's workspace)."
    }
  }
}
