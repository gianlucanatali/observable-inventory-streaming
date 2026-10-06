#!/usr/bin/env bash
# Offline guard: with demo.yaml, `make <target>` uses the values ./demo passes (MODE=cloud TOPOLOGY=hybrid STACK=<stack>,
# AWS profile/region for stack.sh, LAYERS for stack-up); command-line values win; MODE=dev and no demo.yaml keep the
# old defaults; an invalid demo.yaml fails loudly. Synthetic demo.yaml in a temp dir, fake keys; nothing runs remotely.
set -euo pipefail
OVERLAY="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-make-demo-yaml: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp" "$OVERLAY/.state/layers-yamltest.env" "$OVERLAY/.state/layers-cliwins.env"' EXIT  # the Makefile touches the stack's layer env file
# Cloud-mode make checks that the calibration profile is git-tracked; the standalone suite runs on a non-git copy.
mkdir -p "$tmp/bin"
real_git="$(command -v git)"
printf '#!/usr/bin/env bash\ncase " $* " in *" ls-files "*) exit 0;; esac\nexec "%s" "$@"\n' "$real_git" > "$tmp/bin/git"
chmod +x "$tmp/bin/git"
write_yaml() { # write_yaml <file> <stack line> [extra lines...]
  local file="$1" stack_line="$2"; shift 2
  {
    echo "# synthetic test config, fake keys"
    [ -n "$stack_line" ] && echo "$stack_line"
    echo "aws_profile: yamlprofile"
    echo "aws_region: eu-central-1  # region comment"
    echo "layers: core,releases"
    echo "datadog_site: datadoghq.eu"
    echo "dd_api_key: fakeddapikey0001"
    echo "dd_app_key: fakeddappkey0001"
    echo "confluent_cloud_api_key: fakeccapikey01"
    echo "confluent_cloud_api_secret: fakeccsecret0001"
    echo "# allowed_cidr: 203.0.113.7/32"
    for line in "$@"; do echo "$line"; done
  } > "$file"
  chmod 600 "$file"
}
YAML="$tmp/demo.yaml"
write_yaml "$YAML" 'stack: "yamltest"'
mk() { env -u STACK -u MODE -u TOPOLOGY -u ENV_DIR -u AWS_PROFILE -u AWS_REGION -u LAYERS -u PRESENTER_CIDR PATH="$tmp/bin:$PATH" \
  make --no-print-directory -C "$OVERLAY" "$@" 2>&1; }

# 1. STACK, MODE, TOPOLOGY, ENV_DIR come from demo.yaml.
out="$(mk -n DEMO_YAML="$YAML" verify)" || fail "make -n verify with demo.yaml failed: $out"
case "$out" in *"compose -p dd-demo-yamltest "*) ;; *) fail "verify does not use STACK from demo.yaml: $out";; esac
case "$out" in *"compose.hybrid.yaml"*) ;; *) fail "verify is not MODE=cloud TOPOLOGY=hybrid: $out";; esac
case "$out" in *"--env-file $tmp/.env.cloud-yamltest"*) ;; *) fail "ENV_DIR is not the demo.yaml folder: $out";; esac
out="$(mk -n DEMO_YAML="$YAML" route-check)" || fail "route-check with demo.yaml failed: $out"
case "$out" in *"$tmp/.env.cloud-yamltest"*"alb-routing.sh --sync"*) ;; *) fail "route-check is not hybrid on the yaml stack: $out";; esac
case "$out" in *AWS_PROFILE*) fail "routing must not receive the source AWS_PROFILE: $out";; esac
out="$(mk -n DEMO_YAML="$YAML" stack-status)" || fail "stack-status with demo.yaml failed: $out"
case "$out" in *"env AWS_PROFILE=yamlprofile AWS_REGION=eu-central-1 $OVERLAY/compose/scripts/stack.sh status"*) ;; *) fail "stack-status lacks the yaml AWS profile/region: $out";; esac

