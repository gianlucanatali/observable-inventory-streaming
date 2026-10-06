# Account-wide Datadog Cloud Cost Management for AWS.
# Applied once, NOT per stack, and NOT destroyed with a stack: the cost history must survive stack-down.
#   make MODE=cloud STACK=account account-up | account-down
# Requirements (docs.datadoghq.com/cloud_cost_management/setup/aws/, read 2026-10-04): CUR, include resource IDs,
# split cost allocation data, hourly, new report version, GZIP or Parquet, prefix without leading/trailing slash.
# Data appears in Datadog 48-72 h after the first complete report.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "confluent_organization" "main" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  bucket     = "dd-demo-cur-${local.account_id}"
  # Literal: the integration names the role and the role trusts the integration's external ID (no cycle).
  role_name = "DatadogIntegrationRole-dd-demo"
  # Data Exports rejects SELECT *: every column of the table, as listed by AWS (see the file header).
  cur_columns = [for l in split("\n", file("${path.module}/cur2_columns.txt")) : trimspace(l) if trimspace(l) != "" && !startswith(trimspace(l), "#")]
}

# ---------------------------------------------------------------------------------------------
# Account-wide Confluent/Datadog cost identity. The operator created the service account and
# exactly these organization-level role bindings before this directory managed them. Import them
# before the first apply; do not replace either binding with a new grant.
# ---------------------------------------------------------------------------------------------
resource "confluent_service_account" "cost_meter" {
  display_name = "dd-demo-sa-cost-meter"
  description  = "Read-only cost-meter"
  lifecycle { prevent_destroy = true }
}

resource "confluent_role_binding" "cost_meter_billing_admin" {
  principal   = "User:${confluent_service_account.cost_meter.id}"
  role_name   = "BillingAdmin"
  crn_pattern = data.confluent_organization.main.resource_name
  lifecycle { prevent_destroy = true }
}

resource "confluent_role_binding" "cost_meter_metrics_viewer" {
  principal   = "User:${confluent_service_account.cost_meter.id}"
  role_name   = "MetricsViewer"
  crn_pattern = data.confluent_organization.main.resource_name
  lifecycle { prevent_destroy = true }
}

# Cloud resource management key used by the account-wide Datadog Confluent integration and the
# cost meter. Its secret is generated only at create time and is intentionally output as sensitive.
resource "confluent_api_key" "cost_meter" {
  display_name = "dd-demo-cost-meter"
  description  = "Account-wide cost meter and Datadog Confluent Cloud integration"

  owner {
    id          = confluent_service_account.cost_meter.id
    api_version = confluent_service_account.cost_meter.api_version
    kind        = confluent_service_account.cost_meter.kind
  }

  depends_on = [
    confluent_role_binding.cost_meter_billing_admin,
    confluent_role_binding.cost_meter_metrics_viewer,
  ]
}

# Existing account-level Datadog integration. Import it before the first apply so Terraform adopts
# rather than recreates the integration; the generated Confluent key supplies its credentials.
resource "datadog_integration_confluent_account" "cost_meter" {
  api_key    = confluent_api_key.cost_meter.id
  api_secret = confluent_api_key.cost_meter.secret
  tags       = ["project:dd-demo", "purpose:cost"]
}

# ---------------------------------------------------------------------------------------------
# Export bucket: private, encrypted, old report versions expire.
# ---------------------------------------------------------------------------------------------
resource "aws_s3_bucket" "cur" {
  bucket = local.bucket
}

resource "aws_s3_bucket_public_access_block" "cur" {
  bucket                  = aws_s3_bucket.cur.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cur" {
  bucket = aws_s3_bucket.cur.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cur" {
  bucket = aws_s3_bucket.cur.id
  rule {
    id     = "expire-old-reports"
    status = "Enabled"
    filter {}
    expiration {
      days = 120
    }
  }
}

# Data Exports writes as these service principals (docs.aws.amazon.com/cur/latest/userguide/dataexports-s3-bucket.html).
data "aws_iam_policy_document" "cur_bucket" {
  statement {
    sid     = "EnableAWSDataExportsToWriteToS3AndCheckPolicy"
    actions = ["s3:PutObject", "s3:GetBucketPolicy"]
    resources = [
      aws_s3_bucket.cur.arn,
      "${aws_s3_bucket.cur.arn}/*",
    ]
    principals {
      type        = "Service"
      identifiers = ["billingreports.amazonaws.com", "bcm-data-exports.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "StringLike"
      variable = "aws:SourceArn"
      values = [
        "arn:${data.aws_partition.current.partition}:cur:us-east-1:${local.account_id}:definition/*",
        "arn:${data.aws_partition.current.partition}:bcm-data-exports:us-east-1:${local.account_id}:export/*",
      ]
    }
  }
}

resource "aws_s3_bucket_policy" "cur" {
  bucket     = aws_s3_bucket.cur.id
  policy     = data.aws_iam_policy_document.cur_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.cur]
}

