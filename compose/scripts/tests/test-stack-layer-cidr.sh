#!/usr/bin/env bash
# Offline guard: with an empty allowed_cidr, the make paths that plan terraform/vm or terraform/aws (stack-preflight,
# stack-up, layer-on/off -> stack.sh layer-tf) look up the public IPv4 like ./demo create, print it, and fail loudly when
# the lookup fails. An explicit value is never looked up. vars_of stops on an empty CIDR instead of passing
# -var=presenter_cidr= (it runs inside `$(...) || die`, where set -e is off). layer-on/off hand demo.yaml's AWS
# profile/region to stack.sh. The lookup is mocked with a file:// URL; nothing reaches the network.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-stack-layer-cidr: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf '198.51.100.9\n' > "$tmp/ip-ok"; printf '<html>blocked</html>\n' > "$tmp/ip-bad"
run() { # run <PRESENTER_CIDR> <IP_LOOKUP_URL> <script>; sources stack.sh with terraform stubbed
  PRESENTER_CIDR="$1" IP_LOOKUP_URL="$2" STACK=test OVERLAY="$ROOT" ENV_DIR="$tmp" STACK_SOURCE_ONLY=1 TMPD="$tmp" \
    bash -c '. "$OVERLAY/compose/scripts/stack.sh"; LAYERS_FILE="$TMPD/layers"; : > "$LAYERS_FILE"
      configure_aws_refresh_profile() { :; }; tf() { echo "unexpected tf $*" >&2; return 1; }
      '"$3" 2>&1
}
stub='tf_apply() { echo "apply $1 cidr=$PRESENTER_CIDR"; }; write_env_file() { :; }; hybrid_enabled() { false; }; export CONFIRM=yes'

out="$(run '' "file://$tmp/ip-ok" "$stub; dispatch_command layer-tf dd-rum on")" || fail "layer-tf with mocked lookup failed: $out"
case "$out" in *"using your current IP 198.51.100.9/32"*) ;; *) fail "detected IP not printed: $out";; esac
case "$out" in *"apply datadog cidr=198.51.100.9/32"*) ;; *) fail "layer-tf did not use the detected CIDR: $out";; esac

out="$(run 203.0.113.7/32 "file://$tmp/missing" "$stub; dispatch_command layer-tf dd-rum off")" || fail "explicit CIDR failed: $out"
case "$out" in *"using your current IP"*) fail "explicit allowed_cidr was looked up: $out";; esac
case "$out" in *"cidr=203.0.113.7/32"*) ;; *) fail "explicit allowed_cidr did not win: $out";; esac

for bad in missing ip-bad; do
  out="$(run '' "file://$tmp/$bad" "$stub; dispatch_command layer-tf releases on")" && fail "lookup failure ($bad) did not stop layer-tf: $out"
  case "$out" in *"could not be detected"*|*"did not return an IPv4"*) ;; *) fail "lookup failure ($bad) message missing: $out";; esac
  case "$out" in *"apply "*) fail "terraform ran after a failed lookup ($bad): $out";; esac
done

out="$(run '' "file://$tmp/ip-ok" 'preflight() { echo "preflight cidr=$(presenter_cidr)"; }; dispatch_command preflight')" || fail "preflight dispatch failed: $out"
case "$out" in *"preflight cidr=198.51.100.9/32"*) ;; *) fail "preflight did not get the detected CIDR: $out";; esac

out="$(run '' "file://$tmp/ip-ok" 'vf="$(vars_of vm)" || { echo VARS_FAILED; exit 0; }; echo "$vf"')"
case "$out" in *VARS_FAILED*) ;; *) fail "vars_of vm passed an empty CIDR: $out";; esac

grep -q "LAYER_STACK_ENV='\$(strip \$(DEMO_STACK_ENV))' \$(LAYER) on" "$ROOT/Makefile" || fail "layer-on must pass DEMO_STACK_ENV"
grep -q "LAYER_STACK_ENV='\$(strip \$(DEMO_STACK_ENV))' \$(LAYER) off" "$ROOT/Makefile" || fail "layer-off must pass DEMO_STACK_ENV"
[ "$(grep -c 'env ${LAYER_STACK_ENV:-} "$OVERLAY/compose/scripts/stack.sh"' "$ROOT/compose/scripts/layer.sh")" = 2 ] \
  || fail "layer.sh must hand LAYER_STACK_ENV to both stack.sh calls"
printf 'test-stack-layer-cidr: PASS (empty allowed_cidr is looked up like ./demo create; failures stop before Terraform)\n'
