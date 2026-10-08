# Confluent Cloud for dd-demo (contracts section 2), with per-app least-privilege
# service accounts. No managed connector, RDS or Bedrock resource is created here.

data "confluent_organization" "main" {}

locals {
  name = "dd-demo-${var.stack}" # prefix of every display name
}

resource "confluent_environment" "main" {
  display_name = local.name

  stream_governance {
    package = "ESSENTIALS"
  }

  # Guard: the state in use must be the one of this stack (terraform workspace new <stack>).
  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "terraform.workspace is '${terraform.workspace}' but var.stack is '${var.stack}'. Select the stack's own workspace: terraform workspace select ${var.stack} (create it once with terraform workspace new ${var.stack}). This stops one stack's state being applied to another."
    }
  }
}

resource "confluent_kafka_cluster" "main" {
  display_name = local.name
  cloud        = "AWS"
  region       = var.region
  availability = var.kafka_availability

  basic {}

  environment {
    id = confluent_environment.main.id
  }
}

# Schema Registry: provider v2 has no SR cluster resource. Confluent provisions it with the first
# Kafka cluster of the environment, in that cluster's cloud/region (the Essentials package is set
# on the environment above), so var.region drives its region. Verified only by terraform validate:
# Confirm the Schema Registry endpoint region in the Confluent Console during the first apply.
data "confluent_schema_registry_cluster" "main" {
  environment {
    id = confluent_environment.main.id
  }

  depends_on = [confluent_kafka_cluster.main]
}

# ---------------------------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------------------------
locals {
  apps = merge({
    connect      = "Debezium / Kafka Connect worker on the demo VM"
    projector    = "stock-projector"
    storefront   = "storefront backend"
    offers       = "offer-worker"
    flink        = "Flink statements"
    demo-control = "demo-control service (control panel, writes demo.config)"
    }, var.enable_control_center ? {
    control-center = "Control Center (Legacy) on the hybrid VM"
  } : {})
}

resource "confluent_service_account" "app" {
  for_each     = local.apps
  display_name = "${local.name}-sa-${each.key}"
  description  = each.value
}

# Terraform's own data-plane identity: creates topics only. Not exported to any app.
resource "confluent_service_account" "tf_admin" {
  display_name = "${local.name}-sa-tf-admin"
  description  = "Terraform: manages topics on the dd-demo cluster"
}

resource "confluent_role_binding" "tf_admin_cluster" {
  principal   = "User:${confluent_service_account.tf_admin.id}"
  role_name   = "CloudClusterAdmin"
  crn_pattern = confluent_kafka_cluster.main.rbac_crn
}

resource "confluent_api_key" "tf_admin_kafka" {
  display_name = "${local.name}-tf-admin-kafka"
  description  = "Terraform topic management"

  owner {
    id          = confluent_service_account.tf_admin.id
    api_version = confluent_service_account.tf_admin.api_version
    kind        = confluent_service_account.tf_admin.kind
  }

  managed_resource {
    id          = confluent_kafka_cluster.main.id
    api_version = confluent_kafka_cluster.main.api_version
    kind        = confluent_kafka_cluster.main.kind

    environment {
      id = confluent_environment.main.id
    }
  }

  depends_on = [confluent_role_binding.tf_admin_cluster]
}

