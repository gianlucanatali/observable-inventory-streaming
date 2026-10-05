data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.layer_tags
}

# ECR pulls and CloudWatch Logs use AWS's maintained execution policy. SSM is an explicit, stack-local policy below.
resource "aws_iam_role_policy_attachment" "execution_base" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadOnlyThisStacksSecureStrings"
    actions   = ["ssm:GetParameters"]
    resources = values(local.secret_parameter_arns)
  }

  dynamic "statement" {
    for_each = var.ssm_kms_key_arn == null ? [] : [var.ssm_kms_key_arn]
    content {
      sid       = "DecryptOnlyConfiguredSecureStringKey"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-stack-ssm-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.layer_tags
}

data "aws_iam_policy_document" "task_cost_meter" {
  statement {
    sid       = "ReadHybridECSRuntime"
    actions   = ["ecs:ListTasks", "ecs:DescribeTasks"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "task_firelens_cloudwatch" {
  statement {
    sid       = "WriteApplicationLogsThroughFireLens"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for group in aws_cloudwatch_log_group.app : "${group.arn}:*"]
  }

  statement {
    sid       = "DescribeApplicationLogStreams"
    actions   = ["logs:DescribeLogStreams"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_cost_meter" {
  name   = "read-hybrid-ecs-runtime"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_cost_meter.json
}

resource "aws_iam_role_policy" "task_firelens_cloudwatch" {
  name   = "write-application-logs-through-firelens"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_firelens_cloudwatch.json
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = local.applications
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = 7
  tags              = local.layer_tags
}

locals {
  base_environment = [{ name = "DD_ENV", value = "dd-demo-${var.stack}" }, { name = "DD_SERVICE", value = "" }, { name = "DD_VERSION", value = "" }, { name = "DD_TAGS", value = "project:dd-demo stack:${var.stack}" }, { name = "DD_AGENT_HOST", value = "127.0.0.1" }, { name = "DD_DOGSTATSD_PORT", value = "8125" }, { name = "REDIS_URL", value = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0" }]
  datadog_instrumented_apps = toset([
    "stock-projector",
    "inventory-api-100",
    "inventory-api-110",
    "inventory-api-120",
    "storefront",
    "offer-worker",
  ])
  kafka_environment = [{ name = "KAFKA_BOOTSTRAP", value = var.kafka_bootstrap }, { name = "SR_URL", value = var.schema_registry_url }, { name = "KAFKA_SECURITY_PROTOCOL", value = var.kafka_security_protocol }]
  store_hosts       = "S01=${var.on_prem_private_ip}:15431,S02=${var.on_prem_private_ip}:15432,S03=${var.on_prem_private_ip}:15433,S04=${var.on_prem_private_ip}:15434,S05=${var.on_prem_private_ip}:15435"
  app_environment = { for name, app in local.applications : name => concat(
    [for env in local.base_environment : merge(env, env.name == "DD_SERVICE" ? { value = app.service } : env.name == "DD_VERSION" ? { value = app.version } : {})],
    contains(local.datadog_instrumented_apps, name) ? [{ name = "DD_DATA_STREAMS_ENABLED", value = "true" }] : [],
    [{ name = "DD_LOGS_INJECTION", value = "true" }],
    contains(["stock-projector", "storefront", "offer-worker", "demo-control"], name) ? local.kafka_environment : [],
    contains(["stock-projector", "offer-worker", "demo-control"], name) ? [{ name = "STORE_HOSTS", value = local.store_hosts }] : [],
    startswith(name, "inventory-api-") ? [{ name = "PORT", value = "8080" }, { name = "WEB_CONCURRENCY", value = "4" }, { name = "STORE_HOSTS", value = local.store_hosts }, { name = "CATALOGUE_MODE", value = name == "inventory-api-100" ? "none" : name == "inventory-api-110" ? "per_request" : "startup" }] : [],
    name == "stock-projector" ? [{ name = "KAFKA_GROUP_ID", value = "stock-projector" }, { name = "MOVEMENTS_TOPIC", value = "stock.movements" }] : [],
    name == "storefront" ? concat([{ name = "OFFERS_ENABLED", value = tostring(var.enable_offers) }, { name = "DD_SITE", value = var.dd_site }, { name = "STACK", value = var.stack }], var.enable_dd_rum ? [{ name = "DD_RUM_APPLICATION_ID", value = var.rum_application_id }] : []) : [],
    name == "offer-worker" ? concat(var.enable_llmobs ? [{ name = "DD_LLMOBS_ENABLED", value = "1" }, { name = "DD_LLMOBS_ML_APP", value = "urbanstreet-offers" }] : [], [{ name = "KAFKA_GROUP_ID", value = "offer-worker" }, { name = "RISK_TOPIC", value = "carts.at-risk" }, { name = "OFFERS_TOPIC", value = "offers" }, { name = "BEDROCK_ENABLED", value = "false" }, { name = "AWS_REGION", value = var.region }, { name = "OFFERS_KILL_SWITCH", value = "false" }]) : [],
    name == "demo-control" ? [{ name = "STACK", value = var.stack }, { name = "CONFIG_TOPIC", value = "demo.config" }, { name = "PG_PORT", value = "5432" }, { name = "PG_DATABASE", value = "inventory" }, { name = "PG_WRITER_USER", value = "stock_writer" }, { name = "PROCUREMENT_HOST", value = var.on_prem_private_ip }, { name = "PROCUREMENT_PORT", value = "15436" }, { name = "PROCUREMENT_DATABASE", value = "procurement" }, { name = "PROCUREMENT_USER", value = "procurement" }] : [],
    name == "cost-meter" ? [{ name = "STACK", value = var.stack }, { name = "CONFLUENT_ENVIRONMENT_ID", value = var.confluent_environment_id }, { name = "KAFKA_CLUSTER_ID", value = var.kafka_cluster_id }, { name = "FLINK_COMPUTE_POOL_ID", value = var.flink_compute_pool_id }, { name = "COST_EC2_INSTANCE_TYPE", value = "" }, { name = "COST_EBS_GB", value = "" }, { name = "COST_REMOTE_EC2_INSTANCE_TYPE", value = var.remote_ec2_instance_type }, { name = "COST_REMOTE_EBS_GB", value = tostring(var.remote_ebs_gb) }, { name = "COST_REMOTE_PUBLIC_IPV4_COUNT", value = "1" }, { name = "AWS_REGION", value = var.region }, { name = "COST_ECS_CLUSTER", value = aws_ecs_cluster.main.name }, { name = "COST_ELASTICACHE_NODES", value = "1" }, { name = "COST_ALB_COUNT", value = "1" }] : [],
  ) }

  app_secrets = {
    for name, keys in local.app_secret_keys : name => [
      for key in keys : {
        name      = local.secret_environment_names[key]
        valueFrom = local.secret_parameter_arns[key]
      }
    ]
  }
}

resource "aws_ecs_task_definition" "app" {
  for_each                 = local.applications
  family                   = "${local.name}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.service_sizing[each.key].cpu)
  memory                   = tostring(var.service_sizing[each.key].memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  tags = local.layer_tags

  container_definitions = jsonencode([
    {
      name        = "app"
      image       = "${aws_ecr_repository.app[each.value.image].repository_url}:${var.image_tag}"
      essential   = true
      environment = local.app_environment[each.key]
      secrets     = local.app_secrets[each.key]
      dockerLabels = {
        "com.datadoghq.tags.env"     = "dd-demo-${var.stack}"
        "com.datadoghq.tags.service" = each.value.service
        "com.datadoghq.tags.version" = each.value.version
        "com.datadoghq.tags.layer"   = each.value.layer
      }
      portMappings = each.value.port == null ? [] : [{
        containerPort = each.value.port
        hostPort      = each.value.port
        protocol      = "tcp"
      }]
      logConfiguration = {
        logDriver = "awsfirelens"
        options = {
          Name           = "datadog"
          Host           = "http-intake.logs.datadoghq.eu"
          TLS            = "on"
          provider       = "ecs"
          dd_service     = each.value.service
          dd_source      = "python"
          dd_message_key = "log"
          dd_tags        = "env:dd-demo-${var.stack},service:${each.value.service},version:${each.value.version},project:dd-demo,stack:${var.stack}"
        }
        secretOptions = [{ name = "apikey", valueFrom = local.secret_parameter_arns.DD_API_KEY }]
      }
      dependsOn = [{ containerName = "datadog-agent", condition = "START" }, { containerName = "log-router", condition = "START" }]
    },
    {
      name      = "log-router"
      image     = "${aws_ecr_repository.app["log-router"].repository_url}:${var.image_tag}"
      essential = true
      environment = [
        { name = "AWS_REGION", value = var.region },
        { name = "LOG_GROUP_NAME", value = aws_cloudwatch_log_group.app[each.key].name },
      ]
      firelensConfiguration = {
        type = "fluentbit"
        options = {
          enable-ecs-log-metadata = "true"
          config-file-type        = "file"
          config-file-value       = "/extra.conf"
        }
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app[each.key].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "log-router"
        }
      }
    },
    {
      name      = "datadog-agent"
      image     = "gcr.io/datadoghq/agent:7.83.3@sha256:c5bf5ec9be0c51d3d2d47bdadbb82680b5c83ceb9cae8457fe44aa736a1784ad"
      essential = false
      environment = [
        { name = "ECS_FARGATE", value = "true" },
        { name = "DD_APM_ENABLED", value = "true" },
        { name = "DD_SITE", value = var.dd_site },
        { name = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC", value = "true" },
        { name = "DD_ENV", value = "dd-demo-${var.stack}" },
        { name = "DD_SERVICE", value = each.value.service },
        { name = "DD_VERSION", value = each.value.version },
        { name = "DD_TAGS", value = "project:dd-demo stack:${var.stack} service:${each.value.service} version:${each.value.version} layer:${each.value.layer}" },
      ]
      secrets = [{ name = "DD_API_KEY", valueFrom = local.secret_parameter_arns.DD_API_KEY }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app[each.key].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "datadog-agent"
        }
      }
    },
  ])
}

resource "aws_ecs_service" "app" {
  for_each                          = local.applications
  name                              = "${local.name}-${each.key}"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.app[each.key].arn
  desired_count                     = startswith(each.key, "inventory-api-") && each.key != "inventory-api-100" && !var.enable_releases ? 0 : each.key == "offer-worker" && !var.enable_offers ? 0 : 1
  health_check_grace_period_seconds = startswith(each.key, "inventory-api-") ? 120 : null
  launch_type                       = "FARGATE"
  platform_version                  = "LATEST"
  tags                              = local.layer_tags

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }

  dynamic "load_balancer" {
    for_each = each.key == "storefront" ? [aws_lb_target_group.storefront] : each.key == "demo-control" ? [aws_lb_target_group.demo_control] : startswith(each.key, "inventory-api-") ? [aws_lb_target_group.inventory[replace(each.key, "inventory-api-", "")]] : []
    content {
      target_group_arn = load_balancer.value.arn
      container_name   = "app"
      container_port   = each.value.port
    }
  }

  depends_on = [aws_iam_role_policy.execution_secrets, aws_iam_role_policy.task_firelens_cloudwatch, terraform_data.workspace_guard]
}
