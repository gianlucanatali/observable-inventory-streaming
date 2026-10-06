#!/usr/bin/env bash
# Write every page of the running stack to Redis `demo:links`; the control panel's Links card reads it.
# Env (set by the Makefile): DC (docker compose command line), STACK, TOPOLOGY. Hybrid stacks only: the key goes to
# ElastiCache through a short-lived scenario container on the VM (same path as layer.sh's demo:layers).
# Only non-sensitive URLs: Terraform outputs and the links that `make links` prints.
set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly OVERLAY="$(cd "$HERE/../.." && pwd)"
readonly TF="$OVERLAY/terraform"

die() { echo "publish-links: $*" >&2; exit 1; }
: "${DC:?publish-links: DC must be set; run via 'make links-publish'}"
: "${STACK:?publish-links: STACK must be set}"
[ "${TOPOLOGY:-hybrid}" = hybrid ] || die "only the hybrid stack has a control panel on AWS (TOPOLOGY=${TOPOLOGY})"
region="${AWS_REGION:-eu-west-1}"

out() { # out <dir> <output>
  terraform -chdir="$TF/$1" workspace select "$STACK" >/dev/null \
    || die "Terraform workspace '$STACK' is unavailable in terraform/$1"
  terraform -chdir="$TF/$1" output -raw "$2" || die "terraform/$1 output $2 is absent from state"
}

alb="$(out aws alb_url)"
env_id="$(out cloud environment_id)"
cluster_id="$(out cloud kafka_cluster_id)"
dd_links="$(STACK="$STACK" TOPOLOGY="$TOPOLOGY" "$HERE/links.sh")" || die "links.sh failed"

json="$(ALB="$alb" ENV_ID="$env_id" CLUSTER_ID="$cluster_id" REGION="$region" STACK="$STACK" DD_LINKS="$dd_links" python3 - <<'PY'
import json, os, sys
dd = {}
for line in os.environ["DD_LINKS"].splitlines():
    label, sep, url = line.partition(": ")
    if sep:
        dd[label.strip()] = url.strip()
need = ["stock dashboard", "online dashboard", "account-cost dashboard", "APM inventory-api 1.1.0", "DSM map"]
missing = [k for k in need if k not in dd]
if missing:
    sys.exit(f"publish-links: make links did not print {missing}")
alb, region, stack = os.environ["ALB"].rstrip("/"), os.environ["REGION"], os.environ["STACK"]
links = [
    ("shop", "Shop", "Online shop", "Product page of P0042, the product the labs follow", f"{alb}/#/product/P0042"),
    ("shop-home", "Shop", "Online shop home", "All models, as a shopper sees them", f"{alb}/#/"),
    ("stock-dashboard", "Datadog", "Stock dashboard", "Freshness, pipeline, service objective, restock and offers", dd["stock dashboard"]),
    ("online-dashboard", "Datadog", "AWS online dashboard", "ECS services, load balancer and ElastiCache", dd["online dashboard"]),
    ("apm", "Datadog", "APM: inventory-api", "Latency by release (Deployments)", dd["APM inventory-api 1.1.0"]),
    ("dsm", "Datadog", "Data Streams Monitoring", "Which services read and write which Kafka topics", dd["DSM map"]),
    ("cost-dashboard", "Datadog", "Cost dashboard", "AWS and Confluent Cloud cost for every stack", dd["account-cost dashboard"]),
    ("confluent", "Confluent", "Confluent Cloud cluster", "Topics, messages and Stream Lineage",
     f"https://confluent.cloud/environments/{os.environ['ENV_ID']}/clusters/{os.environ['CLUSTER_ID']}/overview"),
]
if "control-center" in dd:
    links.append(("control-center", "Confluent", "Control Center", "The self-managed Connect worker on the VM", dd["control-center"]))
links.append(("ecs", "AWS", "ECS cluster", "The Fargate services, including the three releases",
              f"https://{region}.console.aws.amazon.com/ecs/v2/clusters/dd-demo-{stack}/services?region={region}"))
print(json.dumps([dict(zip(("id", "group", "name", "desc", "url"), l)) for l in links]))
PY
)" || die "could not build the link list"

# shellcheck disable=SC2086
res="$($DC --profile tools run --rm -T --entrypoint python scenario -c \
  'import os,sys; from redis import Redis; print("OK" if Redis.from_url(os.environ["REDIS_URL"]).set("demo:links", sys.argv[1]) else "FAIL")' "$json")" \
  || die "could not write demo:links to ElastiCache"
[ "$(printf '%s' "$res" | tail -n1)" = OK ] || die "redis SET demo:links answered '$res', expected OK"
echo "   demo:links = $(printf '%s' "$json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "links")')"