# ---------------------------------------------------------------------------------------------
# Topics (contracts section 2). One partition each. Replication is fixed by Confluent Cloud.
# Flink output topics (stock.sellable, carts.at-risk, restock.requests, stock.demand, restock.forecast) are NOT
# created here: CREATE TABLE in overlay/flink creates the topic and its key/value subjects. A topic created here
# without a schema shows up in Flink as a raw (key BYTES, val BYTES) table and CREATE TABLE IF NOT EXISTS skips it.
# ---------------------------------------------------------------------------------------------
locals {
  day_ms = 86400000

  topics = {
    "inventory.cdc"            = { cleanup = "delete", retention_ms = tostring(7 * local.day_ms) }
    "inventory.state"          = { cleanup = "compact", retention_ms = "-1" } # compact, never delete
    "carts.events"             = { cleanup = "delete", retention_ms = tostring(1 * local.day_ms) }
    "stock.movements"          = { cleanup = "delete", retention_ms = tostring(1 * local.day_ms) } # contracts section 13: the projector always publishes (core)
    "demo.config"              = { cleanup = "compact", retention_ms = "-1" }                      # contracts section 12: demo-control is core
    "_connect.dd-demo.configs" = { cleanup = "compact", retention_ms = "-1" }
    "_connect.dd-demo.offsets" = { cleanup = "compact", retention_ms = "-1" }
    "_connect.dd-demo.status"  = { cleanup = "compact", retention_ms = "-1" }
  }

  # Layer topics: present only when their layer is on.
  topics_offers = {
    "offers" = { cleanup = "compact", retention_ms = "-1" } # contract lists "compact" only; never-delete assumed
  }
  topics_restock = {
    "procurement.orders" = { cleanup = "delete", retention_ms = tostring(7 * local.day_ms) } # Debezium connector procurement-orders
  }
  topics_control_center = {
    "_confluent-metrics"    = { cleanup = "delete", retention_ms = tostring(3 * local.day_ms) }
    "_confluent-monitoring" = { cleanup = "delete", retention_ms = tostring(3 * local.day_ms) }
    "_confluent-command"    = { cleanup = "delete", retention_ms = tostring(1 * local.day_ms) }
  }

  all_topics = merge(
    local.topics,
    var.enable_offers ? local.topics_offers : {},
    var.enable_restock ? local.topics_restock : {},
    var.enable_control_center ? local.topics_control_center : {},
  )

  # Layer of each topic, for Stream Catalog tags.
  topic_layer = { for t in keys(local.all_topics) : t => contains(keys(local.topics_offers), t) ? "offers" : contains(keys(local.topics_restock), t) ? "restock" : "core" }
}

resource "confluent_kafka_topic" "main" {
  for_each = local.all_topics

  topic_name       = each.key
  partitions_count = 1
  rest_endpoint    = confluent_kafka_cluster.main.rest_endpoint

  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }

  credentials {
    key    = confluent_api_key.tf_admin_kafka.id
    secret = confluent_api_key.tf_admin_kafka.secret
  }

  config = {
    "cleanup.policy" = each.value.cleanup
    "retention.ms"   = each.value.retention_ms
  }
}

