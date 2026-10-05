# terraform/cloud

Confluent Cloud infrastructure for the demo, implementing the topics and identities in [`contracts/README.md`](../../contracts/README.md) section 2. It uses no RDS, managed connector or Bedrock resource.

## Creates

- Environment `dd-demo-<stack>` with Stream Governance package ESSENTIALS. Schema Registry is provisioned by Confluent with the first cluster, in that cluster's region; confirm the endpoint region during the first apply because provider v2 has no SR resource.
- Basic Kafka cluster, AWS, `eu-west-1` (var), SINGLE_ZONE.
- Service accounts `sa-connect`, `sa-projector`, `sa-storefront`, `sa-offers`, `sa-flink`, each with its own Kafka and Schema Registry API key and least-privilege role bindings (topic, consumer group and subject patterns, see `local.bindings`). Plus `sa-tf-admin` (CloudClusterAdmin on the cluster, only used to create topics; its key is not exported).
- Topics exactly as contract section 2 (1 partition) plus `_connect.dd-demo.configs|offsets|status` (compact). Replication is fixed by Confluent Cloud. `offers` retention: the contract says only "compact", so never-delete is assumed.
- Topic `stock.sellable` (compact, 1 partition). `sa-connect` can read it with group `connect-sellable-redis` and its SR subject (Redis sink) next to its Debezium rights (`inventory.cdc`, subjects `inventory.cdc-*` cover all five connectors).
- Flink compute pool `dd-demo` with `max_cfu` (default 5, the smallest accepted value) and an `sa-flink` Flink API key. `flink_statements.tf` runs the statements of `overlay/flink/sellable.sql` (always) and `overlay/flink/cart_at_risk.sql` (when `enable_offers` is true, default, and the file exists). One resource per statement, split on the semicolon, CREATE statements first. Statements bill CFU hours while running. `sa-offers` keys are in `env_file` as `OFFERS_*`.
- Layer `dd-streams` (`enable_dd_streams`, default true): service account `dd-demo-<stack>-sa-datadog-metrics` with MetricsViewer on the organization and a Cloud resource management API key for the Datadog Confluent Cloud integration, exposed as the sensitive outputs `datadog_confluent_api_key` and `datadog_confluent_api_secret` (the datadog dir consumes them, see its README).

## Items to price before apply

- Basic cluster hourly (per `confluent billing price list --cloud aws --region eu-west-1`) plus storage and throughput.
- Schema Registry Essentials allowance and charges.
- Flink CFU-minutes (pool is capped by `flink_max_cfu`; idle pools may still bill: verify).
- Remaining trial credit and its 30-day clock (`confluent billing cost list`).

## Commands for the human

Credentials from the environment only (`CONFLUENT_CLOUD_API_KEY`, `CONFLUENT_CLOUD_API_SECRET`, from the untracked `.env`). Do not paste values into chat.

```sh
cd terraform/cloud
terraform init
terraform workspace new <stack>      # once per stack; later: terraform workspace select <stack>
terraform plan -var stack=<stack> -out cloud.tfplan     # needs live credentials; review it
terraform apply cloud.tfplan         # explicit approval + cost note first
terraform output -raw env_file > ../../../.env.cloud   # sensitive; gitignore-check it, never print or commit
chmod 600 ../../../.env.cloud
```

Destroy at the end of every session: `terraform destroy`. The state holds all API secrets (it is gitignored); keep it local and delete it with the environment.

## Notes

- `env_file` uses per-app variable names (`CONNECT_KAFKA_API_KEY`, `PROJECTOR_SR_API_SECRET`, ...) because each app has its own identity. Contract section 8 names single `KAFKA_API_KEY`/`SR_*` variables: the compose files must map the per-app names onto them (contract to be updated).
- Role names and CRN patterns (DeveloperRead/DeveloperWrite on topic, group and subject; FlinkDeveloper and Assigner for `sa-flink`; prefix patterns with `*`) follow Confluent RBAC docs but need a first apply to confirm. Expect to adjust them if the account requires extra group or transactional-id bindings for a client.
- Only `terraform init -backend=false`, `fmt` and `validate` were run.
- `DeveloperManage` for `sa-flink` on `stock.sellable` and `carts.at-risk` is assumed necessary for CREATE TABLE over an existing topic; confirm it during the first apply.
- Run so far: `fmt`, `init -backend=false`, `validate` (and `terraform console` on the statement split). Nothing applied.

