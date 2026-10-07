# Flink statements from overlay/flink/. The provider runs one SQL statement per resource,
# so each .sql file is split on the semicolon character. CREATE statements are applied first, the
# rest (INSERT ...) after them. Cost: statements run on the pool from main.tf and bill CFU hours.

locals {
  flink_dir = "${path.module}/../../flink"

  sellable_all = [for s in split(";", file("${local.flink_dir}/sellable.sql")) : trimspace(join("\n", [for l in split("\n", s) : l if !startswith(trimspace(l), "--")])) if length(trimspace(join("\n", [for l in split("\n", s) : l if !startswith(trimspace(l), "--")]))) > 0]

  offers_file = "${local.flink_dir}/cart_at_risk.sql"
  offers_all = var.enable_offers && fileexists(local.offers_file) ? [
    for s in split(";", file(local.offers_file)) : trimspace(join("\n", [for l in split("\n", s) : l if !startswith(trimspace(l), "--")]))
    if length(trimspace(join("\n", [for l in split("\n", s) : l if !startswith(trimspace(l), "--")]))) > 0
  ] : []

  # Layer restock: three files, each CREATE sink then INSERT, same split convention.
  # The files are required while the layer is on (file() fails loudly when one is missing).
  restock_files = var.enable_restock ? toset(["demand", "procurement", "restock"]) : toset([])
  restock_stmts = {
    for f in local.restock_files : f => [
      for s in split(";", file("${local.flink_dir}/${f}.sql")) : trimspace(join("\n", [for l in split("\n", s) : l if !startswith(trimspace(l), "--")]))
      if length(trimspace(join("\n", [for l in split("\n", s) : l if !startswith(trimspace(l), "--")]))) > 0
    ]
  }

  # var.flink_deferred holds files back (both statements) until their input topics carry a schema: Confluent Cloud
  # Flink infers a table from the topic's Schema Registry subjects, which the apps register on their first record.
  # overlay/compose/scripts/stack.sh applies with the files deferred, primes the inputs, then applies again with [].
  statement_files = { for f, list in merge(
    {
      sellable = local.sellable_all
      offers   = local.offers_all
    },
    local.restock_stmts,
  ) : f => list if !contains(var.flink_deferred, f) }

  # key "<file>-<index>" => SQL, split in DDL (CREATE ...) and the rest
  flink_ddl     = merge([for f, list in local.statement_files : { for i, s in list : "${f}-${i}" => s if startswith(upper(s), "CREATE") }]...)
  flink_dml_all = merge([for f, list in local.statement_files : { for i, s in list : "${f}-${i}" => s if !startswith(upper(s), "CREATE") } if !contains(var.flink_dml_deferred, f)]...)
  # restock joins outputs from demand/procurement. Offers reads stock.sellable, whose CREATE statement
  # can return before the table is queryable, so it also waits for the first DML wave.
  flink_dml_late = { for k, v in local.flink_dml_all : k => v if startswith(k, "restock-") || startswith(k, "offers-") }
  flink_dml      = { for k, v in local.flink_dml_all : k => v if !startswith(k, "restock-") && !startswith(k, "offers-") }

  flink_props = {
    "sql.current-catalog"  = confluent_environment.main.display_name
    "sql.current-database" = confluent_kafka_cluster.main.display_name
  }
}

# Registered on every stack (before any offers DDL). sa-flink's DeveloperWrite on carts.at-risk-* is therefore a core
# binding (main.tf), and the schema waits for the role bindings.
resource "confluent_schema" "carts_at_risk_value" {
  subject_name  = "carts.at-risk-value"
  format        = "AVRO"
  schema        = file("${path.module}/../../contracts/avro/flink_cart_at_risk_value.avsc")
  rest_endpoint = data.confluent_schema_registry_cluster.main.rest_endpoint
  schema_registry_cluster {
    id = data.confluent_schema_registry_cluster.main.id
  }
  credentials {
    key    = confluent_api_key.sr["flink"].id
    secret = confluent_api_key.sr["flink"].secret
  }

  depends_on = [confluent_role_binding.app]
}

resource "confluent_flink_statement" "ddl" {
  for_each = local.flink_ddl

  statement_name = "${local.name}-${each.key}"
  statement      = "${each.value};"
  properties     = local.flink_props
  rest_endpoint  = data.confluent_flink_region.main.rest_endpoint

  organization {
    id = data.confluent_organization.main.id
  }
  environment {
    id = confluent_environment.main.id
  }
  compute_pool {
    id = confluent_flink_compute_pool.main.id
  }
  principal {
    id = confluent_service_account.app["flink"].id
  }
  credentials {
    key    = confluent_api_key.flink.id
    secret = confluent_api_key.flink.secret
  }

  depends_on = [confluent_role_binding.app, confluent_kafka_acl.app, confluent_kafka_topic.main, confluent_schema.carts_at_risk_value]

  lifecycle {
    precondition {
      condition     = length(local.sellable_all) == 2 && contains([0, 2], length(local.offers_all)) && alltrue([for f, list in local.restock_stmts : length(list) == 2])
      error_message = "overlay/flink/sellable.sql and, while enable_restock is on, demand.sql, procurement.sql and restock.sql and, while enable_offers is on, cart_at_risk.sql must each hold exactly 2 statements (CREATE TABLE, INSERT); a semicolon inside a comment splits a statement. Found sellable ${length(local.sellable_all)}, offers ${length(local.offers_all)}, restock files ${jsonencode({ for f, list in local.restock_stmts : f => length(list) })}."
    }
  }
}

resource "confluent_flink_statement" "dml" {
  for_each = local.flink_dml

  statement_name = "${local.name}-${each.key}"
  statement      = "${each.value};"
  properties     = local.flink_props
  rest_endpoint  = data.confluent_flink_region.main.rest_endpoint

  organization {
    id = data.confluent_organization.main.id
  }
  environment {
    id = confluent_environment.main.id
  }
  compute_pool {
    id = confluent_flink_compute_pool.main.id
  }
  principal {
    id = confluent_service_account.app["flink"].id
  }
  credentials {
    key    = confluent_api_key.flink.id
    secret = confluent_api_key.flink.secret
  }

  depends_on = [confluent_flink_statement.ddl]
}

resource "confluent_flink_statement" "dml_late" {
  for_each = local.flink_dml_late

  statement_name = "${local.name}-${each.key}"
  statement      = "${each.value};"
  properties     = local.flink_props
  rest_endpoint  = data.confluent_flink_region.main.rest_endpoint

  organization {
    id = data.confluent_organization.main.id
  }
  environment {
    id = confluent_environment.main.id
  }
  compute_pool {
    id = confluent_flink_compute_pool.main.id
  }
  principal {
    id = confluent_service_account.app["flink"].id
  }
  credentials {
    key    = confluent_api_key.flink.id
    secret = confluent_api_key.flink.secret
  }

  depends_on = [confluent_flink_statement.ddl, confluent_flink_statement.dml]
}