# 2. stack-up exports LAYERS from demo.yaml (a stub replaces stack.sh; it only prints its environment).
printf '#!/usr/bin/env bash\necho "stub STACK=$STACK LAYERS=${LAYERS:-} ARGS=$*"\n' > "$tmp/stack-stub"
chmod +x "$tmp/stack-stub"
out="$(mk DEMO_YAML="$YAML" STACK_SH="$tmp/stack-stub" stack-up)" || fail "stack-up with stub failed: $out"
case "$out" in *"stub STACK=yamltest LAYERS=core,releases ARGS=up"*) ;; *) fail "stack-up does not export yaml LAYERS: $out";; esac
out="$(mk DEMO_YAML="$YAML" STACK_SH="$tmp/stack-stub" LAYERS=core stack-up)" || fail "stack-up LAYERS=core failed: $out"
case "$out" in *"LAYERS=core ARGS=up"*) ;; *) fail "command-line LAYERS does not win: $out";; esac

# 3. Explicit command-line values win.
out="$(mk -n DEMO_YAML="$YAML" STACK=cliwins verify)" || fail "make -n STACK=cliwins verify failed: $out"
case "$out" in *"compose -p dd-demo-cliwins "*) ;; *) fail "command-line STACK does not win: $out";; esac
out="$(mk -n DEMO_YAML="$YAML" AWS_PROFILE=cliprofile stack-status)" || fail "stack-status AWS_PROFILE=cliprofile failed: $out"
case "$out" in *"env AWS_REGION=eu-central-1 $OVERLAY/compose/scripts/stack.sh status"*) ;; *) fail "command-line AWS_PROFILE does not win: $out";; esac
out="$(mk -n DEMO_YAML="$YAML" MODE=dev verify)" || fail "MODE=dev verify with demo.yaml failed: $out"
case "$out" in *"compose -p dd-demo-dev "*"compose.dev.yaml"*) ;; *) fail "MODE=dev must ignore demo.yaml: $out";; esac

# 4. allowed_cidr reaches stack.sh as PRESENTER_CIDR.
write_yaml "$tmp/cidr.yaml" 'stack: yamltest' 'allowed_cidr: 203.0.113.7/32'
printf '#!/usr/bin/env bash\necho "cidr=${PRESENTER_CIDR:-} tf=${TF_VAR_presenter_cidr:-}"\n' > "$tmp/stack-stub"
out="$(mk DEMO_YAML="$tmp/cidr.yaml" STACK_SH="$tmp/stack-stub" stack-status)" || fail "stack-status with allowed_cidr failed: $out"
case "$out" in *"cidr=203.0.113.7/32 tf=203.0.113.7/32"*) ;; *) fail "allowed_cidr is not exported as PRESENTER_CIDR: $out";; esac

# 5. No demo.yaml: old defaults (MODE=dev STACK=dev; MODE=cloud still requires STACK).
out="$(mk -n DEMO_YAML="$tmp/absent.yaml" verify)" || fail "make -n verify without demo.yaml failed: $out"
case "$out" in *"compose -p dd-demo-dev "*"compose.dev.yaml"*) ;; *) fail "without demo.yaml the default is not MODE=dev STACK=dev: $out";; esac
out="$(mk -n DEMO_YAML="$tmp/absent.yaml" MODE=cloud verify)" && fail "MODE=cloud without STACK or demo.yaml passed: $out"
case "$out" in *"STACK is required in MODE=cloud"*) ;; *) fail "missing-STACK message: $out";; esac

# 6. Invalid demo.yaml fails loudly before any recipe runs.
write_yaml "$tmp/nostack.yaml" ''
out="$(mk -n DEMO_YAML="$tmp/nostack.yaml" verify)" && fail "demo.yaml without stack passed: $out"
case "$out" in *"missing required key(s): stack"*"is not usable"*) ;; *) fail "missing-stack message: $out";; esac
write_yaml "$tmp/badstack.yaml" 'stack: Bad_Name'
out="$(mk -n DEMO_YAML="$tmp/badstack.yaml" verify)" && fail "invalid stack passed: $out"
case "$out" in *'stack must match ^[a-z][a-z0-9-]{1,15}$'*) ;; *) fail "invalid-stack message: $out";; esac
chmod 644 "$YAML"
out="$(mk -n DEMO_YAML="$YAML" verify)" && fail "demo.yaml with mode 0644 passed: $out"
case "$out" in *"must be mode 0600"*) ;; *) fail "mode message: $out";; esac
echo "test-make-demo-yaml: ok"
