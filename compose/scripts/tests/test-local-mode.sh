#!/usr/bin/env bash
# Offline guard for local mode: `mode: local` in demo.yaml makes plain make act on the local stack and never
# on the cloud; local-up/local-down chain the LOCAL.md steps and honour layers; _up starts 1.1.0/1.2.0 only with the
# releases layer; route-check works locally; links/links-publish/links-json publish local URLs.
# Synthetic demo.yaml and stubs only (DC, LAYER, terraform, docker): nothing touches Docker, Lima, AWS or Terraform state.
set -euo pipefail
OVERLAY="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-local-mode: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp" "$OVERLAY/.state/layers-cloudy.env"' EXIT
# Cloud-mode make checks that the calibration profile is git-tracked; the standalone suite runs on a non-git copy.
mkdir -p "$tmp/gitbin"
printf '#!/usr/bin/env bash\ncase " $* " in *" ls-files "*) exit 0;; esac\nexec "%s" "$@"\n' "$(command -v git)" > "$tmp/gitbin/git"
chmod +x "$tmp/gitbin/git"
mk() { env -u STACK -u MODE -u TOPOLOGY -u ENV_DIR -u LAYERS -u LOCAL_LAYERS PATH="$tmp/gitbin:$PATH" \
  make --no-print-directory -C "$OVERLAY" "$@" 2>&1; }
yaml() { # yaml <file> <lines...>
  local f="$1"; shift; printf '%s\n' "# synthetic, fake keys" "$@" > "$f"; chmod 600 "$f"
}
CLOUD_KEYS=("stack: cloudy" "aws_profile: p" "aws_region: eu-west-1" "datadog_site: datadoghq.eu" "dd_api_key: fakeddapikey0001"
  "dd_app_key: fakeddappkey0001" "confluent_cloud_api_key: fakeccapikey01" "confluent_cloud_api_secret: fakeccsecret0001")

# 1. demo-yaml.py: mode is the 7th word, default cloud, owner the 8th (- if absent, always - in local mode); local needs only datadog_site and dd_api_key; bad mode fails.
yaml "$tmp/cloud.yaml" "${CLOUD_KEYS[@]}"
out="$(python3 "$OVERLAY/compose/scripts/demo-yaml.py" "$tmp/cloud.yaml")" || fail "cloud yaml rejected: $out"
[ "$out" = "cloudy p eu-west-1 core - false cloud -" ] || fail "cloud output: '$out'"
yaml "$tmp/cloudowner.yaml" "${CLOUD_KEYS[@]}" "owner: jane.doe@example.com"
out="$(python3 "$OVERLAY/compose/scripts/demo-yaml.py" "$tmp/cloudowner.yaml")" || fail "cloud yaml with owner rejected: $out"
[ "$out" = "cloudy p eu-west-1 core - false cloud jane.doe@example.com" ] || fail "cloud owner output: '$out'"
yaml "$tmp/local.yaml" "mode: local" "datadog_site: datadoghq.eu" "dd_api_key: fakeddapikey0001" "layers: core,releases,dd-synthetics"
out="$(python3 "$OVERLAY/compose/scripts/demo-yaml.py" "$tmp/local.yaml")" || fail "minimal local yaml rejected: $out"
[ "$out" = "- - - core,releases,dd-synthetics - false local -" ] || fail "local output: '$out'"
# one-line flip: the full cloud file with mode: local, even with cloud placeholders left in, is accepted
yaml "$tmp/flip.yaml" "mode: local" "${CLOUD_KEYS[@]}" "confluent_cloud_api_key: REPLACE_WITH_CONFLUENT_API_KEY"
sed -i.bak '/fakeccapikey01/d' "$tmp/flip.yaml"
out="$(python3 "$OVERLAY/compose/scripts/demo-yaml.py" "$tmp/flip.yaml")" || fail "flipped cloud yaml rejected: $out"
case "$out" in *" local -") ;; *) fail "flipped yaml is not local: $out";; esac
yaml "$tmp/badmode.yaml" "mode: laptop" "${CLOUD_KEYS[@]}"
out="$(python3 "$OVERLAY/compose/scripts/demo-yaml.py" "$tmp/badmode.yaml" 2>&1)" && fail "mode: laptop accepted"
case "$out" in *"mode must be local or cloud"*) ;; *) fail "bad mode message: $out";; esac
yaml "$tmp/nokey.yaml" "mode: local" "datadog_site: datadoghq.eu"
out="$(python3 "$OVERLAY/compose/scripts/demo-yaml.py" "$tmp/nokey.yaml" 2>&1)" && fail "local without dd_api_key accepted"
case "$out" in *"missing required key(s): dd_api_key"*) ;; *) fail "local missing-key message: $out";; esac

