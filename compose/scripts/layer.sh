#!/usr/bin/env bash
# Layer toggle. Usage (via make): layer.sh on|off <layer> | layer.sh status | layer.sh running | layer.sh sync
# Layers: releases restock offers (containers) and dd-synthetics dd-streams dd-rum (Datadog/Terraform only, no containers).
# After each on/off (and on `sync`, which `make up-*` runs) the running layers are written to Redis `demo:layers`
# (JSON list, always starting with "core") for the control panel, and the routing to `demo:routing`.
# State files in overlay/.state (untracked), one set per stack: layers-<stack>.env (compose env file, passed last by the
# Makefile: RESTOCK_TOPIC, OFFERS_ENABLED) and dd-layers-<stack> (the Terraform-only layers switched on).
# Env (set by the Makefile): DC (docker compose command line), MODE (dev|cloud), STACK, OVERLAY (overlay/ path), CONFIRM, ROUTING, ROUTING_ENV.
# Each step prints its elapsed seconds; the total is printed at the end. Fails loudly, naming the step.
set -euo pipefail
: "${DC:?layer.sh: DC must be set; run via 'make layer-on' / 'make layer-off'}"
: "${MODE:?layer.sh: MODE must be set}"
: "${STACK:?layer.sh: STACK must be set}"
: "${OVERLAY:?layer.sh: OVERLAY must be set}"

ALL_PROFILES="releases restock offers"
[ "$TOPOLOGY" = hybrid ] && ALL_PROFILES="$ALL_PROFILES control-center"
TF_ONLY_LAYERS="dd-synthetics dd-streams dd-rum"
STATE_DIR="$OVERLAY/.state"
LAYERS_ENV="$STATE_DIR/layers-$STACK.env"   # same path as STATE_ENV in the Makefile
DD_LAYERS_FILE="$STATE_DIR/dd-layers-$STACK"
profile_flags() { local p; for p in $ALL_PROFILES; do printf -- '--profile %s ' "$p"; done; }

services_of() {
  case "$1" in
    releases)      echo "inventory-api-110 inventory-api-120";;
    restock)       echo "procurement-db supplier-sim";;
    offers)        echo "offer-worker";;
    control-center) echo "control-center";;
    dd-synthetics|dd-streams|dd-rum) echo "";;
    *) echo "layer.sh: unknown layer '$1' (releases restock offers control-center dd-synthetics dd-streams dd-rum)" >&2; return 1;;
  esac
}
hybrid_ecs_layer() { # releases/offers run on Fargate for the hybrid topology, never on the VM
  [ "$TOPOLOGY" = hybrid ] && { [ "$1" = releases ] || [ "$1" = offers ]; }
}
# Terraform root module that owns each layer's cloud resources; releases has none (containers only).
tf_dir_of() {
  case "$1" in
    restock|offers|control-center) echo cloud;;
    dd-synthetics|dd-streams|dd-rum) echo datadog;;
    releases) echo datadog;;   # the p95-by-version monitor
  esac
}

# shellcheck disable=SC2086
running_services() { $DC $(profile_flags) ps --status running --services; }

# Terraform-only layers have no containers: remember them in .state/dd-layers when switched on.
tf_only_layers() { [ -f "$DD_LAYERS_FILE" ] && grep -v '^$' "$DD_LAYERS_FILE" || true; }
set_tf_only() { # set_tf_only <layer> <on|off>
  mkdir -p "$STATE_DIR"; printf '*\n' > "$STATE_DIR/.gitignore"
  local keep; keep="$(tf_only_layers | grep -vx "$1" || true)"
  { [ -n "$keep" ] && printf '%s\n' "$keep"; [ "$2" = on ] && printf '%s\n' "$1"; true; } > "$DD_LAYERS_FILE"
}

running_layers() {
  local run l s all out=()
  run="$(running_services)" || { echo "layer.sh: could not list running services with '$DC'" >&2; return 1; }
  for l in $ALL_PROFILES; do
    all=1; for s in $(services_of "$l"); do printf '%s\n' "$run" | grep -qx "$s" || all=0; done
    [ "$all" = 1 ] && out+=("$l")
  done
  (IFS=,; echo "${out[*]:-}")
}

