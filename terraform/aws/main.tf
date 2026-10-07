data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  name       = "dd-demo-${var.stack}"
  layer_tags = { layer = "cloud" }
  # ALB target groups: seconds an old task keeps draining on deploy. Requests are short lookups and panel calls (no
  # long-lived connections), and the 300 s AWS default made each ECS rolling deploy take about ten minutes.
  alb_deregistration_delay_s = 20

  applications = {
    stock-projector   = { image = "stock-projector", port = null, service = "stock-projector", version = "1.0.0", layer = "core" }
    inventory-api-100 = { image = "inventory-api", port = 8080, service = "inventory-api", version = "1.0.0", layer = "core" }
    inventory-api-110 = { image = "inventory-api", port = 8080, service = "inventory-api", version = "1.1.0", layer = "releases" }
    inventory-api-120 = { image = "inventory-api", port = 8080, service = "inventory-api", version = "1.2.0", layer = "releases" }
    storefront        = { image = "storefront", port = 8000, service = "storefront", version = "1.0.0", layer = "core" }
    offer-worker      = { image = "offer-worker", port = null, service = "offer-worker", version = "1.0.0", layer = "offers" }
    demo-control      = { image = "demo-control", port = 8000, service = "demo-control", version = "1.0.0", layer = "core" }
    cost-meter        = { image = "cost-meter", port = null, service = "cost-meter", version = "1.0.0", layer = "core" }
  }


  secret_parameter_names = toset(concat(["DD_API_KEY", "PROJECTOR_KAFKA_API_KEY", "PROJECTOR_KAFKA_API_SECRET", "PROJECTOR_SR_API_KEY", "PROJECTOR_SR_API_SECRET", "STOREFRONT_KAFKA_API_KEY", "STOREFRONT_KAFKA_API_SECRET", "STOREFRONT_SR_API_KEY", "STOREFRONT_SR_API_SECRET", "OFFERS_KAFKA_API_KEY", "OFFERS_KAFKA_API_SECRET", "OFFERS_SR_API_KEY", "OFFERS_SR_API_SECRET", "DEMO_CONTROL_KAFKA_API_KEY", "DEMO_CONTROL_KAFKA_API_SECRET", "DEMO_CONTROL_SR_API_KEY", "DEMO_CONTROL_SR_API_SECRET", "CONTROL_PASSWORD", "SCENARIO_API_TOKEN", "PG_WRITER_PASSWORD", "PG_PROCUREMENT_PASSWORD", "COST_METER_API_KEY", "COST_METER_API_SECRET"], var.enable_jev ? ["JEV_API_KEY"] : [], var.enable_dd_rum ? ["DD_RUM_CLIENT_TOKEN"] : []))
  secret_parameter_arns  = { for name in local.secret_parameter_names : name => "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/dd-demo/${var.stack}/${name}" }

  app_secret_keys = {
    stock-projector   = ["DD_API_KEY", "PROJECTOR_KAFKA_API_KEY", "PROJECTOR_KAFKA_API_SECRET", "PROJECTOR_SR_API_KEY", "PROJECTOR_SR_API_SECRET"]
    inventory-api-100 = ["DD_API_KEY"]
    inventory-api-110 = ["DD_API_KEY"]
    inventory-api-120 = ["DD_API_KEY"]
    storefront        = concat(["DD_API_KEY", "STOREFRONT_KAFKA_API_KEY", "STOREFRONT_KAFKA_API_SECRET", "STOREFRONT_SR_API_KEY", "STOREFRONT_SR_API_SECRET"], var.enable_dd_rum ? ["DD_RUM_CLIENT_TOKEN"] : [])
    offer-worker      = concat(["DD_API_KEY", "OFFERS_KAFKA_API_KEY", "OFFERS_KAFKA_API_SECRET", "OFFERS_SR_API_KEY", "OFFERS_SR_API_SECRET"], var.enable_jev ? ["JEV_API_KEY"] : [])
    demo-control      = ["DD_API_KEY", "DEMO_CONTROL_KAFKA_API_KEY", "DEMO_CONTROL_KAFKA_API_SECRET", "DEMO_CONTROL_SR_API_KEY", "DEMO_CONTROL_SR_API_SECRET", "CONTROL_PASSWORD", "SCENARIO_API_TOKEN", "PG_WRITER_PASSWORD", "PG_PROCUREMENT_PASSWORD"]
    cost-meter        = ["DD_API_KEY", "COST_METER_API_KEY", "COST_METER_API_SECRET"]
  }

  secret_environment_names = {
    DD_API_KEY                  = "DD_API_KEY"
    PROJECTOR_KAFKA_API_KEY     = "KAFKA_API_KEY"
    PROJECTOR_KAFKA_API_SECRET  = "KAFKA_API_SECRET"
    PROJECTOR_SR_API_KEY        = "SR_API_KEY"
    PROJECTOR_SR_API_SECRET     = "SR_API_SECRET"
    STOREFRONT_KAFKA_API_KEY    = "KAFKA_API_KEY"
    STOREFRONT_KAFKA_API_SECRET = "KAFKA_API_SECRET"
    STOREFRONT_SR_API_KEY       = "SR_API_KEY"
    STOREFRONT_SR_API_SECRET    = "SR_API_SECRET"
    OFFERS_KAFKA_API_KEY        = "KAFKA_API_KEY"
    OFFERS_KAFKA_API_SECRET     = "KAFKA_API_SECRET"
    OFFERS_SR_API_KEY           = "SR_API_KEY"
    OFFERS_SR_API_SECRET        = "SR_API_SECRET"
    JEV_API_KEY                 = "JEV_API_KEY"

    DEMO_CONTROL_KAFKA_API_KEY    = "KAFKA_API_KEY"
    DEMO_CONTROL_KAFKA_API_SECRET = "KAFKA_API_SECRET"
    DEMO_CONTROL_SR_API_KEY       = "SR_API_KEY"
    DEMO_CONTROL_SR_API_SECRET    = "SR_API_SECRET"
    CONTROL_PASSWORD              = "CONTROL_PASSWORD"
    SCENARIO_API_TOKEN            = "SCENARIO_API_TOKEN"
    PG_WRITER_PASSWORD            = "PG_WRITER_PASSWORD"
    PG_PROCUREMENT_PASSWORD       = "PROCUREMENT_PASSWORD"
    COST_METER_API_KEY            = "COST_METER_API_KEY"
    COST_METER_API_SECRET         = "COST_METER_API_SECRET"
    DD_RUM_CLIENT_TOKEN           = "DD_RUM_CLIENT_TOKEN"
  }

  required_secret_keys = toset(flatten(values(local.app_secret_keys)))

  # Images live in terraform/images, repository dd-demo-<stack>/<image>. The URL is built from the account
  # and region, so this root never reads that root's state and a destroy works after the images root is gone.
  ecs_images     = setunion(toset([for app in values(local.applications) : app.image]), toset(["log-router"]))
  image_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  image_uri      = { for image in local.ecs_images : image => "${local.image_registry}/${local.name}/${image}:${try(var.image_tags[image], "missing-tag")}" }
}