# ---------------------------------------------------------------------------------------------
# Role bindings, least privilege per app
# ---------------------------------------------------------------------------------------------
locals {
  cluster_crn = confluent_kafka_cluster.main.rbac_crn
  kafka_crn   = "${confluent_kafka_cluster.main.rbac_crn}/kafka=${confluent_kafka_cluster.main.id}"
  sr_crn      = data.confluent_schema_registry_cluster.main.resource_name

  # key => { sa, role, crn }. Keys are static so for_each is known at plan time.
  bindings = {
    # sa-connect: write inventory.cdc, own internal topics (read+write), its group, SR subjects inventory.cdc-*
    connect-write-cdc      = { sa = "connect", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=inventory.cdc" }
    connect-internal-read  = { sa = "connect", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=_connect.dd-demo.*" }
    connect-internal-write = { sa = "connect", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=_connect.dd-demo.*" }
    connect-group-read     = { sa = "connect", role = "DeveloperRead", crn = "${local.kafka_crn}/group=${var.connect_group_id}" }
    connect-sr-read        = { sa = "connect", role = "DeveloperRead", crn = "${local.sr_crn}/subject=inventory.cdc-*" }
    connect-sr-write       = { sa = "connect", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=inventory.cdc-*" }
    # sa-connect, Redis sink connector `sellable-redis`: read stock.sellable; sink consumer group is connect-<connector name>
    connect-read-sellable    = { sa = "connect", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=stock.sellable" }
    connect-group-sellable   = { sa = "connect", role = "DeveloperRead", crn = "${local.kafka_crn}/group=${var.sellable_sink_group_id}" }
    connect-sr-read-sellable = { sa = "connect", role = "DeveloperRead", crn = "${local.sr_crn}/subject=stock.sellable-*" }
    # sa-projector: read inventory.cdc (group stock-projector), write inventory.state, SR
    projector-read-cdc       = { sa = "projector", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=inventory.cdc" }
    projector-group-read     = { sa = "projector", role = "DeveloperRead", crn = "${local.kafka_crn}/group=${var.projector_group_id}" }
    projector-write-state    = { sa = "projector", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=inventory.state" }
    projector-sr-read-cdc    = { sa = "projector", role = "DeveloperRead", crn = "${local.sr_crn}/subject=inventory.cdc-*" }
    projector-sr-read-state  = { sa = "projector", role = "DeveloperRead", crn = "${local.sr_crn}/subject=inventory.state-*" }
    projector-sr-write-state = { sa = "projector", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=inventory.state-*" }
    # sa-projector: stock.movements (contracts section 13), always published
    projector-write-movements    = { sa = "projector", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=stock.movements" }
    projector-sr-write-movements = { sa = "projector", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=stock.movements-*" }
    # sa-demo-control: read+write demo.config, its consumer group (prefix demo-control), SR subjects
    demo-control-read-config     = { sa = "demo-control", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=demo.config" }
    demo-control-write-config    = { sa = "demo-control", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=demo.config" }
    demo-control-group-read      = { sa = "demo-control", role = "DeveloperRead", crn = "${local.kafka_crn}/group=demo-control*" }
    demo-control-sr-read-config  = { sa = "demo-control", role = "DeveloperRead", crn = "${local.sr_crn}/subject=demo.config-*" }
    demo-control-sr-write-config = { sa = "demo-control", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=demo.config-*" }
    # sa-storefront: write carts.events, read offers, SR
    storefront-write-carts    = { sa = "storefront", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=carts.events" }
    storefront-group-read     = { sa = "storefront", role = "DeveloperRead", crn = "${local.kafka_crn}/group=storefront-*" }
    storefront-sr-read-carts  = { sa = "storefront", role = "DeveloperRead", crn = "${local.sr_crn}/subject=carts.events-*" }
    storefront-sr-write-carts = { sa = "storefront", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=carts.events-*" }
    # sa-flink: statement principal (reads inventory.state + carts.events, writes carts.at-risk)
    flink-developer = { sa = "flink", role = "FlinkDeveloper", crn = confluent_environment.main.resource_name }
    # Basic clusters reject topic-scoped roles (we use ACLs), but CC Flink resolves the cluster as a database
    # only for principals with a cluster role; ACLs are not enough. Scoped to this demo cluster only.
    flink-cluster-admin  = { sa = "flink", role = "CloudClusterAdmin", crn = local.cluster_crn }
    flink-read-state     = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=inventory.state" }
    flink-write-sellable = { sa = "flink", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=stock.sellable" }
    # CREATE TABLE over an existing topic: verify that DeveloperManage is sufficient during the first apply.
    flink-manage-sellable   = { sa = "flink", role = "DeveloperManage", crn = "${local.kafka_crn}/topic=stock.sellable" }
    flink-sr-write-sellable = { sa = "flink", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=stock.sellable-*" }
    # carts.at-risk-key/-value are registered by the offers CREATE TABLE (sa-flink is the statement principal).
    flink-sr-write-at-risk = { sa = "flink", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=carts.at-risk-*" }
    flink-sr-read          = { sa = "flink", role = "DeveloperRead", crn = "${local.sr_crn}/subject=*" }
    flink-assigner         = { sa = "flink", role = "Assigner", crn = "${data.confluent_organization.main.resource_name}/service-account=${confluent_service_account.app["flink"].id}" }
  }
}

locals {
  bindings_offers = {
    storefront-read-offers    = { sa = "storefront", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=offers" }
    storefront-sr-read-offers = { sa = "storefront", role = "DeveloperRead", crn = "${local.sr_crn}/subject=offers-*" }
    # sa-offers: read carts.at-risk, write offers, SR
    offers-read-at-risk    = { sa = "offers", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=carts.at-risk" }
    offers-write-offers    = { sa = "offers", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=offers" }
    offers-group-read      = { sa = "offers", role = "DeveloperRead", crn = "${local.kafka_crn}/group=offer-worker*" }
    offers-sr-read-at-risk = { sa = "offers", role = "DeveloperRead", crn = "${local.sr_crn}/subject=carts.at-risk-*" }
    offers-sr-read-offers  = { sa = "offers", role = "DeveloperRead", crn = "${local.sr_crn}/subject=offers-*" }
    offers-sr-write-offers = { sa = "offers", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=offers-*" }
    # sa-flink statements of cart_at_risk.sql
    flink-read-carts     = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=carts.events" }
    flink-manage-at-risk = { sa = "flink", role = "DeveloperManage", crn = "${local.kafka_crn}/topic=carts.at-risk" }
    flink-write-at-risk  = { sa = "flink", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=carts.at-risk" }
  }

  # Layer restock (contracts section 10): Flink writes restock.requests; the JDBC sink connector
  # `restock-procurement` (sa-connect) reads it. Verify these role/CRN conventions during the first apply.
  bindings_restock = {
    connect-read-restock    = { sa = "connect", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=restock.requests" }
    connect-group-restock   = { sa = "connect", role = "DeveloperRead", crn = "${local.kafka_crn}/group=${var.restock_sink_group_id}" }
    connect-sr-read-restock = { sa = "connect", role = "DeveloperRead", crn = "${local.sr_crn}/subject=restock.requests-*" }
    flink-write-restock     = { sa = "flink", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=restock.requests" }
    flink-manage-restock    = { sa = "flink", role = "DeveloperManage", crn = "${local.kafka_crn}/topic=restock.requests" }
    flink-sr-write-restock  = { sa = "flink", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=restock.requests-*" }
    # Debezium connector `procurement-orders` (sa-connect) writes procurement.orders and its subjects (RegexRouter + default TopicNameStrategy: subjects procurement.orders-key/-value)
    connect-write-orders    = { sa = "connect", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=procurement.orders" }
    connect-sr-read-orders  = { sa = "connect", role = "DeveloperRead", crn = "${local.sr_crn}/subject=procurement.orders-*" }
    connect-sr-write-orders = { sa = "connect", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=procurement.orders-*" }
    # sa-flink, statements of demand.sql / procurement.sql / restock.sql (overlay/flink/README.md): read inputs, write the two new sinks
    flink-read-movements    = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=stock.movements" }
    flink-read-orders       = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=procurement.orders" }
    flink-read-demand       = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=stock.demand" }
    flink-read-forecast     = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=restock.forecast" }
    flink-read-config       = { sa = "flink", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=demo.config" }
    flink-write-demand      = { sa = "flink", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=stock.demand" }
    flink-write-forecast    = { sa = "flink", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=restock.forecast" }
    flink-manage-demand     = { sa = "flink", role = "DeveloperManage", crn = "${local.kafka_crn}/topic=stock.demand" }
    flink-manage-forecast   = { sa = "flink", role = "DeveloperManage", crn = "${local.kafka_crn}/topic=restock.forecast" }
    flink-sr-write-demand   = { sa = "flink", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=stock.demand-*" }
    flink-sr-write-forecast = { sa = "flink", role = "DeveloperWrite", crn = "${local.sr_crn}/subject=restock.forecast-*" }
  }
  bindings_control_center = {
    control-center-read-topics     = { sa = "control-center", role = "DeveloperRead", crn = "${local.kafka_crn}/topic=*" }
    control-center-read-groups     = { sa = "control-center", role = "DeveloperRead", crn = "${local.kafka_crn}/group=*" }
    control-center-internal-manage = { sa = "control-center", role = "DeveloperManage", crn = "${local.kafka_crn}/topic=_confluent*" }
    # C3 persists its cluster registry in _confluent-command; without WRITE the cluster list stays empty ("No clusters
    # found") and, because the UI pairs Schema Registry with a listed Kafka cluster, messages are not Avro-decoded.
    control-center-internal-write = { sa = "control-center", role = "DeveloperWrite", crn = "${local.kafka_crn}/topic=_confluent*" }
    control-center-sr-read        = { sa = "control-center", role = "DeveloperRead", crn = "${local.sr_crn}/subject=*" }
  }

  all_bindings = merge(
    local.bindings,
    var.enable_offers ? local.bindings_offers : {},
    var.enable_restock ? local.bindings_restock : {},
    var.enable_control_center ? local.bindings_control_center : {},
  )
}

# Basic clusters reject resource-level RBAC roles on Kafka topics and groups ("Basic Clusters can not use resource
# roles", first apply 2026-10-03). Kafka-scoped entries above become ACLs with the same scope; Schema Registry,
# environment and organization entries stay role bindings (those were accepted).
locals {
  kafka_ops = {
    "DeveloperRead:topic"   = ["READ", "DESCRIBE", "DESCRIBE_CONFIGS"]
    "DeveloperWrite:topic"  = ["WRITE", "DESCRIBE", "DESCRIBE_CONFIGS"]
    "DeveloperManage:topic" = ["CREATE", "ALTER", "ALTER_CONFIGS", "DESCRIBE", "DESCRIBE_CONFIGS"]
    "DeveloperRead:group"   = ["READ", "DESCRIBE"]
  }
  # Classify by static binding keys, not computed CRNs. On a new stack the cluster/SR
  # CRNs are unknown during planning; filtering on them makes for_each keys unknown.
  role_binding_keys = toset(concat(
    [for k in keys(local.all_bindings) : k if strcontains(k, "-sr-")],
    ["flink-developer", "flink-cluster-admin", "flink-assigner"],
  ))
  role_bindings  = { for k, v in local.all_bindings : k => v if contains(local.role_binding_keys, k) }
  kafka_bindings = { for k, v in local.all_bindings : k => v if !contains(local.role_binding_keys, k) }
  # The Control Center Cloud guide requires cluster-level DESCRIBE and DESCRIBE_CONFIGS.
  # These are explicit ACLs instead of a broad cluster RBAC role.
  explicit_control_center_cluster_acls = var.enable_control_center ? [
    {
      sa      = "control-center"
      type    = "CLUSTER"
      name    = "kafka-cluster"
      pattern = "LITERAL"
      op      = "DESCRIBE"
    },
    {
      sa      = "control-center"
      type    = "CLUSTER"
      name    = "kafka-cluster"
      pattern = "LITERAL"
      op      = "DESCRIBE_CONFIGS"
    },
    # Its internal producers are idempotent (Kafka client default), which needs IdempotentWrite on
    # the cluster; without it every send fails with ClusterAuthorizationException and C3 never turns healthy.
    {
      sa      = "control-center"
      type    = "CLUSTER"
      name    = "kafka-cluster"
      pattern = "LITERAL"
      op      = "IDEMPOTENT_WRITE"
    },
  ] : []
  acl_list = concat(
    flatten([
      for k, v in local.kafka_bindings : [
        for op in local.kafka_ops["${v.role}:${split("=", split("/", trimprefix(v.crn, "${local.kafka_crn}/"))[0])[0]}"] : {
          sa   = v.sa
          type = upper(split("=", trimprefix(v.crn, "${local.kafka_crn}/"))[0])
          # "topic=*" means every resource: Kafka wants LITERAL "*", not PREFIXED "" (rejected with 400).
          name    = split("=", trimprefix(v.crn, "${local.kafka_crn}/"))[1] == "*" ? "*" : trimsuffix(split("=", trimprefix(v.crn, "${local.kafka_crn}/"))[1], "*")
          pattern = split("=", trimprefix(v.crn, "${local.kafka_crn}/"))[1] == "*" ? "LITERAL" : (endswith(v.crn, "*") ? "PREFIXED" : "LITERAL")
          op      = op
        }
      ]
    ]),
    local.explicit_control_center_cluster_acls,
  )
  # Several entries can grant the same operation (e.g. DESCRIBE from read and write): one ACL each.
  acls = { for a in local.acl_list : "${a.sa}|${a.type}|${a.pattern}|${a.name}|${a.op}" => a... }
}

resource "confluent_kafka_acl" "app" {
  for_each = local.acls

  kafka_cluster {
    id = confluent_kafka_cluster.main.id
  }
  resource_type = each.value[0].type
  resource_name = each.value[0].name
  pattern_type  = each.value[0].pattern
  principal     = "User:${confluent_service_account.app[each.value[0].sa].id}"
  host          = "*"
  operation     = each.value[0].op
  permission    = "ALLOW"
  rest_endpoint = confluent_kafka_cluster.main.rest_endpoint
  credentials {
    key    = confluent_api_key.tf_admin_kafka.id
    secret = confluent_api_key.tf_admin_kafka.secret
  }
}

resource "confluent_role_binding" "app" {
  for_each = local.role_bindings

  principal   = "User:${confluent_service_account.app[each.value.sa].id}"
  role_name   = each.value.role
  crn_pattern = each.value.crn
}

# ---------------------------------------------------------------------------------------------
# API keys: one Kafka key and one Schema Registry key per app service account
# ---------------------------------------------------------------------------------------------
resource "confluent_api_key" "kafka" {
  for_each = local.apps

  display_name = "${local.name}-${each.key}-kafka"
  description  = "Kafka API key for sa-${each.key}"

  owner {
    id          = confluent_service_account.app[each.key].id
    api_version = confluent_service_account.app[each.key].api_version
    kind        = confluent_service_account.app[each.key].kind
  }

  managed_resource {
    id          = confluent_kafka_cluster.main.id
    api_version = confluent_kafka_cluster.main.api_version
    kind        = confluent_kafka_cluster.main.kind

    environment {
      id = confluent_environment.main.id
    }
  }

  depends_on = [confluent_role_binding.app, confluent_kafka_acl.app]
}

resource "confluent_api_key" "sr" {
  for_each = local.apps

  display_name = "${local.name}-${each.key}-sr"
  description  = "Schema Registry API key for sa-${each.key}"

  owner {
    id          = confluent_service_account.app[each.key].id
    api_version = confluent_service_account.app[each.key].api_version
    kind        = confluent_service_account.app[each.key].kind
  }

  managed_resource {
    id          = data.confluent_schema_registry_cluster.main.id
    api_version = data.confluent_schema_registry_cluster.main.api_version
    kind        = data.confluent_schema_registry_cluster.main.kind

    environment {
      id = confluent_environment.main.id
    }
  }

  depends_on = [confluent_role_binding.app, confluent_kafka_acl.app]
}

# ---------------------------------------------------------------------------------------------
# Flink: compute pool, API key and the statements from overlay/flink/ (see flink_statements.tf).
# ---------------------------------------------------------------------------------------------
resource "confluent_flink_compute_pool" "main" {
  display_name = local.name
  cloud        = "AWS"
  region       = var.region
  max_cfu      = var.flink_max_cfu

  environment {
    id = confluent_environment.main.id
  }
}

data "confluent_flink_region" "main" {
  cloud  = "AWS"
  region = var.region
}

resource "confluent_api_key" "flink" {
  display_name = "${local.name}-flink"
  description  = "Flink API key for sa-flink (used by the statements in overlay/flink/)"

  owner {
    id          = confluent_service_account.app["flink"].id
    api_version = confluent_service_account.app["flink"].api_version
    kind        = confluent_service_account.app["flink"].kind
  }

  managed_resource {
    id          = data.confluent_flink_region.main.id
    api_version = data.confluent_flink_region.main.api_version
    kind        = data.confluent_flink_region.main.kind

    environment {
      id = confluent_environment.main.id
    }
  }

  depends_on = [confluent_role_binding.app, confluent_kafka_acl.app]
}


# ---------------------------------------------------------------------------------------------
# Layer dd-streams: read-only identity for the Datadog Confluent Cloud integration. The key and
# secret are exposed as sensitive outputs (datadog_confluent_api_key / _secret); the datadog dir
# takes them as variables. Never hardcode them.
# ---------------------------------------------------------------------------------------------
resource "confluent_service_account" "datadog" {
  count        = var.enable_dd_streams ? 1 : 0
  display_name = "${local.name}-sa-datadog-metrics"
  description  = "Read-only: Datadog Confluent Cloud integration (Metrics API)"
}

resource "confluent_role_binding" "datadog_metrics_viewer" {
  count       = var.enable_dd_streams ? 1 : 0
  principal   = "User:${confluent_service_account.datadog[0].id}"
  role_name   = "MetricsViewer"
  crn_pattern = data.confluent_organization.main.resource_name
}

# Cloud resource management key: no managed_resource block.
resource "confluent_api_key" "datadog" {
  count        = var.enable_dd_streams ? 1 : 0
  display_name = "${local.name}-datadog-metrics"
  description  = "Cloud resource management key for the Datadog integration"

  owner {
    id          = confluent_service_account.datadog[0].id
    api_version = confluent_service_account.datadog[0].api_version
    kind        = confluent_service_account.datadog[0].kind
  }

  depends_on = [confluent_role_binding.datadog_metrics_viewer]
}

# ---------------------------------------------------------------------------------------------
# Stream Catalog tags: project / stack / layer on every topic. The README lists the account checks required before enabling them:
# availability on the ESSENTIALS package, the role needed to create tags, and the entity_name format.
# Off by default (var.enable_catalog_tags). Schemas are not tagged: they are registered by the
# apps at runtime, not by Terraform; their subjects carry the topic name.
# ---------------------------------------------------------------------------------------------
locals {
  catalog_tag_names = var.enable_catalog_tags ? toset(concat(
    ["dd-demo", "stack-${var.stack}"],
    [for l in distinct(values(local.topic_layer)) : "layer-${l}"],
  )) : toset([])

  # topic => list of tag names
  topic_tag_pairs = var.enable_catalog_tags ? merge([
    for t, layer in local.topic_layer : {
      for n in ["dd-demo", "stack-${var.stack}", "layer-${layer}"] : "${t}|${n}" => { topic = t, tag = n }
    }
  ]...) : {}
}

resource "confluent_role_binding" "tf_admin_catalog" {
  count       = var.enable_catalog_tags ? 1 : 0
  principal   = "User:${confluent_service_account.tf_admin.id}"
  role_name   = "DataSteward" # Confirm that this role can create and bind Stream Catalog tags before enabling the flag.
  crn_pattern = confluent_environment.main.resource_name
}

resource "confluent_api_key" "tf_admin_sr" {
  count        = var.enable_catalog_tags ? 1 : 0
  display_name = "${local.name}-tf-admin-sr"
  description  = "Terraform: Stream Catalog tags"

  owner {
    id          = confluent_service_account.tf_admin.id
    api_version = confluent_service_account.tf_admin.api_version
    kind        = confluent_service_account.tf_admin.kind
  }

  managed_resource {
    id          = data.confluent_schema_registry_cluster.main.id
    api_version = data.confluent_schema_registry_cluster.main.api_version
    kind        = data.confluent_schema_registry_cluster.main.kind

    environment {
      id = confluent_environment.main.id
    }
  }

  depends_on = [confluent_role_binding.tf_admin_catalog]
}

resource "confluent_tag" "main" {
  for_each = local.catalog_tag_names

  name          = each.key
  description   = "dd-demo stack ${var.stack}"
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint

  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }

  credentials {
    key    = confluent_api_key.tf_admin_sr[0].id
    secret = confluent_api_key.tf_admin_sr[0].secret
  }
}

resource "confluent_tag_binding" "topic" {
  for_each = local.topic_tag_pairs

  tag_name      = confluent_tag.main[each.value.tag].name
  entity_name   = "${data.confluent_schema_registry_cluster.main.id}:${confluent_kafka_cluster.main.id}:${confluent_kafka_topic.main[each.value.topic].topic_name}"
  entity_type   = "kafka_topic"
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint

  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }

  credentials {
    key    = confluent_api_key.tf_admin_sr[0].id
    secret = confluent_api_key.tf_admin_sr[0].secret
  }
}