publish_layers() { # writes demo:layers; the panel reads it
  local r l json="" run
  if [ "$TOPOLOGY" = hybrid ]; then
    run="$(tr '\n' ',' < "$OVERLAY/.state/stack-$STACK.layers" | sed 's/,$//')"
  else
    run="$(running_layers)" || return 1
  fi
  local seen=" "
  for l in core $(printf '%s' "$run" | tr ',' ' ') $(tf_only_layers); do
    case "$seen" in *" $l "*) continue;; esac   # a Terraform-only layer can be in both lists (dd-streams)
    seen="$seen$l "; json="$json\"$l\","
  done
  json="[${json%,}]"
  local out
  # -x: value from stdin, so quoting never depends on how the docker command is wrapped (limactl shell)
  # shellcheck disable=SC2086
  if [ "$TOPOLOGY" = hybrid ]; then
    out="$($DC --profile tools run --rm -T --entrypoint python scenario -c \
      'import os,sys; from redis import Redis; print("OK" if Redis.from_url(os.environ["REDIS_URL"]).set("demo:layers", sys.argv[1]) else "FAIL")' "$json")" \
      || { echo "layer.sh: could not write demo:layers to ElastiCache" >&2; return 1; }
  else
    out="$(printf '%s' "$json" | $DC exec -T redis redis-cli -x SET demo:layers)" \
      || { echo "layer.sh: could not write demo:layers to redis" >&2; return 1; }
  fi
  [ "$out" = OK ] || { echo "layer.sh: redis SET demo:layers answered '$out', expected OK" >&2; return 1; }
  echo "   demo:layers = $json"
}

# .state/layers.env holds layer-dependent compose variables (one KEY=VALUE per line): RESTOCK_TOPIC switches
# sellable-dev's restock logic (dev only; in cloud Flink runs the statements), OFFERS_ENABLED switches the shop's offer
# branch and sellable-dev's cart-at-risk stand-in. set_state_var rewrites one key and keeps the others.
set_state_var() { # set_state_var <KEY> <value|"">  (empty value removes the key)
  mkdir -p "$STATE_DIR"; printf '*\n' > "$STATE_DIR/.gitignore"; touch "$LAYERS_ENV"
  local rest; rest="$(grep -v "^$1=" "$LAYERS_ENV" || true)"
  { [ -n "$rest" ] && printf '%s\n' "$rest"; [ -n "$2" ] && printf '%s=%s\n' "$1" "$2"; true; } > "$LAYERS_ENV"
}

recreate_if_present() { # recreate_if_present <service...> : compose re-reads layers.env; services missing in this variant are skipped
  local svc have
  # shellcheck disable=SC2086
  have="$($DC config --services)"
  for svc in "$@"; do
    if printf '%s\n' "$have" | grep -qx "$svc"; then
      # shellcheck disable=SC2086
      $DC up -d --no-deps "$svc" || return 1
    else
      echo "   ($svc is not in this variant)"
    fi
  done
}

set_restock_topic() { # set_restock_topic <on|off>
  if [ "$1" = on ]; then
    set_state_var RESTOCK_TOPIC restock.requests
    set_state_var PROCUREMENT_HOST procurement-db
  else
    set_state_var RESTOCK_TOPIC ""
    set_state_var PROCUREMENT_HOST ""
  fi
  recreate_if_present sellable-dev
}

set_offers_enabled() { # set_offers_enabled <on|off>
  if [ "$1" = on ]; then set_state_var OFFERS_ENABLED true; else set_state_var OFFERS_ENABLED ""; fi
  recreate_if_present storefront sellable-dev
}

T0=$(date +%s)
run_routing() { # source only the generated hybrid routing contract, then execute the configured router
  if [ -n "${ROUTING_ENV:-}" ]; then
    [ -r "$ROUTING_ENV" ] || { echo "layer.sh: routing environment $ROUTING_ENV is missing or unreadable" >&2; return 1; }
    set -a; # shellcheck disable=SC1090
    . "$ROUTING_ENV"; set +a
  fi
  "$ROUTING" "$@"
}
step() { # step <label> <command...>
  local label="$1" s e; shift
  echo "== [$label]"
  s=$(date +%s)
  "$@" || { echo "layer.sh: step '$label' FAILED (command: $*)" >&2; return 1; }
  e=$(date +%s)
  echo "   [$label] took $((e - s)) s"
}

