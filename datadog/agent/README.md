# Agent configuration (docker, compose service `datadog-agent`)

Settings come from environment in `compose/compose.yaml` (APM 8126, DogStatsD 8125/udp, both non-local; logs from all
containers except the agent; process collection off; `DD_SITE=datadoghq.eu`, `DD_ENV=dd-demo-<STACK>`, host tags `project:dd-demo stack:<STACK>`,
`DD_HOSTNAME` default `dd-demo-host` locally, set to the instance name on EC2). Checks are the three files in
`conf.d/`, injected through compose `configs`.

Not configured on purpose: a Kafka Connect JMX check. The freshness probe already reports `stock.connect.task_running`
from the Connect REST API, which is the signal the demo uses.