resource "terraform_data" "workspace_guard" {
  input = var.stack

  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "terraform.workspace is '${terraform.workspace}' but stack is '${var.stack}'. Create/select the matching workspace first."
    }
    precondition {
      condition     = length(setsubtract(local.required_secret_keys, local.secret_parameter_names)) == 0
      error_message = "Every task secret must use a generated stack-local parameter name."
    }
    precondition {
      condition     = length(setsubtract(local.ecs_images, toset(keys(var.image_tags)))) == 0
      error_message = "image_tags needs a content tag for every ECS image (stack.sh writes them to .state/stack-<stack>.image-tags)."
    }
    precondition {
      condition     = !var.enable_dd_rum || (var.rum_application_id != null && trimspace(var.rum_application_id) != "")
      error_message = "dd-rum requires a non-empty RUM application ID from the generated Datadog stack contract."
    }
  }
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "dd-demo public ALB, presenter only"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from presenter"
  cidr_ipv4         = var.presenter_cidr
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  tags              = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_synthetics" {
  for_each = var.datadog_synthetics_cidrs

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from Datadog managed Synthetics"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  tags              = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_on_prem" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from this stack on-prem VM tools"
  cidr_ipv4         = "${var.on_prem_public_ip}/32"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  tags              = local.layer_tags
}

