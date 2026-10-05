output "dashboard_url" {
  description = "Path of the dashboard (prefix with your Datadog site, for example https://app.datadoghq.eu)."
  value       = datadog_dashboard_json.stock.url
}

output "online_dashboard_url" {
  description = "Path of the optional AWS-native online dashboard; null when Fargate is disabled. Prefix with the Datadog site."
  value       = one(datadog_dashboard_json.online[*].url)
}

output "monitor_ids" {
  description = "Monitor IDs by name."
  value = {
    p95_latency   = one(datadog_monitor.p95_latency[*].id)
    probe_age     = datadog_monitor.probe_age.id
    probe_no_data = datadog_monitor.probe_no_data.id
    feed_state    = datadog_monitor.feed_state.id
    connect_task  = datadog_monitor.connect_task.id
    host_no_data  = datadog_monitor.host_no_data.id
  }
}

output "rum_application_id" {
  description = "RUM application ID (null when enable_dd_rum is false)."
  value       = var.enable_dd_rum ? datadog_rum_application.shop[0].id : null
}

output "rum_client_token" {
  description = "RUM client token (null when enable_dd_rum is false)."
  value       = var.enable_dd_rum ? datadog_rum_application.shop[0].client_token : null
  sensitive   = true
}

output "env_file" {
  description = "KEY=VALUE lines for the overlay .env (RUM settings). Sensitive: redirect to a file, never print."
  value = join("\n", concat(
    ["STACK=${var.stack}"],
    var.enable_dd_rum ? [
      "DD_RUM_APPLICATION_ID=${datadog_rum_application.shop[0].id}",
      "DD_RUM_CLIENT_TOKEN=${datadog_rum_application.shop[0].client_token}",
    ] : [],
  ))
  sensitive = true
}
