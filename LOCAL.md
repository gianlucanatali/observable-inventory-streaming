# Run locally (no AWS or Confluent Cloud cost)

On your computer (`mode: local`) the whole demo runs in a Lima VM, with no AWS and no Confluent Cloud. It is for
trying the labs without AWS or Confluent Cloud costs and for changing the code and testing it on your computer; the cloud gives the full workshop. You switch with one line in `demo.yaml`, and the
commands stay the same:

```yaml
mode: local    # or: cloud (the default when the line is absent)
```

```sh
./demo create    # build and start everything locally (no AWS or Confluent Cloud resources)
./demo status    # containers, routing, layers, connectors, then the local links
./demo links     # shop, control panel and Datadog links
./demo reset     # sales off, all traffic to 1.0.0, demo data back to the seeded state
./demo destroy   # stop the local containers; asks before deleting data volumes (default: keep)
```

Local mode is a preview: it has not been tested end to end on a fresh machine yet, so expect rough edges, and please report problems as GitHub issues.

Locally the stack is always called `dev` (Datadog env `dd-demo-dev`), whatever `stack` says, so local data never
mixes with the cloud stack in Datadog. On your computer `./demo` reads only `datadog_site`, `dd_api_key`, `layers` and `jev_api_key`
from `demo.yaml`; the cloud keys can stay in the file, so flipping back to `mode: cloud` needs no other change.

## Your `demo.yaml` for local mode

Locally you need no AWS or Confluent Cloud account, so the file is short. Create `demo.yaml` in the repository root
with only these lines, then run `chmod 600 demo.yaml` (`./demo` refuses a file that others can read):

```yaml
mode: local
datadog_site: datadoghq.eu          # your Datadog site
dd_api_key: <your Datadog API key>
layers: core                        # optional: core (default), all, or a list such as core,releases
# jev_api_key: <your Jev key>       # optional: the AI choice for offers; without it the safe rule is used
```

If you already have a cloud `demo.yaml`, you can instead add `mode: local` to it: the cloud keys are ignored locally.

## Prerequisites

- **A Lima VM named `dd-demo`** running Docker. The demo's VM uses Lima's `docker` template (rootless Docker), `vz`, 6
  CPUs, 12 GiB memory and a 60 GiB disk. To create one (check the flags with `limactl create --help`; this command was
  not run on a fresh machine for this guide):

  ```sh
  limactl create --name=dd-demo --cpus=6 --memory=12 --disk=60 template:docker
  limactl start dd-demo
  ```

  It must be **running** before `./demo create`: `limactl start dd-demo`. `limactl stop dd-demo` gives the memory back.
  Lima shares your home folder with the VM, so the repository must live under your home folder.
- **The `docker` CLI and `make`** on your computer (the Docker engine runs in the VM; the CLI on your computer only
  validates the compose files), plus `python3` and `openssl`.
- **A Datadog API key** of your own (a trial works) in `demo.yaml` as `dd_api_key`, with `datadog_site`. `./demo create`
  writes it to `.env` (mode 600) and keeps every other line already in `.env`, so the keys of a cloud stack survive the
  flip. Never commit or print the key.
- **Internet** for the first build (base images and the Kafka Connect plugins are downloaded).
- **Port 8088** free on your computer (`compose/dev.env`, `INGRESS_PORT`).

`./demo create` checks the VM, the docker CLI and the key first (`make local-preflight`) and stops with the fix if one
is missing.

## What `./demo create` does locally

`make local-up`, which is the step list this page used to give, in one target:

1. `local-preflight`, `secrets` (random local passwords in `.env.secrets`, never printed), `config`, `build` (in the VM).
2. `up-dev`: the core services. The optional layers are **not** started here.
3. `seed`, then `register-connector` (waits until every connector is RUNNING).
4. `layer-on` for each layer in `layers` (see below).
5. `links-publish` (fills the control panel's Links card), `verify`, `smoke`, then the links.

`layers` works as in the cloud: no key or `core` = core only (the step-by-step way), `all` = every layer that exists
locally (`releases`, `restock`, `offers`), or a list. Cloud-only layers in the list (`dd-streams`, `dd-synthetics`,
`dd-rum`, `control-center`) are skipped with a message. Later, `make layer-on L=releases` and the other lab steps work
as written: with `mode: local` in `demo.yaml`, plain `make <target>` acts on the local stack.

The shop is at http://localhost:8088 and the control panel at http://localhost:8088/control/ (user `demo`, password
`CONTROL_PASSWORD` in `.env.secrets`; `make control` prints where).

