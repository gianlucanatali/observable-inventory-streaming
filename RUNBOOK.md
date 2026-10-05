# Runbook

The cloud build, the labs, troubleshooting and teardown are in the workshop guide: [workshop/README.md](workshop/README.md).

## Local development without cloud cost

A local variant runs the same services in a Lima VM named `dd-demo`, with a local Kafka and Schema Registry instead of Confluent Cloud, `sellable-dev` standing in for Confluent Cloud Flink, local Redis instead of ElastiCache, and nginx instead of the ALB. It is for development only; the workshop guide describes the cloud stack. See [LOCAL.md](LOCAL.md) for the reader-facing summary.

1. Create `.env` in the repository root with `DD_API_KEY` and `DD_SITE=datadoghq.eu`. Never paste the key anywhere.
2. `make secrets` creates `.env.secrets` with random passwords (mode 600, never printed).
3. `make config` validates the compose files without starting anything.
4. `make build` builds every image in the VM.
5. `make up-dev` starts the core layer (you run it; it starts containers locally).
6. `make seed`, then `make register-connector` (waits until every connector and task is RUNNING).
7. `make verify`, `make status`, `make smoke`.
8. Open http://localhost:8088 for the shop; `make control` prints the control panel URL (user `demo`, password `CONTROL_PASSWORD` in `.env.secrets`).
9. `make down-dev` stops it (add `PURGE=1` to delete data volumes).
