# terraform/datadog

Dashboard "UrbanStreet stock service" and monitors, all scoped to `env:dd-demo-<stack>`. Provider `DataDog/datadog`, EU site (`https://api.datadoghq.eu/`). Keys only from the environment: `DD_API_KEY`, `DD_APP_KEY` (never in files).

## Creates

- Dashboard (via `datadog_dashboard_json`, queries built from the tables in `main.tf`) with groups Service objective, Freshness, Pipeline, Redis and host, Offers.
- Monitors: inventory-api p95 above `p95_threshold_seconds` (default 0.2 s) by version; `stock.probe.age` > 15 s per store (multi-alert by `store`); `stock.sellable.age` > `sellable_age_threshold_seconds` (default 20, uncalibrated); `stock.probe.age` no-data 2 min (`notify_no_data`, "missing data is not healthy"); `stock.feed.state` < 1 per store; `stock.connect.task_running` < 1 per Debezium connector (`inventory-*`) and a separate monitor for `connector:sellable-redis`; Agent `datadog.agent.up` service check missing or critical (no-data 2 min). Handles come from `notification_handles` (default empty).

Cost: monitors and dashboards have no hourly resource cost here, but check which Datadog products the trial includes (APM, custom metrics volume, Confluent Cloud integration metrics) before relying on it.

## Commands for the human

```sh
export DD_API_KEY=...   # from the untracked .env, do not paste in chat
export DD_APP_KEY=...
cd terraform/datadog
terraform init
terraform workspace new <stack>   # once per stack
terraform plan -var stack=<stack> -var 'notification_handles=["@you@example.com"]'
terraform apply -var stack=<stack> -var 'notification_handles=["@you@example.com"]'
```

## Verified against docs

- APM trace metrics `trace.<SPAN_NAME>.hits`, `.errors`, and the distribution metric `trace.<SPAN_NAME>` queried as `p95:trace.<SPAN_NAME>{...}`; error rate = errors / hits (docs.datadoghq.com/tracing/metrics/metrics_namespace/). `trace.flask.request` is the documented example name.
- Confluent Cloud integration: `confluent_cloud.kafka.consumer_lag_offsets` with tags `consumer_group_id`, `topic`; needs a Cloud resource management key (docs.datadoghq.com/integrations/confluent_cloud/).

## Checks that still require a live account

