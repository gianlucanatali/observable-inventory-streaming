#!/usr/bin/env bash
# Write every page of the running stack to Redis `demo:links`; the control panel's Links card reads it.
# Env (set by the Makefile): DC (docker compose command line), STACK, TOPOLOGY. Hybrid stacks only: the key goes to
# ElastiCache through a short-lived scenario container on the VM (same path as layer.sh's demo:layers).
# Only non-sensitive URLs: Terraform outputs and the links that `make links` prints.
# Also writes `demo:stack` (ids of the stack) so the panel's "Copy for the workshop guide" button can build the guide JSON.
# `--print` writes nothing: it prints that guide JSON to stdout (make links-json; read-only, needs no DC).
# Local mode (MODE=dev): shop and panel on LOCAL_BASE_URL, Datadog links from links.sh's local branch, the
# keys go to the local Redis container. demo:stack keeps the guide's fields with local values: vm_public_ip=localhost
# (the Lima VM), confluent_env=kafka_cluster=local (local Kafka, no Confluent Cloud ids).
set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly OVERLAY="$(cd "$HERE/../.." && pwd)"
readonly TF="$OVERLAY/terraform"

die() { echo "publish-links: $*" >&2; exit 1; }
mode=publish
case "${1:-}" in
  "") ;;
  --print) mode=print ;;
  *) die "unknown argument '$1' (only --print is accepted)" ;;
esac
if [ "$mode" = publish ]; then : "${DC:?publish-links: DC must be set; run via 'make links-publish'}"; fi
: "${STACK:?publish-links: STACK must be set}"
region="${AWS_REGION:-eu-west-1}"

out() { # out <dir> <output>
  terraform -chdir="$TF/$1" workspace select "$STACK" >/dev/null \
    || die "Terraform workspace '$STACK' is unavailable in terraform/$1"
  terraform -chdir="$TF/$1" output -raw "$2" || die "terraform/$1 output $2 is absent from state"
}

LOCAL=0
if [ "${MODE:-cloud}" = dev ]; then
  LOCAL=1
  alb="${LOCAL_BASE_URL:?publish-links: LOCAL_BASE_URL must be set in local mode (the Makefile sets it)}"
  env_id=local; cluster_id=local; vm_ip=localhost
else
  [ "${TOPOLOGY:-hybrid}" = hybrid ] || die "only the hybrid stack has a control panel on AWS (TOPOLOGY=${TOPOLOGY})"

  alb="$(out aws alb_url)"
  env_id="$(out cloud environment_id)"
  cluster_id="$(out cloud kafka_cluster_id)"
  vm_ip="$(out vm public_ip)"
fi
dd_links="$(STACK="$STACK" TOPOLOGY="$TOPOLOGY" "$HERE/links.sh")" || die "links.sh failed"

json="$(LOCAL="$LOCAL" ALB="$alb" ENV_ID="$env_id" CLUSTER_ID="$cluster_id" REGION="$region" STACK="$STACK" DD_LINKS="$dd_links" python3 - <<'PY'
import json, os, sys
dd = {}
for line in os.environ["DD_LINKS"].splitlines():
    label, sep, url = line.partition(": ")
    if sep:
        dd[label.strip()] = url.strip()
local = os.environ["LOCAL"] == "1"
need = (["APM inventory-api 1.1.0", "DSM map"] if local else
        ["overview dashboard", "stock dashboard", "online dashboard", "account-cost dashboard", "APM inventory-api 1.1.0", "DSM map"])
missing = [k for k in need if k not in dd]
if missing:
    sys.exit(f"publish-links: make links did not print {missing}")
alb, region, stack = os.environ["ALB"].rstrip("/"), os.environ["REGION"], os.environ["STACK"]
# LLM Observability root spans of the offer worker's ml_app, on the DSM link's Datadog site (same path as
# overview.tf home_link.llmobs and scripts/demo-windows.sh dd:llm-traces; the ml_app is shared by all stacks).
from urllib.parse import urlsplit
_dd = urlsplit(dd["DSM map"])
llm_obs = (f"{_dd.scheme}://{_dd.netloc}/llm/traces"
           "?query=%40ml_app%3Aurbanstreet-offers%20%40event_type%3Aspan%20%40is_root_span%3Atrue")