# 2. A local demo.yaml: plain make targets the local stack (the old trap), cloud targets refuse.
out="$(mk -n DEMO_YAML="$tmp/local.yaml" verify)" || fail "make -n verify with local yaml failed: $out"
case "$out" in *"limactl shell dd-demo"*"compose -p dd-demo-dev "*"compose.dev.yaml"*) ;; *) fail "local yaml verify is not MODE=dev STACK=dev: $out";; esac
case "$out" in *compose.cloud.yaml*|*compose.hybrid.yaml*) fail "local yaml reached a cloud compose file: $out";; esac
out="$(mk DEMO_YAML="$tmp/local.yaml" STACK_SH=/usr/bin/false stack-up)" && fail "stack-up ran with a local yaml: $out"
case "$out" in *"stack-up: run with MODE=cloud"*) ;; *) fail "stack-up refusal message: $out";; esac
out="$(mk -n DEMO_YAML="$tmp/cloud.yaml" verify)" || fail "cloud yaml verify failed: $out"
case "$out" in *"compose -p dd-demo-cloudy "*"compose.hybrid.yaml"*) ;; *) fail "cloud yaml no longer targets the cloud: $out";; esac

# 3. local-up: the LOCAL.md sequence in order, layers from the yaml, cloud-only layers skipped, bad layers refused.
out="$(mk -n DEMO_YAML="$tmp/local.yaml" local-up)" || fail "make -n local-up failed: $out"
prev=0
for want in "local-preflight" "gen-secrets.sh" "config --quiet" " build" "compose.dev.yaml.* up -d" "scenario seed" \
            "register-connector.sh" "layer.sh on releases" "publish-links.sh" "scenario verify" "run --rm -T smoke" "scripts/links.sh"; do
  n="$(printf '%s\n' "$out" | grep -nE -- "$want" | head -n1 | cut -d: -f1)"
  [ -n "$n" ] || fail "local-up dry run lacks '$want': $out"
  [ "$n" -gt "$prev" ] || fail "local-up step '$want' is out of order (line $n after $prev)"
  prev="$n"
done
case "$out" in *"layer.sh on restock"*|*"layer.sh on offers"*) fail "local-up turned on a layer not in the yaml";; esac
case "$out" in *"skipped locally: dd-synthetics"*) ;; *) fail "cloud-only layer not reported as skipped: $out";; esac
out="$(mk -n DEMO_YAML="$tmp/absent.yaml" MODE=dev local-up LOCAL_LAYERS=all)" || fail "local-up LOCAL_LAYERS=all failed: $out"
for l in releases restock offers; do case "$out" in *"layer.sh on $l"*) ;; *) fail "all lacks $l locally";; esac; done
out="$(mk DEMO_YAML="$tmp/absent.yaml" MODE=dev local-up LOCAL_LAYERS=core,kafka)" && fail "unknown local layer accepted"
case "$out" in *"unknown layer(s) 'kafka'"*) ;; *) fail "unknown layer message: $out";; esac
out="$(mk DEMO_YAML="$tmp/absent.yaml" MODE=cloud STACK=cloudy local-up)" && fail "local-up ran in MODE=cloud"
case "$out" in *"local mode only"*) ;; *) fail "local-up MODE=cloud message: $out";; esac

# 4. local-down keeps volumes unless PURGE=1.
out="$(mk -n DEMO_YAML="$tmp/local.yaml" local-down)" || fail "local-down failed: $out"
printf '%s\n' "$out" | grep -E 'compose.dev.yaml.* down *$' >/dev/null || fail "local-down must run down without -v: $out"
out="$(mk -n DEMO_YAML="$tmp/local.yaml" local-down PURGE=1)" || fail "local-down PURGE=1 failed: $out"
printf '%s\n' "$out" | grep -E 'compose.dev.yaml.* down -v' >/dev/null || fail "local-down PURGE=1 must delete volumes: $out"

