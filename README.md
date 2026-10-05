# Live stock for a retailer: Debezium, Confluent Cloud, AWS and Datadog

A hands-on demo of a fictional retailer, UrbanStreet, that streams stock changes from five unchanged store databases into Confluent Cloud, serves one honest online stock number from a Redis serving view on AWS, and uses Datadog to find and fix a slow release with a canary. All data is synthetic.

**Start here: [the workshop guide](workshop/README.md)** (architecture, prerequisites, build, seven labs, troubleshooting, teardown). For a zero-cloud-cost rehearsal, see [Run locally](LOCAL.md).

What is in this repository:

- Five PostgreSQL store databases with self-managed Kafka Connect and Debezium, writing directly to Confluent Cloud (Kafka, Schema Registry, Flink SQL).
- A stock projector and a rebuildable Redis serving view (ElastiCache); the website never queries the store databases.
- Three releases of the stock lookup service behind an Application Load Balancer with weighted target groups, for the incident and the canary.
- Optional layers: demand-driven restocking, and offers for carts at risk with an optional AI choice and a rule default.
- Datadog APM, logs, Data Streams Monitoring, freshness probes, LLM Observability, Synthetics, RUM, dashboards and monitors, all as code.
- Docker Compose, Terraform and Make targets in this directory.

Never commit keys. Provider credentials go in an untracked `.env` next to your clone; see [Where each secret goes](workshop/README.md#33-where-each-secret-goes).

Local development without cloud cost: [LOCAL.md](LOCAL.md) and [RUNBOOK.md](RUNBOOK.md).