# Datadog list pages filtered to this stack, on the same site as the DSM link; the filters are the ones the guide
# tells the reader to type in Lab 6.1. No account ids: Datadog picks the organisation from the login.
from urllib.parse import quote
_dd_base = f"{_dd.scheme}://{_dd.netloc}"
synthetics = f"{_dd_base}/synthetics/tests?q={quote('dd-demo-' + stack)}"
monitors = f"{_dd_base}/monitors/manage?q={quote('tag:project:dd-demo tag:stack:' + stack)}"
rum = f"{_dd_base}/rum/sessions?query={quote('@application.name:dd-demo-' + stack + '-shop')}"
if local:  # local mode: no AWS, no Confluent Cloud; dashboards only when terraform/datadog was applied for the stack
    links = [
        ("overview-dashboard", "Datadog", "Demo home", "Start here: the six Chapters, each with its key charts and links to the deeper views", dd.get("overview dashboard")),
        ("shop", "Shop", "Online shop", "Product page of P0042, the product the labs follow", f"{alb}/#/product/P0042"),
        ("shop-home", "Shop", "Online shop home", "All models, as a shopper sees them", f"{alb}/#/"),
        ("shop-p0048", "Shop", "Online shop: P0048", "Product page of the boot, used in Lab 5.1", f"{alb}/#/product/P0048"),
        ("shop-p0092", "Shop", "Online shop: P0092", "Product page of the light hiking shoe, used in Lab 5.1", f"{alb}/#/product/P0092"),
        ("stock-dashboard", "Datadog", "Stock dashboard", "Freshness, pipeline, service objective, restock and offers", dd.get("stock dashboard")),
        ("apm", "Datadog", "APM: inventory-api", "Latency by release (Deployments)", dd["APM inventory-api 1.1.0"]),
        ("dsm", "Datadog", "Data Streams Monitoring", "Which services read and write which Kafka topics", dd["DSM map"]),
    ]
    print(json.dumps([dict(zip(("id", "group", "name", "desc", "url"), l)) for l in links if l[4]]))
    sys.exit(0)
links = [
    ("overview-dashboard", "Datadog", "Demo home", "Start here: the six Chapters, each with its key charts and links to the deeper views", dd["overview dashboard"]),
    ("shop", "Shop", "Online shop", "Product page of P0042, the product the labs follow", f"{alb}/#/product/P0042"),
    ("shop-home", "Shop", "Online shop home", "All models, as a shopper sees them", f"{alb}/#/"),
    ("shop-p0048", "Shop", "Online shop: P0048", "Product page of the boot, used in Lab 5.1", f"{alb}/#/product/P0048"),
    ("shop-p0092", "Shop", "Online shop: P0092", "Product page of the light hiking shoe, used in Lab 5.1", f"{alb}/#/product/P0092"),
    ("stock-dashboard", "Datadog", "Stock dashboard", "Freshness, pipeline, service objective, restock and offers", dd["stock dashboard"]),
    ("online-dashboard", "Datadog", "AWS online dashboard", "ECS services, load balancer and ElastiCache", dd["online dashboard"]),
    ("apm", "Datadog", "APM: inventory-api", "Latency by release (Deployments)", dd["APM inventory-api 1.1.0"]),
    ("dsm", "Datadog", "Data Streams Monitoring", "Which services read and write which Kafka topics", dd["DSM map"]),
    ("llm-obs", "Datadog", "LLM Observability", "The offer worker's Jev calls: what was sent and what came back", llm_obs),
    ("synthetics", "Datadog", "Synthetic tests", "The API and browser tests of this stack", synthetics),
    ("monitors", "Datadog", "Monitors", "The monitors of this stack", monitors),
    ("rum", "Datadog", "RUM sessions", "Browser sessions of the online shop", rum),
    ("cost-dashboard", "Datadog", "Cost dashboard", "AWS and Confluent Cloud cost for every stack", dd["account-cost dashboard"]),
    ("confluent", "Confluent", "Confluent Cloud cluster", "Topics, messages and Stream Lineage",
     f"https://confluent.cloud/environments/{os.environ['ENV_ID']}/clusters/{os.environ['CLUSTER_ID']}/overview"),
]
# One-click pages inside the cluster (console URL patterns checked in the console: stream-lineage, and topics/<name>/...)
ccloud = f"https://confluent.cloud/environments/{os.environ['ENV_ID']}/clusters/{os.environ['CLUSTER_ID']}"
links += [
    ("stream-lineage", "Confluent", "Stream Lineage", "Who writes to and reads from each topic, as a graph", f"{ccloud}/stream-lineage"),
    ("topic-inventory-cdc", "Confluent", "Topic inventory.cdc", "The Debezium change events, Messages tab", f"{ccloud}/topics/inventory.cdc/message-viewer"),
    ("topic-stock-sellable", "Confluent", "Topic stock.sellable", "The Flink sellable totals; Query with Flink is on this page", f"{ccloud}/topics/stock.sellable/overview"),
]
if "control-center" in dd:
    links.append(("control-center", "Confluent", "Control Center", "The self-managed Connect worker on the VM", dd["control-center"]))
