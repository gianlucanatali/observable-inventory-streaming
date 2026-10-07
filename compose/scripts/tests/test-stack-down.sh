#!/usr/bin/env bash
# Offline guard for stack-down: credential preflight, keep-going per layer, state kept on failure.
# Runs the real `stack.sh down` against a temporary overlay with fake terraform, aws, confluent, curl, docker
# and ssh-keygen on PATH. Nothing reaches a real account, Terraform state or ~/.ssh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK_SH="$ROOT/compose/scripts/stack.sh"
fail() { printf 'test-stack-down: %s\n' "$*" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"

cat >"$BIN/terraform" <<'SH'
#!/usr/bin/env bash
dir="${1#-chdir=}"; dir="${dir##*/}"; shift
echo "terraform $dir $*" >>"$CALLS"
case "$1" in
  init) exit 0;;
  workspace) case "$2" in list) printf '  default\n* hybrid\n';; *) exit 0;; esac; exit 0;;
  state) case "$2" in
      list) case " $STATE_LAYERS " in *" $dir "*) echo "x.$dir"; [ "$dir" = cloud ] && echo "confluent_schema.orders"; [ "$dir" = aws ] && echo "aws_ecr_repository.app";; esac; exit 0;;
      rm) exit 0;;
    esac;;
  output) case "$*" in
      *ecr_repositories*) echo '{"storefront":"000000000000.dkr.ecr.eu-west-1.amazonaws.com/dd-demo-hybrid-storefront"}';;
      *public_ip*) echo 203.0.113.9;;
      *) echo value;;
    esac; exit 0;;
  plan) for a in "$@"; do case "$a" in -out=*) : >"${a#-out=}";; esac; done; exit 0;;
  show) echo "Plan: 0 to add, 0 to change, 3 to destroy."; exit 0;;
  apply) case " ${TF_FAIL_APPLY:-} " in *" $dir "*) echo "Error: simulated $dir destroy failure" >&2; exit 1;; esac; exit 0;;
esac
echo "fake terraform: unexpected: $dir $*" >&2; exit 3
SH
cat >"$BIN/aws" <<'SH'
#!/usr/bin/env bash
echo "aws $*" >>"$CALLS"
case "$*" in
  *"sts get-caller-identity"*)
    [ -n "${AWS_EXPIRED:-}" ] && { echo "Error when retrieving token from sso: Token has expired and refresh failed" >&2; exit 255; }
    echo arn:aws:iam::000000000000:user/test;;
  *"ecr describe-repositories"*) case "${ECR_MODE:-exists}" in
      missing) echo "An error occurred (RepositoryNotFoundException) when calling the DescribeRepositories operation: not found" >&2; exit 254;;
      denied) echo "An error occurred (AccessDeniedException) when calling the DescribeRepositories operation: not authorized" >&2; exit 254;;
      *) echo '{"repositories":[{}]}';;
    esac;;
  *"ssm describe-parameters"*) echo None;;
  *"resourcegroupstaggingapi get-resources"*) :;;
esac
exit 0
SH
cat >"$BIN/confluent" <<'SH'
#!/usr/bin/env bash
echo "confluent $*" >>"$CALLS"
[ -n "${CONFLUENT_EXPIRED:-}" ] && { echo "Error: you must log in to use this command" >&2; exit 1; }
echo '[]'
SH
cat >"$BIN/curl" <<'SH'
#!/usr/bin/env bash
cfg="$(cat)"   # config on stdin (-K -): credentials never on argv
case "$cfg" in *api.confluent.cloud*) echo "curl confluent" >>"$CALLS"; printf '%s' "${CONFLUENT_HTTP:-200}";;
  *datadoghq*) echo "curl datadog" >>"$CALLS"; printf '%s' "${DD_HTTP:-200}";;
  *) exit 7;; esac