# 5. _up recreates 1.1.0/1.2.0 only while the releases layer runs (stubs for DC and LAYER; nothing starts).
printf '#!/usr/bin/env bash\necho "dc $*" >> "%s"\n' "$tmp/dc.log" > "$tmp/dc"
printf '#!/usr/bin/env bash\n[ "$1" = running ] && { echo "${RUNNING:-}"; exit 0; }\necho "layer $*" >> "%s"\n' "$tmp/dc.log" > "$tmp/layer"
chmod +x "$tmp/dc" "$tmp/layer"
for running in "" "releases"; do
  : > "$tmp/dc.log"
  RUNNING="$running" mk DEMO_YAML="$tmp/absent.yaml" MODE=dev DC="$tmp/dc" LAYER="$tmp/layer" _up > /dev/null || fail "_up with stubs failed"
  rec="$(grep -- '--force-recreate' "$tmp/dc.log")"
  if [ -z "$running" ]; then
    [ "$rec" = "dc up -d --force-recreate --no-deps inventory-api-100" ] || fail "core-only _up recreated: '$rec'"
  else
    [ "$rec" = "dc up -d --force-recreate --no-deps inventory-api-100 inventory-api-110 inventory-api-120" ] || fail "releases _up: '$rec'"
  fi
  grep -qx "layer sync" "$tmp/dc.log" || fail "_up no longer publishes layers/routing"
done

# 6. route-check works locally (nginx read-back), still the ALB in hybrid.
out="$(mk -n DEMO_YAML="$tmp/local.yaml" route-check)" || fail "route-check locally failed: $out"
case "$out" in *"nginx/apply-routing.sh --sync"*) ;; *) fail "local route-check is not the nginx read-back: $out";; esac

# 7. links.sh local: shop, panel, APM, DSM always; dashboards only with terraform/datadog state for the stack.
root="$tmp/ov"; mkdir -p "$root/compose/scripts" "$root/terraform/datadog" "$tmp/tfbin" "$tmp/notf"
cp "$OVERLAY/compose/scripts/links.sh" "$OVERLAY/compose/scripts/publish-links.sh" "$root/compose/scripts/"
for t in python3 sed grep bash cat env dirname tr tail; do ln -sf "$(command -v "$t")" "$tmp/notf/$t"; done
lk() { env MODE=dev STACK=dev LOCAL_BASE_URL=http://localhost:8088 "$@" 2>&1; }
out="$(lk PATH="$tmp/notf" "$root/compose/scripts/links.sh")" || fail "local links without terraform failed: $out"
for want in "shop: http://localhost:8088/#/product/P0042" "control panel: http://localhost:8088/control/" \
            "APM inventory-api 1.1.0: https://app.datadoghq.eu/apm/services/inventory-api?env=dd-demo-dev&version=1.1.0" \
            "DSM map: https://app.datadoghq.eu/data-streams?env=dd-demo-dev" "terraform is not installed"; do
  case "$out" in *"$want"*) ;; *) fail "local links lack '$want': $out";; esac
done
case "$out" in *"overview dashboard:"*) fail "dashboards printed without state: $out";; esac
cat > "$tmp/tfbin/terraform" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  *"workspace list"*) printf '  default\n* hybrid\n%s\n' "${TF_WS:-}";;
  *"output -json"*) [ "${TF_WORKSPACE:-}" = dev ] || { echo "wrong workspace ${TF_WORKSPACE:-}" >&2; exit 1; }; printf '%s\n' "${TF_OUT:-{\}}";;
  *) echo "unexpected terraform $*" >&2; exit 1;;