- That the inventory-api spans are named `flask.request` (depends on ddtrace's Flask integration and `DD_SERVICE=inventory-api`).
- The `version` tag on trace metrics (needs `DD_VERSION` or the unified tag set on the service).
- `percentile(last_5m):p95:trace.flask.request{...}` monitor syntax and p95 for `stock.display.delay`. `stock.freshness.apply_delay` is managed as a distribution metric with `include_percentiles = true`, so its p95 dashboard query is configured rather than depending on a Metrics Summary default.
- `redis.mem.used`, `redis.clients.blocked`, `container.cpu.user`, `system.cpu.user`: standard integration/Agent names, not checked against the account. Redis, container and lag widgets use `{*}` because the `env` tag is not guaranteed on integration metrics.
- `datadog.agent.up` service-check monitor query and the `env:dd-demo` host tag (set via `DD_ENV` or `DD_TAGS` in the Agent config).
- `datadog_dashboard_json` payload shape is validated by the API only at apply time.

## Stacks, layers, tags

- `stack` (required) = Terraform workspace name (precondition on the dashboard). All queries use `env:dd-demo-<stack>` (the variable `env` is gone). Monitors, private location and Synthetics tests are tagged `project:dd-demo`, `stack:<stack>`, `env:dd-demo-<stack>` (monitors, tests) and `layer:<layer>`. The dashboard `UrbanStreet stock service [dd-demo-<stack>]` has a template variable `env` (prefix `env`, default `dd-demo-<stack>`) and all its queries use `$env` (Redis, container and host widgets stay `{*}`). Dashboards cannot carry free tags: the stack is in the title and description; confirm the account's dashboard-tag restrictions before adding free-form tags. The RUM application has no tags argument: name prefix only.
- Toggles (core always on): `enable_releases` (true): p95-by-version monitor (output `monitor_ids.p95_latency` is null when off). `enable_restock` (true): monitors `restock.orders.open` growth (change over 15 min above `restock_open_growth_threshold`, default 5) and supplier simulator no-data (on `restock.lead_time`), dashboard group Restock. `enable_offers` (true): dashboard group Offers. `enable_dd_streams` (true): `datadog_integration_confluent_account` and `_resource` (cluster, type `kafka`) and the consumer lag widget. `enable_dd_synthetics` (false): private location, API test (every 60 s: `GET http://nginx/api/availability/P0042`, status 200, response time below `synthetics_latency_budget_ms` default 800, `$.status` matches available/out_of_stock/unknown) and browser test (every 300 s: opens `http://nginx/#/product/P0042`, waits 3 s, asserts the page contains "online", which both "available online" and "Out of stock online" contain), both on the private location only. `enable_dd_rum` (false): `datadog_rum_application` `dd-demo-<stack>-shop`.
- dd-streams inputs (never hardcoded, never in files): `TF_VAR_confluent_api_key`, `TF_VAR_confluent_api_secret`, `TF_VAR_confluent_cluster_id`, taken from the cloud dir (same stack's workspace): `terraform -chdir=../cloud output -raw datadog_confluent_api_key` / `datadog_confluent_api_secret` / `kafka_cluster_id`. Preconditions fail the plan when they are missing while the layer is on.
- Synthetics private location: `terraform output -raw synthetics_pl_config > ../../../.secrets/synthetics-pl-<stack>.json` (untracked; `secrets/` and `.env*` are gitignored, check that `.secrets/` is too before writing) and mount it into the `dd-synthetics` compose service.
- RUM: outputs `rum_application_id`, `rum_client_token` (sensitive); `terraform output -raw env_file` prints `STACK` and, when RUM is on, `DD_RUM_APPLICATION_ID` and `DD_RUM_CLIENT_TOKEN` for the shop `.env`.
- Argument names were checked against the provider schema (`terraform providers schema -json`, provider ~> 3.0 as locked). A live account must still confirm trial access to Synthetics, RUM and the Confluent integration; the `kafka_id` tag name in the lag widget; integration resource tags reaching metrics; the `assertPageContains` and `wait` browser step types and their `params.value`; the `matches` operator of the JSON path assertion; the `chrome.laptop_large` device id; the shop wording behind the browser assertion; and any additional private-location key or role settings.
- Run: `fmt`, `init -backend=false`, `validate` only.

## Public-location Synthetics and demo config events (contracts 12, 13b)

- The private location and the outputs `synthetics_pl_config` and `synthetics_pl_id` are gone. The two tests run from the Datadog-managed location `synthetics_location` (default `aws:eu-central-1`, the id format used by the provider docs; confirm that the account offers it) against the VM's public address: variable `ingress_base_url` (for example `http://203.0.113.7`), taken from `terraform -chdir=../vm output -raw ingress_base_url` (same stack's workspace). The plan fails when the layer is on and `ingress_base_url` is unset. `synthetics_location` must equal the one given to `overlay/terraform/vm`, which opens port 80 to that location's Synthetics IPs only while the layer is on. The tests no longer use `http://nginx`.
- Dashboard group Restock now has `restock.orders.open`, `restock.orders.delivered` by store, `restock.lead_time` (these three existed) plus an event stream widget "Demo config changes" and the same events overlaid on the `restock.orders.open` chart. Query `tags:project:dd-demo tags:stack:<stack> "demo config:"` (the stack is a literal: the dashboard only has an `env` template variable). Confirm the event search syntax, `event_stream` widget fields and timeseries `events` overlay against the live API before the demo.
- Run: `fmt`, `init -backend=false`, `validate` only.