## Stacks, layers and tags

- `stack` (required, `^[a-z][a-z0-9-]{1,15}$`): every display name is `dd-demo-<stack>-...` (environment, cluster, compute pool, service accounts, API keys, Flink statements). One Terraform workspace per stack, named like the stack: `terraform workspace new live`. A precondition on the environment fails the plan when `terraform.workspace != var.stack`, so a state cannot be applied to another stack.
- Layer toggles (core is always on): `enable_restock` (default true): topic `restock.requests` (compact), Flink statements of `overlay/flink/restock.sql` (skipped while the file does not exist, same semicolon split), Connect read on the topic, group `connect-restock-procurement` (var `restock_sink_group_id`) and subject `restock.requests-*`, plus `sa-flink` write/manage/subject. `enable_offers` (default true): topics `offers` and `carts.at-risk`, the `sa-offers` and storefront-offers bindings and `cart_at_risk.sql`. `enable_dd_streams` (default true): the MetricsViewer identity above. The `releases`, `dd-synthetics` and `dd-rum` layers create nothing on Confluent. Example: `terraform apply -var stack=layers -var enable_restock=false -var enable_offers=false`.
- Tags: Confluent Cloud resources carry no tags by naming only. Optional Stream Catalog tags (`enable_catalog_tags`, default false): `confluent_tag` `dd-demo`, `stack-<stack>`, `layer-<layer>` bound with `confluent_tag_binding` (entity_type `kafka_topic`, entity_name `<sr id>:<cluster id>:<topic>`) to each topic, using an SR API key of `sa-tf-admin` and role DataSteward on the environment. Before enabling the flag, confirm that Stream Catalog tags work on the ESSENTIALS package, that DataSteward is the right role and scope, the tag name charset (dashes), and the entity_name format. Schemas are not tagged (registered by the apps at runtime). If the first apply with the flag fails, leave it off: the naming prefix is the grouping.
- `env_file` adds `STACK` and `RESTOCK_SINK_GROUP_ID`. The earlier variables `environment_name`, `cluster_name` and `enable_datadog_integration_key` are gone (replaced by `stack` and `enable_dd_streams`).
- Run: `fmt`, `init -backend=false`, `validate` only.

## Demand-driven restock and demo control (contracts sections 12-13b)

- New topics. Core: `stock.movements` (delete, 1 day; the projector always publishes), `demo.config` (compact; demo-control is core). Layer restock: `stock.demand` (compact), `restock.forecast` (compact), `procurement.orders` (delete, 7 days). `restock.requests` unchanged.
- New identity `sa-demo-control` (core): read+write `demo.config`, group prefix `demo-control*`, subjects `demo.config-*`. `env_file` gains `DEMO_CONTROL_KAFKA_API_KEY/SECRET` and `DEMO_CONTROL_SR_API_KEY/SECRET`. `sellable-dev` has no cloud identity.
- `sa-projector`: also writes `stock.movements` and its subjects. `sa-connect` (restock layer): also writes `procurement.orders` and read/write subjects `procurement.orders-*` (Debezium connector `procurement-orders`).
- `sa-flink` (restock layer): read `stock.movements`, `procurement.orders`, `stock.demand`, `restock.forecast`, `demo.config` (plus the existing `inventory.state`); write `stock.demand`, `restock.forecast`, `restock.requests`; subjects `stock.demand-*`, `restock.forecast-*`, `restock.requests-*` writable (read on `*` already existed). `DeveloperManage` on the two new sink topics follows the existing sink convention; confirm it during the first apply.
- Flink statements: `demand.sql`, `procurement.sql`, `restock.sql` (2 statements each, a precondition checks it). All CREATE statements run first (`ddl`), then the INSERTs of sellable, offers, demand and procurement (`dml`), then the restock INSERT (`dml_late`, `depends_on` ddl and dml). The restock INSERT address is `confluent_flink_statement.dml_late["restock-1"]`; it used to be in `dml` (a state move if an older stack was applied). The restock files are required while `enable_restock` is on.
- Before the first apply, confirm that TopicRecordNameStrategy subjects start with the topic name (the `procurement.orders-*` pattern relies on it) and that the role set permits Flink to read the Debezium topics.
- Run: `fmt`, `init -backend=false`, `validate`, and `terraform console` on the statement split (ddl: demand-0, procurement-0, restock-0, sellable-0; dml_late: restock-1). Nothing applied.