wait_healthy() { # wait_healthy <timeout_s> <service...> : state running and (no healthcheck or healthy)
  local timeout="$1"; shift
  local deadline=$(( $(date +%s) + timeout )) svc line
  for svc in "$@"; do
    while :; do
      # shellcheck disable=SC2086
      line="$($DC $(profile_flags) ps --format '{{.State}} {{.Health}}' "$svc" 2>/dev/null | head -n1)"
      case "$line" in
        "running healthy"|"running ") break;;
      esac
      if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "layer.sh: $svc not running/healthy after ${timeout}s (state='${line:-none}'); see: $DC logs $svc" >&2; return 1
      fi
      sleep 3
    done
    echo "   $svc ${line}"
  done
}

terraform_step() { # terraform_step <layer> <on|off> : cloud only; stack.sh applies every Terraform dir the layer touches
  if [ -z "$(tf_dir_of "$1")" ]; then echo "   (layer $1 has no Terraform resources)"; return 0; fi
  if [ "$MODE" != cloud ]; then echo "   (MODE=dev: Terraform skipped; the layer's cloud resources are not needed locally)"; return 0; fi
  if [ "${LAYER_SKIP_TF:-}" = 1 ]; then echo "   (LAYER_SKIP_TF=1: Terraform handled by the caller)"; return 0; fi
  # shellcheck disable=SC2086  # LAYER_STACK_ENV: demo.yaml AWS_PROFILE/AWS_REGION for stack.sh only (Makefile)
  env ${LAYER_STACK_ENV:-} "$OVERLAY/compose/scripts/stack.sh" layer-tf "$1" "$2"
}

post_step() { # post_step <layer> <on|off> : cloud only, e.g. Flink statements whose inputs need a first record (restock), RUM ids (dd-rum)
  if [ "$MODE" != cloud ] || [ "${LAYER_SKIP_TF:-}" = 1 ]; then echo "   (nothing to do)"; return 0; fi
  # shellcheck disable=SC2086  # LAYER_STACK_ENV: demo.yaml AWS_PROFILE/AWS_REGION for stack.sh only (Makefile)
  env ${LAYER_STACK_ENV:-} "$OVERLAY/compose/scripts/stack.sh" layer-post "$1" "$2"
}

connect_py() { # run a python snippet in the connect container
  $DC exec -T connect python3 -c "$1"
}

connector_running() { # connector_running <name> : exit 0 when connector and tasks are RUNNING
  connect_py "
import json, sys, urllib.request
n = '$1'
st = json.load(urllib.request.urlopen('http://localhost:8083/connectors/' + n + '/status', timeout=5))
states = [st['connector']['state']] + [t['state'] for t in st.get('tasks', [])]
print(n, states)
sys.exit(0 if st.get('tasks') and all(s == 'RUNNING' for s in states) else 1)"
}

health_check() { # health_check <layer>
  case "$1" in
    releases)
      wait_healthy 120 inventory-api-110 inventory-api-120 || return 1
      local r
      for r in 110 120; do
        $DC exec -T "inventory-api-$r" python -c "import urllib.request; print('inventory-api-$r readyz', urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3).status)" || return 1
      done;;
    restock)
      wait_healthy 120 procurement-db supplier-sim || return 1
      connector_running restock-procurement || return 1
      connector_running procurement-orders || return 1;;
    offers) wait_healthy 60 offer-worker;;
    control-center) wait_healthy 120 control-center;;
    dd-synthetics|dd-streams|dd-rum) echo "   (Datadog-side layer: verify in the Datadog UI / Terraform output)";;
  esac
}

require_control_center_keys() { # compose.hybrid.yaml takes these with ':-' (every stack interpolates the service)
  local f="${ENV_DIR:?layer.sh: ENV_DIR must be set}/.env.cloud-$STACK" n
  for n in CONTROL_CENTER_KAFKA_API_KEY CONTROL_CENTER_KAFKA_API_SECRET CONTROL_CENTER_SR_API_KEY CONTROL_CENTER_SR_API_SECRET; do
    grep -q "^$n=." "$f" || { echo "layer.sh: $n is missing in $f: terraform/cloud did not create the Control Center keys (enable_control_center)" >&2; return 1; }
  done
}