resource "aws_vpc_security_group_egress_rule" "alb_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to ECS applications"
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8080
  tags                         = local.layer_tags
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "dd-demo Fargate tasks reached only through ALB"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "HTTP from the stack ALB"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8080
  ip_protocol                  = "tcp"
  tags                         = local.layer_tags
}

resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  description       = "Outbound to AWS, Confluent Cloud and Datadog"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  tags              = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "on_prem_databases" {
  for_each = toset(["15431", "15432", "15433", "15434", "15435", "15436"])

  security_group_id            = var.on_prem_security_group_id
  description                  = "Hybrid demo control from ECS tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  ip_protocol                  = "tcp"
  tags                         = local.layer_tags
}

# Identity-only group attached to the demo-control task (in addition to "tasks"): it has no rules of its own and
# exists so the VM can admit Kafka Connect REST from demo-control alone. Connect REST has no authentication.
resource "aws_security_group" "demo_control" {
  name        = "${local.name}-demo-control"
  description = "dd-demo demo-control task identity for Connect REST on the VM"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "on_prem_connect_rest" {
  security_group_id            = var.on_prem_security_group_id
  description                  = "Kafka Connect REST from the demo-control task only (store feed pause/resume)"
  referenced_security_group_id = aws_security_group.demo_control.id
  from_port                    = 8083
  to_port                      = 8083
  ip_protocol                  = "tcp"
  tags                         = local.layer_tags
}

# The scenario API on the VM (load / verify / canary-check for the Checks card). Bearer token in the app;
# the network boundary is this group reference, never a CIDR.
resource "aws_vpc_security_group_ingress_rule" "on_prem_scenario_api" {
  security_group_id            = var.on_prem_security_group_id
  description                  = "Scenario API from the demo-control task only (load, verify, canary-check)"
  referenced_security_group_id = aws_security_group.demo_control.id
  from_port                    = 8090
  to_port                      = 8090
  ip_protocol                  = "tcp"
  tags                         = local.layer_tags
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "dd-demo ElastiCache reached only by tasks and on-prem VM"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "redis_tasks" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from ECS tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
  tags                         = local.layer_tags
}

resource "aws_vpc_security_group_ingress_rule" "redis_on_prem" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from this stack on-prem VM"
  referenced_security_group_id = var.on_prem_security_group_id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
  tags                         = local.layer_tags
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = data.aws_subnets.default.ids
  tags       = local.layer_tags
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "${local.name}-redis"
  engine               = "redis"
  node_type            = "cache.t4g.small"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.redis.name
  security_group_ids   = [aws_security_group.redis.id]
  apply_immediately    = false
  tags                 = local.layer_tags
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  tags = local.layer_tags

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_lb" "main" {
  name               = local.name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
  tags               = local.layer_tags
}

resource "aws_lb_target_group" "storefront" {
  name        = "${local.name}-storefront"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.layer_tags

  deregistration_delay = local.alb_deregistration_delay_s
  health_check { path = "/healthz" }
}

resource "aws_lb_target_group" "inventory" {
  for_each    = toset(["100", "110", "120"])
  name        = "${local.name}-inventory-${each.value}"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.layer_tags

  deregistration_delay = local.alb_deregistration_delay_s
  health_check { path = "/readyz" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.storefront.arn
  }
}

resource "aws_lb_listener_rule" "inventory" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type = "forward"
    forward {
      dynamic "target_group" {
        for_each = aws_lb_target_group.inventory
        content {
          arn    = target_group.value.arn
          weight = var.inventory_weights[target_group.key]
        }
      }
    }
  }

  condition {
    path_pattern { values = ["/api/availability/*"] }
  }

  lifecycle {
    ignore_changes = [action]
  }
}

resource "aws_lb_target_group" "demo_control" {
  name                 = "${local.name}-demo-control"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = data.aws_vpc.default.id
  tags                 = local.layer_tags
  deregistration_delay = local.alb_deregistration_delay_s
  health_check { path = "/control/healthz" }
}

resource "aws_lb_listener_rule" "demo_control" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 5
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.demo_control.arn
  }
  condition {
    path_pattern { values = ["/control", "/control/*"] }
  }
}