links.append(("ecs", "AWS", "ECS cluster", "The Fargate services, including the three releases",
              f"https://{region}.console.aws.amazon.com/ecs/v2/clusters/dd-demo-{stack}/services?region={region}"))
print(json.dumps([dict(zip(("id", "group", "name", "desc", "url"), l)) for l in links]))
PY
)" || die "could not build the link list"

stack_json="$(ALB="$alb" VM_IP="$vm_ip" ENV_ID="$env_id" CLUSTER_ID="$cluster_id" STACK="$STACK" python3 -c '
import json, os
print(json.dumps({"version": 1, "stack": os.environ["STACK"], "env": "dd-demo-" + os.environ["STACK"], "alb": os.environ["ALB"].rstrip("/"),
                  "vm_public_ip": os.environ["VM_IP"], "confluent_env": os.environ["ENV_ID"], "kafka_cluster": os.environ["CLUSTER_ID"]}))
')" || die "could not build demo:stack"

if [ "$mode" = print ]; then
  STACK_JSON="$stack_json" LINKS_JSON="$json" python3 -c '
import json, os
stack, links = json.loads(os.environ["STACK_JSON"]), json.loads(os.environ["LINKS_JSON"])
ordered = {"control": stack["alb"] + "/control/"}
ordered.update((l["id"], l["url"]) for l in links)
print(json.dumps({**stack, "links": ordered}, indent=2))
' || die "could not assemble the guide JSON"
  exit 0
fi

if [ "$LOCAL" = 1 ]; then # the local Redis container, the same path as layer.sh and apply-routing.sh in dev
  for pair in "demo:links=$json" "demo:stack=$stack_json"; do
    key="${pair%%=*}"
    # -x: value from stdin, so quoting never depends on how the docker command is wrapped (limactl shell)
    # shellcheck disable=SC2086
    res="$(printf '%s' "${pair#*=}" | $DC exec -T redis redis-cli -x SET "$key")" || die "could not write $key to the local redis"
    [ "$res" = OK ] || die "redis SET $key answered '$res', expected OK"
  done
  echo "   demo:links = $(printf '%s' "$json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "links")') (local)"
  exit 0
fi
# shellcheck disable=SC2086
res="$($DC --profile tools run --rm -T --entrypoint python scenario -c \
  'import os,sys; from redis import Redis; print("OK" if Redis.from_url(os.environ["REDIS_URL"]).set("demo:links", sys.argv[1]) else "FAIL")' "$json")" \
  || die "could not write demo:links to ElastiCache"
[ "$(printf '%s' "$res" | tail -n1)" = OK ] || die "redis SET demo:links answered '$res', expected OK"
res="$($DC --profile tools run --rm -T --entrypoint python scenario -c \
  'import os,sys; from redis import Redis; print("OK" if Redis.from_url(os.environ["REDIS_URL"]).set("demo:stack", sys.argv[1]) else "FAIL")' "$stack_json")" \
  || die "could not write demo:stack to ElastiCache"
[ "$(printf '%s' "$res" | tail -n1)" = OK ] || die "redis SET demo:stack answered '$res', expected OK"
echo "   demo:links = $(printf '%s' "$json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "links")')"