layer_on() {
  local layer="$1" svcs
  svcs="$(services_of "$layer")"
  step "terraform on" terraform_step "$layer" on
  if hybrid_ecs_layer "$layer"; then
    echo "   ($layer runs on ECS for hybrid; no VM containers started)"
  elif [ -n "$svcs" ]; then
    if [ "$layer" = control-center ]; then step "Control Center keys in .env.cloud-$STACK" require_control_center_keys; fi
    # shellcheck disable=SC2086
    step "compose up $svcs" $DC --profile "$layer" up -d $svcs
    if [ "$layer" = restock ]; then
      step "register connectors (restock-procurement, procurement-orders)" env LAYERS=restock DC="$DC" "$OVERLAY/connect/register-connector.sh"
      step "sellable-dev restock logic on" set_restock_topic on
    fi
    if [ "$layer" = offers ]; then step "shop offers + cart-at-risk stand-in on" set_offers_enabled on; fi
    step "health check" health_check "$layer"
  else
    set_tf_only "$layer" on
    step "health check" health_check "$layer"
  fi
  step "after containers (cloud)" post_step "$layer" on
  step "publish demo:layers" publish_layers
}

layer_off() {
  local layer="$1" svcs
  svcs="$(services_of "$layer")"
  if [ "$layer" = releases ]; then
    step "route 100/0/0 (releases are going away)" run_routing 100 0 0
  fi
  if [ "$layer" = offers ] && ! hybrid_ecs_layer "$layer"; then step "shop offers + cart-at-risk stand-in off" set_offers_enabled off; fi
  if [ "$layer" = restock ]; then
    step "sellable-dev restock logic off" set_restock_topic off
    step "delete connectors restock-procurement, procurement-orders" connect_py "
import urllib.request, urllib.error
for n in ('restock-procurement', 'procurement-orders'):
    req = urllib.request.Request('http://localhost:8083/connectors/' + n, method='DELETE')
    try:
        urllib.request.urlopen(req, timeout=10); print('deleted', n)
    except urllib.error.HTTPError as e:
        if e.code != 404: raise
        print(n, 'was not registered')"
  fi
  if [ -n "$svcs" ] && ! hybrid_ecs_layer "$layer"; then
    # shellcheck disable=SC2086
    step "compose stop+rm $svcs" bash -c "$DC --profile $layer stop $svcs && $DC --profile $layer rm -f $svcs"
  fi
  step "terraform off" terraform_step "$layer" off
  if [ -z "$svcs" ]; then set_tf_only "$layer" off; fi
  step "after terraform (cloud)" post_step "$layer" off
  step "publish demo:layers" publish_layers
}

publish_links() { # hybrid cloud: refresh demo:links so the panel's Links card matches the layers (stack-up does its own)
  if [ "$MODE" != cloud ] || [ "$TOPOLOGY" != hybrid ] || [ "${LAYER_SKIP_TF:-}" = 1 ]; then echo "   (nothing to do)"; return 0; fi
  DC="$DC" STACK="$STACK" TOPOLOGY="$TOPOLOGY" "$OVERLAY/compose/scripts/publish-links.sh"
}

case "${1:-}" in
  on)  : "${2:?layer.sh on <layer>}"; services_of "$2" >/dev/null; layer_on "$2"; step "publish demo:links" publish_links
       echo "== layer $2 ON for stack $STACK, total $(( $(date +%s) - T0 )) s";;
  off) : "${2:?layer.sh off <layer>}"; services_of "$2" >/dev/null; layer_off "$2"; step "publish demo:links" publish_links
       echo "== layer $2 OFF for stack $STACK, total $(( $(date +%s) - T0 )) s";;
  running) running_layers;;
  sync)
    # Called after `make up-*`: redis may still be starting, retry briefly. Layers and routing are what is actually running.
    for i in 1 2 3 4 5 6 7 8 9 10; do publish_layers 2>/dev/null && break; [ "$i" = 10 ] && { publish_layers; exit 1; }; sleep 3; done
    run_routing --sync;;
  status)
    if [ "$TOPOLOGY" = hybrid ]; then
      r=",$(tr '\n' ',' < "$OVERLAY/.state/stack-$STACK.layers" | sed 's/,$//'),"
    else
      r=",$(running_layers),"
    fi
    echo "stack $STACK, core always on; optional layers:"
    for l in $ALL_PROFILES; do
      case "$r" in *",$l,"*) echo "  $l: ON ($(services_of "$l"))";; *) echo "  $l: off";; esac
    done
    t=" $(tf_only_layers | tr '\n' ' ')"
    for l in $TF_ONLY_LAYERS; do
      case "$t" in *" $l "*) echo "  $l: ON (Datadog/Terraform only, no containers)";; *) echo "  $l: off (Datadog/Terraform only, no containers)";; esac
    done;;
  *) echo "usage: layer.sh on|off <layer> | status | running | sync" >&2; exit 2;;
esac