## What is different locally

| Area | Cloud | Local |
|---|---|---|
| Kafka, Schema Registry | Confluent Cloud | one local Kafka broker and Schema Registry, no auth |
| Sellable stock (Flink) | Confluent Cloud Flink statements | `sellable-dev`, a Python stand-in that computes the same topics; restock and cart-at-risk logic re-implemented in Python, which can drift from the SQL (`sellable-dev/README.md`) |
| Serving view | ElastiCache | local Redis |
| Ingress and canary routing | Application Load Balancer, weighted rule | nginx weighted upstream (exact 10 in 100, `nginx/render-routing.sh`) |
| Releases 1.0.0 / 1.1.0 / 1.2.0 | ECS Fargate, 0.5 vCPU / 1 GB | containers in the VM, 2 CPU / 768 MB each; whether 1.1.0 fails the canary gate in the VM is not yet measured |
| Control panel | all cards | all cards, including Release routing, Full demo reset and Links (nginx and local Redis instead of the ALB and ElastiCache) |
| Confluent Cloud console, Stream Lineage, Lab 1.2 | yes | **no** (inspect the local topics with `kafka-console-consumer` in the `kafka` container) |
| Control Center | optional layer | **no** |
| Datadog APM, DSM, logs, metrics | yes | yes, env `dd-demo-dev` (the DSM map is shorter: no Flink hop) |
| Datadog dashboards and monitors | created by `./demo create` | only after the optional apply below |
| Datadog Synthetics | yes | **no** (Datadog's test locations cannot reach `localhost`) |
| Datadog Confluent integration, AWS dashboard, ECS | yes | **no** |
| Cost dashboard, cost meter AWS/Confluent lines | yes | **no** (nothing is billed) |
| Bedrock (offers AI) | yes | **no**; Jev only if `jev_api_key` is set, otherwise the safe rule |
| RUM | `dd-rum` layer | **no** without setting the RUM ids by hand |

The Links card and `make links-json` work locally too. The workshop guide's "Connect your stack" box accepts the local
JSON: the shop and panel links point at `localhost:8088`, the VM address is `localhost`, the Confluent ids read `local`,
and there are no Confluent, ECS, AWS or cost links.

## Datadog dashboards locally (optional, unverified)

`./demo create` never runs Terraform locally. To see the overview and stock dashboards for the local stack, apply the
Datadog part yourself once, with your Datadog API and application keys in the environment (it creates only Datadog
objects, no AWS or Confluent Cloud cost):

```sh
cd terraform/datadog
terraform init
terraform workspace new dev    # or: terraform workspace select dev
DD_API_KEY=... DD_APP_KEY=... terraform apply -var stack=dev
```

After that `./demo links` and the Links card include the overview and stock dashboards. Without it they say the
dashboards were not created, and the APM and DSM links still work. This path was not tried for this guide.

## Things to know

- **A cloud stack keeps billing when you flip to local.** `mode: local` does not destroy anything in the cloud;
  `./demo create` warns when a cloud stack's `.env.cloud-<stack>` file is still there. Flip back to `mode: cloud` and
  run `./demo destroy` when you are done with it.
- **Plain `make` follows `mode`.** With `mode: cloud` in `demo.yaml`, plain `make <target>` acts on the cloud stack
  named there; add `MODE=dev` to act on the local one. With `mode: local` (or without `demo.yaml`) plain `make` is local
  and the cloud-only targets (`stack-up`, `stack-down`, ...) refuse to run.
- **Rollback memory.** The panel remembers the routing before its last change; `make rollback` remembers the routing
  before the last `make` change. Use one of the two at a time, as on the cloud stack.
- **One Datadog host name.** Local and cloud agents both report as `dd-demo-host` unless `DD_HOSTNAME` is set in
  `.env`; set it if both run at the same time.
- **Memory.** Core plus releases fits the 12 GiB VM; every layer plus the smoke browser at once has not been measured.

The cloud walkthrough stays in [the workshop guide](workshop/README.md); [RUNBOOK.md](RUNBOOK.md) has operator notes.
