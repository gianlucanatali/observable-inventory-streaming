output "alb_url" {
  description = "Public storefront URL. The ALB defaults to storefront and routes /api/availability/* through weighted inventory target groups."
  value       = "http://${aws_lb.main.dns_name}"
}

output "alb_listener_arn" {
  description = "HTTP listener ARN for W3 weighted routing."
  value       = aws_lb_listener.http.arn
}

output "alb_inventory_rule_arn" {
  description = "Inventory listener rule mutated by the weighted canary helper."
  value       = aws_lb_listener_rule.inventory.arn
}

output "redis_endpoint" {
  description = "ElastiCache endpoint for the on-prem Redis sink and cloud tasks."
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "ecs_cluster_name" {
  description = "ECS cluster name used by stack.sh deployment commands."
  value       = aws_ecs_cluster.main.name
}

output "ecs_services" {
  description = "Stable ECS service-name map for W4 deployments."
  value       = { for name, service in aws_ecs_service.app : name => service.name }
}

output "inventory_target_group_arns" {
  description = "Inventory target groups keyed by release suffix for ALB routing scripts."
  value       = { for release, target_group in aws_lb_target_group.inventory : release => target_group.arn }
}

output "demo_control_target_group_arn" {
  description = "Demo-control target group ARN for the /control listener rule."
  value       = aws_lb_target_group.demo_control.arn
}