# ---------------------------------------------------------------------------------------------
# CUR 2.0 export
# ---------------------------------------------------------------------------------------------
resource "aws_bcmdataexports_export" "cur" {
  provider = aws.us_east_1

  export {
    name = var.export_name

    data_query {
      query_statement = "SELECT ${join(", ", local.cur_columns)} FROM COST_AND_USAGE_REPORT"
      table_configurations = {
        COST_AND_USAGE_REPORT = {
          BILLING_VIEW_ARN                      = "arn:${data.aws_partition.current.partition}:billing::${local.account_id}:billingview/primary"
          TIME_GRANULARITY                      = "HOURLY"
          INCLUDE_RESOURCES                     = "TRUE"
          INCLUDE_SPLIT_COST_ALLOCATION_DATA    = "TRUE"
          INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY = "FALSE"
        }
      }
    }

    destination_configurations {
      s3_destination {
        s3_bucket = aws_s3_bucket.cur.id
        s3_prefix = var.export_prefix
        s3_region = var.region
        s3_output_configurations {
          overwrite   = "CREATE_NEW_REPORT"
          format      = "PARQUET"
          compression = "PARQUET"
          output_type = "CUSTOM"
        }
      }
    }

    refresh_cadence {
      frequency = "SYNCHRONOUS"
    }
  }

  depends_on = [aws_s3_bucket_policy.cur]
}

# ---------------------------------------------------------------------------------------------
# Datadog AWS integration: retain CCM, collect only online namespaces.
# Logs, custom metrics, X-Ray and extended resource collection remain off.
# ---------------------------------------------------------------------------------------------
resource "datadog_integration_aws_account" "main" {
  aws_account_id = local.account_id
  aws_partition  = data.aws_partition.current.partition
  account_tags   = ["project:dd-demo"]

  auth_config {
    aws_auth_config_role {
      role_name = local.role_name
    }
  }
  aws_regions {
    include_only = [var.region]
  }
  metrics_config {
    automute_enabled          = false
    collect_cloudwatch_alarms = false
    collect_custom_metrics    = false
    enabled                   = true
    namespace_filters {
      include_only = ["AWS/ECS", "AWS/ElastiCache", "AWS/ApplicationELB"]
    }
  }
  logs_config {
    lambda_forwarder {}
  }
  resources_config {
    cloud_security_posture_management_collection = false
    extended_collection                          = false
  }
  traces_config {
    xray_services {}
  }
}

data "aws_iam_policy_document" "datadog_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${var.datadog_aws_account_id}:root"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [datadog_integration_aws_account.main.auth_config.aws_auth_config_role.external_id]
    }
  }
}

resource "aws_iam_role" "datadog" {
  name               = local.role_name
  description        = "Datadog CCM and ECS, ElastiCache and ALB metrics for dd-demo"
  assume_role_policy = data.aws_iam_policy_document.datadog_trust.json
}

# Datadog CCM policy (docs.datadoghq.com/cloud_cost_management/setup/aws/) plus the Data Exports reads of the
# integration policy. ce:Get* lets Datadog check accuracy against Cost Explorer (each API request costs $0.01).
data "aws_iam_policy_document" "datadog_ccm" {
  statement {
    sid       = "DDCloudCostReadBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.cur.arn]
  }
  statement {
    sid       = "DDCloudCostGetBill"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.cur.arn}/${var.export_prefix}/${var.export_name}/*"]
  }
  statement {
    sid       = "DDCloudCostCheckAccuracy"
    actions   = ["ce:Get*"]
    resources = ["*"]
  }
  statement {
    sid       = "DDCloudCostListCURs"
    actions   = ["cur:DescribeReportDefinitions", "bcm-data-exports:GetExport", "bcm-data-exports:ListExports"]
    resources = ["*"]
  }
  statement {
    sid       = "DDCloudCostListOrganizations"
    actions   = ["organizations:Describe*", "organizations:List*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "datadog_ccm" {
  name   = "datadog-ccm"
  role   = aws_iam_role.datadog.id
  policy = data.aws_iam_policy_document.datadog_ccm.json
}

# Read-only discovery/tagging and CloudWatch reads, separate from the unchanged CCM policy.
# Sources: docs.datadoghq.com/integrations/amazon_web_services/, amazon_elasticache/, amazon_elb/.
data "aws_iam_policy_document" "datadog_online_metrics" {
  statement {
    sid = "ReadOnlineMetrics"
    actions = [
      "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
      "tag:GetResources", "tag:GetTagKeys", "tag:GetTagValues",
      "ecs:ListClusters", "ecs:ListServices", "ecs:ListTasks", "ecs:ListContainerInstances",
      "ecs:DescribeClusters", "ecs:DescribeServices", "ecs:DescribeTasks", "ecs:DescribeContainerInstances",
      "ecs:ListTagsForResource",
      "elasticache:DescribeCacheClusters", "elasticache:DescribeReplicationGroups", "elasticache:ListTagsForResource",
      "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeTags",
      "ec2:DescribeInstances", "ec2:DescribeTags", "ec2:DescribeRegions",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "datadog_online_metrics" {
  name   = "datadog-online-metrics"
  role   = aws_iam_role.datadog.id
  policy = data.aws_iam_policy_document.datadog_online_metrics.json
}

resource "datadog_integration_aws_account_ccm_config" "main" {
  aws_account_config_id = datadog_integration_aws_account.main.id

  ccm_config {
    data_export_configs {
      report_name   = var.export_name
      report_prefix = var.export_prefix
      report_type   = "CUR2.0"
      bucket_name   = aws_s3_bucket.cur.id
      bucket_region = var.region
    }
  }

  depends_on = [aws_iam_role_policy.datadog_ccm, aws_bcmdataexports_export.cur]
}
