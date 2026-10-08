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

  # The offers path (sellable.sql: non-windowed GROUP BY, cart_at_risk.sql: regular join and keep-last
  # deduplication) has no event-time operator, so its INSERT statements run with watermark alignment off.
  # Confluent: setting the drift to 0 "can prevent performance bottlenecks and latency for queries that don't
  # require event-time semantics, like regular joins, non-windowed aggregations, and ETL".
  # https://docs.confluent.io/cloud/current/flink/reference/statements/set.html
  # Never add a file with windows, interval or temporal joins here (restock's HOP windows need alignment).
  # Only the INSERT statements get it: the CREATE TABLE statements, and so the topics, are unchanged.
  no_alignment_files = ["sellable", "offers"]
  flink_props_no_alignment = merge(local.flink_props, {
    "sql.tables.scan.watermark-alignment.max-allowed-drift" = "0 ms"
  })
  dml_props = { for k in keys(local.flink_dml_all) : k => contains(local.no_alignment_files, split("-", k)[0]) ? local.flink_props_no_alignment : local.flink_props }
}

# The provider cannot update a running statement in place: its update accepts only `stopped`, `properties_sensitive`,
# `principal`, `compute_pool` and `credentials`, and fails on any other change ("stopped" or "properties_sensitive"
# attribute must be updated). Confluent Cloud treats a statement's code as immutable.
# https://registry.terraform.io/providers/confluentinc/confluent/latest/docs/resources/confluent_flink_statement
# https://docs.confluent.io/cloud/current/flink/operate-and-deploy/flink-rest-api.html
# A `statement` change already forces a replacement (ForceNew); a `properties` change does not. This revision
# holds a hash of each INSERT statement's SQL and properties, and every DML statement is replaced when its
# revision changes (replace_triggered_by): delete, then create under the same name, which Confluent allows.
# The new statement starts from its configured startup offset and rebuilds its state.
# Creating a revision never triggers a replacement (Terraform replaces only on an update of the referenced
# instance), so a statement applied before this revision existed needs one plan with -replace=<address> when its
# properties change (overlay/compose/scripts/stack.sh: TF_REPLACE).
resource "terraform_data" "flink_dml_revision" {
  for_each = local.flink_dml_all

  input = sha256(jsonencode({ statement = "${each.value};", properties = local.dml_props[each.key] }))
}

# No schema is pre-registered for the Flink output tables: each CREATE TABLE registers its own <table>-key and
# <table>-value subjects on a fresh stack, so stack.sh's readiness poll on those subjects means "table created".
# https://docs.confluent.io/cloud/current/flink/reference/statements/create-table.html
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

  depends_on = [confluent_role_binding.app, confluent_kafka_acl.app, confluent_kafka_topic.main]

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
  properties     = local.dml_props[each.key]
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

  lifecycle {
    replace_triggered_by = [terraform_data.flink_dml_revision[each.key]]
  }
}

resource "confluent_flink_statement" "dml_late" {
  for_each = local.flink_dml_late

  statement_name = "${local.name}-${each.key}"
  statement      = "${each.value};"
  properties     = local.dml_props[each.key]
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

  lifecycle {
    replace_triggered_by = [terraform_data.flink_dml_revision[each.key]]
  }
}
