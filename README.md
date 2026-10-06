# Observable inventory streaming with Confluent and Datadog

> [!IMPORTANT]
> This is an educational demo. Do not use it for production workloads. It is provided as is, without warranty (see [LICENSE](LICENSE)). It creates cloud resources that you pay for, and you are responsible for the costs and for deleting them. It uses synthetic data only. It is not an official Datadog, Confluent or AWS project. Product names and logos are trademarks of their owners and are used only to identify the products (see [NOTICE](NOTICE)).

This is a hands-on demo built around UrbanStreet, a fictional retailer. Stock changes stream from five store databases, which stay unchanged, into Confluent Cloud. The website reads the stock number from a Redis serving view on AWS (a copy of the store stock kept up to date by the stream), so it matches what the stores hold. Then you use Datadog to find a slow release and roll out the fix as a canary, a release that first gets only a small share of traffic. All data is synthetic.

[![Architecture: five store databases on an on-prem VM stream through Debezium and Confluent Cloud into a Redis serving view on AWS; three releases sit behind a load balancer; Datadog observes every layer](workshop/img/arch-5-datadog.png)](workshop/img/arch-5-datadog.png)

*Full architecture (click to enlarge). The [workshop guide](workshop/README.md#2-the-architecture-at-a-glance) builds it up layer by layer.*

**Start here: [the workshop website](https://gianlucanatali.github.io/observable-inventory-streaming/)** (best way to follow it), or read [the workshop guide](workshop/README.md) on GitHub. It covers the architecture, prerequisites, build, seven labs, troubleshooting and teardown. To run it on your own machine with no cloud cost, see [Run locally](LOCAL.md).

What is in this repository:

- Five PostgreSQL store databases. Debezium, running on Kafka Connect that you manage yourself, writes their changes directly to Confluent Cloud (Kafka, Schema Registry, Flink SQL).
- A stock projector and a Redis serving view (ElastiCache) that you can rebuild from the stream. The website never queries the store databases.
- Three releases of the stock lookup service behind an Application Load Balancer with weighted target groups, for the incident and the canary.
- Optional layers: demand-driven restocking, and offers for carts at risk with an optional AI choice and a rule default.
- Datadog APM, logs, Data Streams Monitoring, freshness probes, LLM Observability, Synthetics, RUM, dashboards and monitors. All of it is defined as code.
- Docker Compose, Terraform and Make targets in this directory.

Never commit keys. Put provider credentials in `demo.yaml` in the repository root. Git ignores that file. See [Where each secret goes](workshop/README.md#52-where-each-secret-goes).

For local development with no cloud cost, see [LOCAL.md](LOCAL.md) and [RUNBOOK.md](RUNBOOK.md).

## Repository layout

As a learner, you mostly need `./demo` and the [workshop guide](workshop/README.md). Everything else is what `./demo` builds and runs.

| Path | What it is |
|---|---|
| **Start here** | |
| `demo` | Launcher: `./demo create\|status\|links\|reset\|destroy`, validates `demo.yaml` and drives the Make lifecycle |
| `demo.example.yaml` | Template for your `demo.yaml` (stack name, credentials; git ignores the copy) |
| `workshop/` | The workshop guide (Markdown), its images, and the generator of the web version |
| `docs/` | Generated GitHub Pages site: landing page `index.html` and the guide as `workshop.html` (do not edit by hand) |
| `LOCAL.md`, `RUNBOOK.md` | Local run without cloud cost; pointer to the guide's build, labs and teardown |
| **Services** (one container image each) | |
| `inventory-api/` | Stock lookup API; its three releases (healthy, slow, fix) are one image with different settings |
| `stock-projector/` | Turns the Debezium change stream into the Redis serving view and the `inventory.state` topic |
| `storefront/` | UrbanStreet online shop: React frontend and Flask backend, with product photos |
| `watchdog/` | Freshness probes that decide whether the stock feed can be trusted |
| `offer-worker/` | Offers for carts at risk (optional layer), AI choice with a rule default |
| `supplier-sim/` | Simulated supplier for the restock layer |
| `demo-control/` | Control panel (`/control/`) and JSON API for the labs: sales, routing, checks |
| `cost-meter/` | Running cost estimate per vendor, sent to Datadog as metrics |
| `sellable-dev/` | Local-only stand-in for the Confluent Cloud Flink `sellable` job |
| `jr/` | Synthetic background sales with JR (built from `vendor/jr`) |
| `connect/` | Kafka Connect image (Debezium, Redis sink, JDBC sink) and connector configs |
| `postgres/` | PostgreSQL settings and init scripts for the store and procurement databases |
| `nginx/` | Weighted routing between releases where the AWS load balancer is not used (local and single-VM runs) |
| `log-router/` | Fluent Bit sidecar image that copies control-panel logs to CloudWatch on ECS |
| **Infrastructure and configuration** | |
| `contracts/` | Interfaces every component builds against: Avro schemas, source SQL, shared parameters |
| `flink/` | Confluent Cloud Flink SQL statements |
| `compose/` | Docker Compose files (local, cloud, hybrid) and the stack scripts they use |
| `terraform/` | `vm` (EC2 store estate), `aws` (ECS, ALB, ElastiCache), `cloud` (Confluent Cloud), `datadog` (dashboards, monitors, Synthetics, RUM), `account` (account-wide cost management) |
| `datadog/` | Datadog Agent check configuration |
| `Makefile` | Make targets behind `./demo` |
| **Tooling and tests** | |
| `scenario/` | CLI for seeding, resetting and verifying the demo data |
| `smoke/` | Automated end-to-end smoke test of a running stack |
| `tests/` | Tests for the public launcher and for running the suites from this repository alone |
| `vendor/jr` | Git submodule: the JR fork with the SQL producer |
| `LICENSE`, `NOTICE`, `licenses/` | Apache-2.0 license, third-party notices and trademarks, third-party license texts |

## The guide as a web page

`docs/` holds a static site for GitHub Pages (Settings > Pages > Deploy from a branch > `main` / `/docs`). It has a landing page (`index.html`, built from `workshop/site/landing.html`) and the workshop guide (`workshop.html`). The guide is generated from `workshop/README.md`, its only source. To change it, edit the Markdown, rebuild from this directory, and commit `docs/` together with your change. Old links to `index.html#<heading>` redirect to `workshop.html#<heading>`.

```sh
uv run --with markdown-it-py --with mdit-py-plugins --with pygments --with playwright \
  python workshop/site/build.py
```

The build checks every link inside the page and every relative file link. It leaves out screenshots that do not exist yet (with `--strict`, a missing screenshot is an error). Playwright renders the Mermaid diagrams only when one of them changed, and the rendered SVGs are cached in `workshop/site/mermaid/`. Styles, scripts and the bundled template, icons and fonts are in `workshop/site/` (licenses in [NOTICE](NOTICE)).

## License

Apache License 2.0, see [LICENSE](LICENSE). [NOTICE](NOTICE) lists third-party material, the software the build downloads, and trademark notices.

## Trademarks

Datadog, Confluent, Apache Kafka, Apache Flink, AWS, PostgreSQL, Redis and all other product names, logos and brands belong to their owners. This repository uses them only to identify the products the demo works with. This does not mean that those owners are affiliated with, sponsor or endorse this project. Full notices: [NOTICE](NOTICE).