esac
STUB
chmod +x "$tmp/tfbin/terraform"
out="$(lk PATH="$tmp/tfbin:$PATH" "$root/compose/scripts/links.sh")" || fail "uninitialised terraform failed: $out"
case "$out" in *"not initialised"*) ;; *) fail "no .terraform not reported: $out";; esac
mkdir "$root/terraform/datadog/.terraform"
out="$(lk PATH="$tmp/tfbin:$PATH" "$root/compose/scripts/links.sh")" || fail "no dev workspace failed: $out"
case "$out" in *"has no workspace 'dev'"*) ;; *) fail "missing workspace not reported: $out";; esac
TF_OUT='{"overview_dashboard_url":{"value":"/dashboard/ov1/x"},"dashboard_url":{"value":"/dashboard/st1/y"}}'
out="$(lk PATH="$tmp/tfbin:$PATH" TF_WS="  dev" TF_OUT="$TF_OUT" "$root/compose/scripts/links.sh")" || fail "dev state links failed: $out"
case "$out" in *"overview dashboard: https://app.datadoghq.eu/dashboard/ov1/x"*"stock dashboard: https://app.datadoghq.eu/dashboard/st1/y"*) ;;
  *) fail "dashboards from dev state missing: $out";; esac
out="$(lk PATH="$tmp/tfbin:$PATH" TF_WS="  dev" TF_OUT='{"dashboard_url":{"value":"/d"}}' "$root/compose/scripts/links.sh")" \
  && fail "half-applied state accepted: $out"
case "$out" in *"overview_dashboard_url is absent"*) ;; *) fail "half-applied state message: $out";; esac

# 8. publish-links.sh local: guide JSON the workshop guide accepts; demo:links/demo:stack go to the local redis.
out="$(lk PATH="$tmp/notf" TOPOLOGY=vm "$root/compose/scripts/publish-links.sh" --print)" || fail "local links-json failed: $out"
printf '%s\n' "$out" | sed -n '/^{/,$p' | python3 -c '
import json, re, sys
g = json.load(sys.stdin)
assert g["alb"] == "http://localhost:8088" and g["stack"] == "dev" and g["env"] == "dd-demo-dev", g
# the guide (workshop/site/site.js parseStack) requires these fields and links
for k, rx in {"vm_public_ip": r"^[A-Za-z0-9][A-Za-z0-9.-]*$", "confluent_env": r"^[A-Za-z0-9_-]+$", "kafka_cluster": r"^[A-Za-z0-9_-]+$"}.items():
    assert re.match(rx, g[k]), (k, g[k])
assert list(g["links"])[:3] == ["control", "shop", "shop-home"], list(g["links"])
assert g["links"]["control"] == "http://localhost:8088/control/"
assert "confluent" not in g["links"] and "ecs" not in g["links"] and "overview-dashboard" not in g["links"]
' || fail "local guide JSON is not what the guide accepts: $out"
printf '#!/usr/bin/env bash\ncat > "%s/redis.$(echo "$*" | sed "s/.* SET //")"\necho OK\n' "$tmp" > "$tmp/dcpub"; chmod +x "$tmp/dcpub"
out="$(lk PATH="$tmp/notf" TOPOLOGY=vm DC="$tmp/dcpub" "$root/compose/scripts/publish-links.sh")" || fail "local links-publish failed: $out"
case "$out" in *"(local)"*) ;; *) fail "local publish summary missing: $out";; esac
python3 -c 'import json,sys; l=json.load(open(sys.argv[1])); assert [x["id"] for x in l]==["shop","shop-home","shop-p0048","shop-p0092","apm","dsm"], l; s=json.load(open(sys.argv[2])); assert s["alb"]=="http://localhost:8088" and s["version"]==1' \
  "$tmp/redis.demo:links" "$tmp/redis.demo:stack" || fail "local demo:links/demo:stack content"
out="$(mk -n DEMO_YAML="$tmp/local.yaml" links-publish)" || fail "make -n links-publish locally refused: $out"
case "$out" in *"limactl shell dd-demo"*"publish-links.sh"*) ;; *) fail "local links-publish does not use the local DC: $out";; esac

# 9. The panel's routing hand-over files exist and nginx's busybox sh can run them (POSIX: no bash-only syntax).
for f in render-routing.sh routing-watch.sh; do
  head -n1 "$OVERLAY/nginx/$f" | grep -qx '#!/bin/sh' || fail "nginx/$f must be POSIX sh (busybox in the nginx image)"
  if command -v dash >/dev/null; then dash -n "$OVERLAY/nginx/$f" || fail "nginx/$f is not POSIX sh"; fi
done
echo "test-local-mode: ok"
