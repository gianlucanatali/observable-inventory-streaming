output "public_ip" {
  description = "Public IPv4 of the demo VM."
  value       = aws_instance.main.public_ip
}

output "instance_id" {
  description = "EC2 instance ID (also the SSM session target)."
  value       = aws_instance.main.id
}

output "architecture" {
  description = "CPU architecture of the selected AMI."
  value       = local.arch
}

output "docker_context_hint" {
  description = "Command to create a Docker context over SSH. Wait for cloud-init to finish first (see README)."
  value       = "docker context create ${local.name} --docker \"host=ssh://ubuntu@${aws_instance.main.public_ip}\" && docker context use ${local.name}"
}

output "ingress_base_url" {
  description = "Public base URL of the demo VM (http://<public ip>). Pass it to overlay/terraform/datadog as -var ingress_base_url=... for the Synthetics tests."
  value       = "http://${aws_instance.main.public_ip}"
}

output "dd_synthetics_cidrs" {
  description = "Datadog Synthetics source ranges allowed on port 80 (empty while enable_dd_synthetics is false)."
  value       = sort(tolist(local.dd_synthetics_cidrs))
}

output "env_file" {
  description = "Lines for the stack env file: what the host is, for cost-meter."
  value       = join("\n", ["COST_EC2_INSTANCE_TYPE=${var.instance_type}", "COST_EBS_GB=${var.root_volume_gb}"])
}
