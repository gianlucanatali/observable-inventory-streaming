# Optional organization-wide resource adoption. Leave every import ID empty for
# a new account; set only the IDs that already exist in the adopting org.
import {
  to       = confluent_service_account.cost_meter
  id       = var.cost_meter_service_account_import_id
  for_each = var.cost_meter_service_account_import_id == "" ? {} : { adopt = var.cost_meter_service_account_import_id }
}

import {
  to       = confluent_role_binding.cost_meter_billing_admin
  id       = var.cost_meter_billing_admin_import_id
  for_each = var.cost_meter_billing_admin_import_id == "" ? {} : { adopt = var.cost_meter_billing_admin_import_id }
}

import {
  to       = confluent_role_binding.cost_meter_metrics_viewer
  id       = var.cost_meter_metrics_viewer_import_id
  for_each = var.cost_meter_metrics_viewer_import_id == "" ? {} : { adopt = var.cost_meter_metrics_viewer_import_id }
}

import {
  to       = datadog_integration_confluent_account.cost_meter
  id       = var.cost_meter_datadog_integration_import_id
  for_each = var.cost_meter_datadog_integration_import_id == "" ? {} : { adopt = var.cost_meter_datadog_integration_import_id }
}