SH
cat >"$BIN/docker" <<'SH'
#!/usr/bin/env bash
echo "docker $*" >>"$CALLS"
case "$*" in "context inspect"*) [ -n "${DOCKER_CTX:-}" ] && exit 0; exit 1;; esac
exit 0
SH
cat >"$BIN/ssh-keygen" <<'SH'
#!/usr/bin/env bash
echo "ssh-keygen $*" >>"$CALLS"
SH
chmod +x "$BIN"/*

setup() { # fresh overlay, env dir and local state files for each case
  rm -rf "$TMP/ov" "$TMP/env"; mkdir -p "$TMP/ov/terraform"/{aws,vm,cloud,datadog,images}/.terraform "$TMP/ov/.state" "$TMP/env"
  printf '_down:\n\t@echo "fake compose down" >>"$$CALLS"\n' >"$TMP/ov/Makefile"
  printf 'CONFLUENT_CLOUD_API_KEY=test\nCONFLUENT_CLOUD_API_SECRET=test\nDD_API_KEY=test\nDD_APP_KEY=test\n' >"$TMP/env/.env"
  printf 'RUM=test\n' >"$TMP/env/.env.cloud-hybrid"
  printf 'releases\n' >"$TMP/ov/.state/stack-hybrid.layers"
  printf 'x\n' >"$TMP/ov/.state/routing-hybrid"
  : >"$TMP/calls"
}
run() { # run [VAR=value...] : the real stack-down; prints combined output, returns its status
  env PATH="$BIN:$PATH" CALLS="$TMP/calls" STACK=hybrid OVERLAY="$TMP/ov" ENV_DIR="$TMP/env" CONFIRM=yes \
    STATE_LAYERS="datadog aws vm cloud" "$@" bash "$STACK_SH" down 2>&1
}
kept() { [ -f "$TMP/env/.env.cloud-hybrid" ] && [ -f "$TMP/ov/.state/stack-hybrid.layers" ] && [ -f "$TMP/ov/.state/routing-hybrid" ]; }
destroys() { grep -c '^terraform [a-z]* apply' "$TMP/calls" || true; }

# (a) Expired AWS session at preflight: non-zero, no plan or apply, no local file removed, login named.
setup
out="$(run AWS_EXPIRED=1)" && fail "(a) expired AWS session passed: $out"
case "$out" in *"AWS session expired or missing"*"aws login --profile dd-demo"*"rerun stack-down"*) ;; *) fail "(a) message: $out";; esac
grep -Eq '^terraform [a-z]+ (plan|apply)' "$TMP/calls" && fail "(a) terraform plan/apply ran: $(cat "$TMP/calls")"
grep -q 'ecr describe-repositories' "$TMP/calls" && fail "(a) ECR touched"
kept || fail "(a) local files removed"

# (a2) Confluent CLI logged out and rejected Cloud API key: same, nothing touched.
setup
out="$(run CONFLUENT_EXPIRED=1)" && fail "(a2) Confluent CLI logout passed: $out"
case "$out" in *"run confluent login, then rerun stack-down"*) ;; *) fail "(a2) message: $out";; esac
setup
out="$(run CONFLUENT_HTTP=401)" && fail "(a2) rejected Confluent key passed: $out"
case "$out" in *"Confluent Cloud API key"*"rejected (HTTP 401)"*) ;; *) fail "(a2) key message: $out";; esac
grep -Eq '^terraform [a-z]+ (plan|apply)' "$TMP/calls" && fail "(a2) terraform plan/apply ran"
kept || fail "(a2) local files removed"

# (b) aws destroy fails: cloud is still destroyed, vm skipped with reason, exit non-zero, files kept, summary.
setup
out="$(run TF_FAIL_APPLY=aws DOCKER_CTX=1)" && fail "(b) failed aws layer passed: $out"
grep -q '^terraform datadog apply' "$TMP/calls" || fail "(b) datadog not destroyed"
grep -q '^terraform aws apply' "$TMP/calls" || fail "(b) aws destroy not attempted"
grep -q '^terraform cloud apply' "$TMP/calls" || fail "(b) cloud destroy not attempted after aws failure"
grep -q '^terraform vm apply' "$TMP/calls" && fail "(b) vm destroyed while aws still references its security group"
case "$out" in *"[destroy vm] SKIPPED"*) ;; *) fail "(b) vm skip not explained: $out";; esac
case "$out" in *"FAILED: aws vm(skipped); Terraform state, local env files and docker context kept"*"rerun stack-down"*) ;; *) fail "(b) summary: $out";; esac
grep -q 'ssm describe-parameters' "$TMP/calls" && fail "(b) SSM cleaned after a failed aws destroy"
grep -q 'docker context rm' "$TMP/calls" && fail "(b) docker context removed"
kept || fail "(b) local files removed"

# (b2) vm destroy fails: cloud still destroyed, files kept.
setup
out="$(run TF_FAIL_APPLY=vm)" && fail "(b2) failed vm layer passed: $out"
grep -q '^terraform cloud apply' "$TMP/calls" || fail "(b2) cloud destroy not attempted after vm failure"
case "$out" in *"FAILED: vm;"*) ;; *) fail "(b2) summary: $out";; esac
kept || fail "(b2) local files removed"

# (b3) rerun resumes: only the layers that still have state are destroyed.
setup
out="$(run STATE_LAYERS="aws vm")" || fail "(b3) resume failed: $out"
[ "$(destroys)" = 2 ] || fail "(b3) expected aws+vm destroys only: $(cat "$TMP/calls")"
grep -q 'curl confluent' "$TMP/calls" && fail "(b3) Confluent API key checked without cloud state"

# (c) everything succeeds: datadog first, then aws, vm, images in turn with cloud in parallel; cleanup, leftover check.
setup
out="$(run DOCKER_CTX=1 STATE_LAYERS="datadog aws vm cloud images")" || fail "(c) clean teardown failed: $out"
order="$(grep -o '^terraform [a-z]* apply' "$TMP/calls" | awk '{print $2}' | tr '\n' ' ')"
case "$order" in "datadog "*) ;; *) fail "(c) datadog is not destroyed first: $order";; esac
seq="$(printf '%s' "$order" | tr ' ' '\n' | grep -v '^cloud$' | tr '\n' ' ')"
[ "$seq" = "datadog aws vm images " ] || fail "(c) sequential destroy order: $seq (all: $order)"
case " $order " in *" cloud "*) ;; *) fail "(c) cloud not destroyed: $order";; esac
case "$out" in *"[destroy cloud] started in the background"*"[destroy cloud] background output:"*"[destroy cloud] took"*) ;; *) fail "(c) background cloud output not copied: $out";; esac
grep -q 'ecr delete-repository --repository-name dd-demo-hybrid-storefront --force' "$TMP/calls" || fail "(c) ECR not emptied"
grep -q 'state rm confluent_schema.orders' "$TMP/calls" || fail "(c) schema not detached"
grep -q 'ssh-keygen -R 203.0.113.9' "$TMP/calls" || fail "(c) known_hosts not cleaned"
grep -q 'docker context rm -f dd-demo-hybrid' "$TMP/calls" || fail "(c) docker context kept"
grep -q 'fake compose down' "$TMP/calls" || fail "(c) compose down not run"
[ ! -e "$TMP/env/.env.cloud-hybrid" ] && [ ! -e "$TMP/ov/.state/stack-hybrid.layers" ] && [ ! -e "$TMP/ov/.state/routing-hybrid" ] \
  || fail "(c) local files not removed"
case "$out" in *"no billable AWS leftovers"*"== stack hybrid destroyed"*) ;; *) fail "(c) leftover check/final line: $out";; esac

# (c2) keep_images true: the images layer is kept, everything else destroyed; an invalid value stops before any destroy.
setup
out="$(run KEEP_IMAGES=true STATE_LAYERS="datadog aws vm cloud images")" || fail "(c2) keep_images teardown failed: $out"
grep -q '^terraform images apply' "$TMP/calls" && fail "(c2) images destroyed although keep_images is true"
case "$out" in *"[destroy images] KEPT: keep_images is true"*) ;; *) fail "(c2) kept line: $out";; esac
setup
out="$(run KEEP_IMAGES=maybe STATE_LAYERS="datadog aws vm cloud images")" && fail "(c2) invalid KEEP_IMAGES passed: $out"
case "$out" in *"KEEP_IMAGES must be true or false"*) ;; *) fail "(c2) invalid KEEP_IMAGES message: $out";; esac
grep -Eq '^terraform [a-z]+ (plan|apply)' "$TMP/calls" && fail "(c2) terraform ran with an invalid KEEP_IMAGES"

# (c3) cloud fails in the background: reported in the summary, files kept.
setup
out="$(run TF_FAIL_APPLY=cloud)" && fail "(c3) failed cloud layer passed: $out"
case "$out" in *"FAILED: cloud;"*) ;; *) fail "(c3) summary: $out";; esac
kept || fail "(c3) local files removed"

# (d) ECR access error: the aws layer fails loudly with the AWS text, aws destroy not run, files kept.
setup
out="$(run ECR_MODE=denied)" && fail "(d) ECR access error passed: $out"
case "$out" in *"could not check ECR repository dd-demo-hybrid-storefront"*AccessDeniedException*) ;; *) fail "(d) message: $out";; esac
grep -q '^terraform aws apply' "$TMP/calls" && fail "(d) aws destroy ran after ECR error"
case "$out" in *"FAILED: aws vm(skipped)"*) ;; *) fail "(d) summary: $out";; esac
kept || fail "(d) local files removed"
# (d2) a missing repository is skipped.
setup
out="$(run ECR_MODE=missing)" || fail "(d2) missing ECR repository failed: $out"
case "$out" in *"ECR repository dd-demo-hybrid-storefront already gone"*) ;; *) fail "(d2) skip line: $out";; esac

# (e) Terraform state and workspaces are never deleted by stack.sh.
grep -Eq 'workspace[[:space:]]+delete' "$STACK_SH" && fail "(e) stack.sh deletes a Terraform workspace"
grep -Eq 'rm[[:space:]][^#]*tfstate' "$STACK_SH" && fail "(e) stack.sh removes a terraform.tfstate file"
grep -Eq 'rm[[:space:]][^#]*terraform\.tfstate\.d' "$STACK_SH" && fail "(e) stack.sh removes terraform.tfstate.d"
printf 'test-stack-down: PASS (credential preflight, keep-going layers, state kept on failure, ECR errors loud)\n'
