#!/usr/bin/env bash
# Offline guard for the per-stack image repositories: content tags, "skipped: image unchanged" when ECR has the tag, a build only for the
# missing images, VM images pulled instead of rebuilt, and a forced ECS deployment only where an image was pushed.
# Fake aws, docker and compose on PATH; the real build inputs are hashed read-only. Nothing reaches an account.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-stack-images: %s\n' "$*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN" "$TMP/state"

cat >"$BIN/aws" <<'SH'
#!/usr/bin/env bash
echo "aws $*" >>"$CALLS"
case "$*" in
  *"ecr get-login-password"*) echo fake-password;;
  *"ecr describe-images"*)
    for image in $PRESENT; do case "$*" in *"--repository-name dd-demo-hybrid/$image "*) exit 0;; esac; done
    echo "An error occurred (ImageNotFoundException) when calling the DescribeImages operation: not found" >&2; exit 254;;
esac
exit 0
SH
cat >"$BIN/docker" <<'SH'
#!/usr/bin/env bash
echo "docker $*" >>"$CALLS"
case "$*" in *" login "*) cat >/dev/null;; esac
exit 0
SH
cat >"$BIN/fakedc" <<'SH'
#!/usr/bin/env bash
echo "compose ELASTICACHE_REDIS_URL=${ELASTICACHE_REDIS_URL:-} $*" >>"$CALLS"
SH
chmod +x "$BIN"/*

run() { # run <PRESENT images> <commands> : sources stack.sh, then runs the commands
  local present="$1"; shift
  : >"$TMP/calls"
  PATH="$BIN:$PATH" CALLS="$TMP/calls" PRESENT="$present" STACK=hybrid OVERLAY="$ROOT" TOPOLOGY=hybrid \
    STACK_SOURCE_ONLY=1 DC="$BIN/fakedc" JR_CONTEXT="${JR_CONTEXT:-../../vendor/jr}" \
    JR_DOCKERFILE="${JR_DOCKERFILE:-../../overlay/jr/Dockerfile}" bash -c '
      . "$OVERLAY/compose/scripts/stack.sh"
      STATE_DIR="'"$TMP/state"'"; IMAGE_TAGS_FILE="$STATE_DIR/tags"; IMAGES_PUSHED_FILE="$STATE_DIR/pushed"
      IMAGE_REPOS_JSON="$(all_images | jq -Rn "[inputs | {key: ., value: (\"000000000000.dkr.ecr.eu-west-1.amazonaws.com/dd-demo-hybrid/\" + .)}] | from_entries")"
      aws_cluster() { echo dd-demo-hybrid; }
      '"$*" 2>&1
}
[ -d "$ROOT/compose/../../vendor/jr" ] || { JR_CONTEXT=../vendor/jr; JR_DOCKERFILE=../../jr/Dockerfile; }   # public layout

# 1. Tags are stable and change with the content.
out="$(run "" 'write_image_tags; cat "$IMAGE_TAGS_FILE"')" || fail "write_image_tags failed: $out"
first="$(printf '%s\n' "$out" | grep '^inventory-api=')"
[[ "$first" =~ ^inventory-api=c-[0-9a-f]{20}$ ]] || fail "unexpected tag line: $first"
again="$(run "" 'write_image_tags >/dev/null; grep ^inventory-api= "$IMAGE_TAGS_FILE"')" || fail "second run failed"
[ "$first" = "$again" ] || fail "tag not stable: $first vs $again"
changed="$(run "" 'CATALOGUE_PRODUCTS=7 write_image_tags >/dev/null; grep ^inventory-api= "$IMAGE_TAGS_FILE"')" || fail "build-arg run failed"
[ "$first" != "$changed" ] || fail "a changed build argument did not change the tag"
run "" 'write_image_tags >/dev/null' >/dev/null || fail "restoring tags failed"

# 2. Everything in ECR: no build, no push, VM images pulled, no forced deployment, one batched wait.
all="inventory-api storefront stock-projector offer-worker demo-control cost-meter log-router connect jr freshness-probe scenario smoke supplier-sim"
out="$(run "$all" 'images_ready && roll_out_images')" || fail "(2) all present failed: $out"
[ "$(printf '%s\n' "$out" | grep -c 'skipped: image unchanged')" = 13 ] || fail "(2) expected 13 skipped lines: $out"
grep -q '^compose ' "$TMP/calls" && fail "(2) compose build ran although every image is in ECR"
grep -q 'docker .* push ' "$TMP/calls" && fail "(2) an image was pushed"
[ "$(grep -c 'docker --context dd-demo-hybrid pull' "$TMP/calls")" = 6 ] || fail "(2) expected 6 VM pulls: $(cat "$TMP/calls")"
grep -q 'force-new-deployment' "$TMP/calls" && fail "(2) deployment forced without a new image"
case "$out" in *"no image changed: no forced deployment"*) ;; *) fail "(2) no-change line: $out";; esac
[ "$(grep -c 'ecs wait services-stable' "$TMP/calls")" = 1 ] || fail "(2) expected one batched services-stable wait"

# 3. storefront and smoke missing (keep_images true): one compose build of exactly those, two pushes, storefront forced only.
present="$(printf '%s\n' $all | grep -vx -e storefront -e smoke | tr '\n' ' ')"
out="$(KEEP_IMAGES=true run "$present" 'images_ready && roll_out_images')" || fail "(3) partial failed: $out"
build="$(grep '^compose ' "$TMP/calls")"
case "$build" in *"ELASTICACHE_REDIS_URL=redis://build.invalid:6379/0 "*" build storefront smoke") ;; *) fail "(3) build call: $build";; esac
[ "$(grep -c 'docker --context dd-demo-hybrid push' "$TMP/calls")" = 2 ] || fail "(3) expected 2 pushes: $(cat "$TMP/calls")"
[ "$(sort "$TMP/state/pushed" | tr '\n' ' ')" = "smoke storefront " ] || fail "(3) pushed file: $(cat "$TMP/state/pushed")"
[ "$(grep -c 'force-new-deployment' "$TMP/calls")" = 1 ] || fail "(3) expected one forced deployment"
grep -q 'update-service .*dd-demo-hybrid-storefront --force-new-deployment' "$TMP/calls" || fail "(3) storefront not forced"

# 3b. keep_images false: the missing VM image is built but not pushed (nothing would pull it again).
out="$(run "$present" 'images_ready')" || fail "(3b) partial failed: $out"
[ "$(grep -c 'docker --context dd-demo-hybrid push' "$TMP/calls")" = 1 ] || fail "(3b) expected only the storefront push: $(cat "$TMP/calls")"
case "$out" in *"built smoke on the VM (not pushed: keep_images is false"*) ;; *) fail "(3b) not-pushed line: $out";; esac

# 4. log-router missing: built with docker build, every service forced (it is in every task).
present="$(printf '%s\n' $all | grep -vx log-router | tr '\n' ' ')"
out="$(run "$present" 'images_ready && roll_out_images')" || fail "(4) log-router failed: $out"
grep -q 'docker --context dd-demo-hybrid build --platform linux/arm64' "$TMP/calls" || fail "(4) log-router not built"
[ "$(grep -c 'force-new-deployment' "$TMP/calls")" = 8 ] || fail "(4) expected 8 forced deployments"

# 5. An ECR error that is not "image not found" fails loudly.
cat >"$BIN/aws" <<'SH'
#!/usr/bin/env bash
case "$*" in *"ecr describe-images"*) echo "An error occurred (AccessDeniedException) when calling DescribeImages" >&2; exit 254;; esac
exit 0
SH
out="$(run "" 'images_ready')" && fail "(5) ECR access error passed: $out"
case "$out" in *"could not check image inventory-api"*AccessDeniedException*) ;; *) fail "(5) message: $out";; esac

printf 'test-stack-images: PASS (content tags, skip unchanged, build missing only, forced deploys only for pushed images)\n'
