# Run locally (no cloud cost)

This mode runs the demo on one machine without AWS or Confluent Cloud. It uses a Lima VM, local Kafka and Schema Registry, the `sellable-dev` service as a stand-in for Confluent Cloud Flink, local Redis, and nginx instead of an AWS Application Load Balancer. It requires a Datadog trial API key in the local `.env` so the observability path remains part of the local run.

From the repository root:

1. Create `.env` with `DD_API_KEY` and `DD_SITE=datadoghq.eu`; never commit or print the key.
2. Run `make secrets` to create `.env.secrets` with random local passwords.
3. Run `make MODE=dev config` to validate configuration without starting containers.
4. Run `make MODE=dev build` to build images in the Lima VM.
5. Run `make MODE=dev up-dev` to start the local core services.
6. Run `make MODE=dev seed`, then `make MODE=dev register-connector` and wait for connectors to report `RUNNING`.
7. Run `make MODE=dev verify`, `make MODE=dev status`, and `make MODE=dev smoke`.
8. Open http://localhost:8088 for the shop. `make MODE=dev control` prints the control-panel URL; the password is `CONTROL_PASSWORD` in `.env.secrets`.
9. Run `make MODE=dev down-dev` to stop the services; add `PURGE=1` when you also want to delete local data volumes.

The local path is deliberately a local run, not a cloud deployment: no AWS, Confluent Cloud, ElastiCache, or ALB resources are created, so it has zero cloud cost. The cloud walkthrough remains in [the workshop guide](workshop/README.md); the step-by-step local notes are also in [RUNBOOK.md](RUNBOOK.md).
