# Layer dd-rum: one RUM browser application for the shop. The shop SDK config takes
# the application id and client token (outputs rum_application_id, rum_client_token, env_file).
# datadog_rum_application has no tags argument: grouping is by the name prefix.

resource "datadog_rum_application" "shop" {
  count = var.enable_dd_rum ? 1 : 0

  name = "${local.env}-shop"
  type = "browser"
}
