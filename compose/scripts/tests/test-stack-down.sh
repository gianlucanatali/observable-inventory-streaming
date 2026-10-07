#!/usr/bin/env bash
# Offline guard for stack-down: credential preflight, keep-going per layer, state kept on failure, transient
# network retries, the leftover check and the final banner (always the last output, DESTROY INCOMPLETE on failure).
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
  apply)
    if [ "$dir" = "${TF_TRANSIENT_DIR:-}" ]; then   # fail with a network error the first TF_TRANSIENT_FAILS times
      n="$(cat "$CALLS.transient" 2>/dev/null || echo 0)"
      if [ "$n" -lt "$TF_TRANSIENT_FAILS" ]; then
        echo $((n + 1)) >"$CALLS.transient"
        printf '\033[31m│\033[0m \033[1;31mError: \033[0mreading ECS Service (x): request send failed, Post "https://ecs.eu-west-1.amazonaws.com/": dial tcp: lookup ecs.eu-west-1.amazonaws.com: no such host\n' >&2
        [ -z "${TF_MIXED:-}" ] || echo "│ Error: AccessDeniedException: not authorized to perform ecs:DeleteService" >&2
        exit 1
      fi
    fi
    case " ${TF_FAIL_APPLY:-} " in *" $dir "*) echo "│ Error: simulated $dir destroy failure" >&2; exit 1;; esac; exit 0;;
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
  *"resourcegroupstaggingapi get-resources"*)
    [ -z "${LEFT_INSTANCE:-}" ] || printf 'arn:aws:ec2:eu-west-1:000000000000:instance/%s\thybrid\n' "$LEFT_INSTANCE";;
  *"ec2 describe-instances"*) echo 1;;
esac
exit 0
SH
cat >"$BIN/confluent" <<'SH'
#!/usr/bin/env bash
echo "confluent $*" >>"$CALLS"
[ -n "${CONFLUENT_EXPIRED:-}" ] && { echo "Error: you must log in to use this command" >&2; exit 1; }
# CONFLUENT_LIST_FAILS: the preflight call passes, the later leftover-check call fails (network gone mid-run).
if [ -n "${CONFLUENT_LIST_FAILS:-}" ] && [ -f "$CALLS.confluent-ok" ]; then echo "Error: dial tcp: lookup api.confluent.cloud: no such host" >&2; exit 1; fi
[ -z "${CONFLUENT_LIST_FAILS:-}" ] || : >"$CALLS.confluent-ok"
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
  : >"$TMP/calls"; rm -f "$TMP/calls.transient" "$TMP/calls.confluent-ok"
}
RULE='############################################################'
banner() { # banner <output> : the lines from the first rule to the end; the output must end with the closing rule
  [ "$(printf '%s\n' "$1" | tail -n1)" = "$RULE" ] || fail "the banner is not the last output: $(printf '%s\n' "$1" | tail -n5)"
  printf '%s\n' "$1" | awk -v r="$RULE" '$0 == r { n++ } n >= 1'
}
run() { # run [VAR=value...] : the real stack-down; prints combined output, returns its status
  env PATH="$BIN:$PATH" CALLS="$TMP/calls" STACK=hybrid OVERLAY="$TMP/ov" ENV_DIR="$TMP/env" CONFIRM=yes CI=1 \
    TF_RETRY_DELAYS="0 0" STATE_LAYERS="datadog aws vm cloud" "$@" bash "$STACK_SH" down 2>&1
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

# (b) aws destroy fails: cloud is still destroyed, the VM instance alone is terminated (its security group stays for
#     terraform/aws), exit non-zero, files kept, summary.
setup
out="$(run TF_FAIL_APPLY=aws DOCKER_CTX=1)" && fail "(b) failed aws layer passed: $out"
grep -q '^terraform datadog apply' "$TMP/calls" || fail "(b) datadog not destroyed"
grep -q '^terraform aws apply' "$TMP/calls" || fail "(b) aws destroy not attempted"
grep -q '^terraform cloud apply' "$TMP/calls" || fail "(b) cloud destroy not attempted after aws failure"
grep -q '^terraform vm plan -destroy -input=false -target=aws_instance.main' "$TMP/calls" \
  || fail "(b) the VM instance was not terminated after the aws failure: $(cat "$TMP/calls")"
grep -q '^terraform vm apply' "$TMP/calls" || fail "(b) vm instance destroy not applied"
grep -Eq '^terraform vm plan -destroy -input=false -out' "$TMP/calls" && fail "(b) full vm destroy while aws still references its security group"
case "$out" in *"[destroy vm] EC2 instance only"*) ;; *) fail "(b) vm instance-only destroy not explained: $out";; esac
case "$out" in *"FAILED: aws vm(partial); Terraform state, local env files and docker context kept"*"rerun stack-down"*) ;; *) fail "(b) summary: $out";; esac
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
case "$(banner "$out")" in *"DESTROY COMPLETE: nothing left billing for stack hybrid"*) ;; *) fail "(c) success banner: $out";; esac

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
case "$out" in *"FAILED: aws vm(partial)"*) ;; *) fail "(d) summary: $out";; esac
kept || fail "(d) local files removed"
# (d2) a missing repository is skipped.
setup
out="$(run ECR_MODE=missing)" || fail "(d2) missing ECR repository failed: $out"
case "$out" in *"ECR repository dd-demo-hybrid-storefront already gone"*) ;; *) fail "(d2) skip line: $out";; esac

# (f) aws destroy hits "no such host" twice, then succeeds: retried with a fresh plan each time, the destroy completes.
setup
out="$(run TF_TRANSIENT_DIR=aws TF_TRANSIENT_FAILS=2)" || fail "(f) transient errors were not retried: $out"
[ "$(grep -c '^terraform aws apply' "$TMP/calls")" = 3 ] || fail "(f) expected 3 aws apply attempts: $(cat "$TMP/calls")"
[ "$(grep -c '^terraform aws plan -destroy' "$TMP/calls")" = 3 ] || fail "(f) each retry needs a fresh destroy plan: $(cat "$TMP/calls")"
case "$out" in *"transient network error (attempt 1/3): Error: reading ECS Service"*"no such host"*"retrying terraform/aws with a fresh plan in 0s (attempt 2/3)"*"(attempt 3/3)"*) ;;
  *) fail "(f) retries not logged: $out";; esac
b="$(banner "$out")"
case "$b" in *"DESTROY COMPLETE: nothing left billing for stack hybrid"*) ;; *) fail "(f) success banner: $b";; esac
# (f2) three transient failures in a row: no fourth attempt, the layer fails with the network error as its cause.
setup
out="$(run TF_TRANSIENT_DIR=aws TF_TRANSIENT_FAILS=5)" && fail "(f2) endless transient errors passed: $out"
[ "$(grep -c '^terraform aws apply' "$TMP/calls")" = 3 ] || fail "(f2) expected exactly 3 aws apply attempts: $(cat "$TMP/calls")"
case "$(banner "$out")" in *"Cause: aws layer failed: terraform/aws: Error: reading ECS Service"*"no such host"*) ;; *) fail "(f2) cause: $out";; esac
# (f3) a transient error next to a real one is not retried.
setup
out="$(run TF_TRANSIENT_DIR=aws TF_TRANSIENT_FAILS=1 TF_MIXED=1)" && fail "(f3) mixed errors passed: $out"
[ "$(grep -c '^terraform aws apply' "$TMP/calls")" = 1 ] || fail "(f3) a real error was retried: $(cat "$TMP/calls")"

# (g) a hard failure: the banner is the last output, names what bills and why, the exit is non-zero.
setup
out="$(run TF_FAIL_APPLY=aws)" && fail "(g) hard failure passed: $out"
[ "$(grep -c '^terraform aws apply' "$TMP/calls")" = 1 ] || fail "(g) a hard error was retried"
b="$(banner "$out")"
case "$b" in *"DESTROY INCOMPLETE: stack hybrid is STILL BILLING (estimate: AWS about \$0.50/h)"*) ;; *) fail "(g) headline: $b";; esac
case "$b" in *"Not destroyed (Terraform state kept): aws vm(partial)"*) ;; *) fail "(g) layers: $b";; esac
case "$b" in *"Cause: aws layer failed: terraform/aws: Error: simulated aws destroy failure"*"Note: vm: EC2 instance terminated"*) ;; *) fail "(g) cause: $b";; esac
case "$b" in *"Fix: ./demo destroy --yes   (it resumes"*) ;; *) fail "(g) fix line: $b";; esac
[ "$(head -n1 "$TMP/ov/.state/stack-hybrid.outcome")" = exit=1 ] || fail "(g) outcome file for ./demo: $(cat "$TMP/ov/.state/stack-hybrid.outcome")"
grep -q 'resourcegroupstaggingapi get-resources' "$TMP/calls" || fail "(g) no leftover check after a failed destroy"

# (h) every layer destroyed, but an EC2 instance of the stack still exists: DESTROY INCOMPLETE, non-zero, instance listed.
setup
out="$(run LEFT_INSTANCE=i-0abc)" && fail "(h) leftover instance passed: $out"
b="$(banner "$out")"
case "$b" in *"DESTROY INCOMPLETE: stack hybrid is STILL BILLING"*"Left: AWS ec2 instance/i-0abc"*"Cause: resources still exist after terraform destroy"*) ;;
  *) fail "(h) banner: $b";; esac
case "$out" in *"DESTROY COMPLETE"*) fail "(h) DESTROY COMPLETE printed with a leftover: $out";; esac

# (i) the leftover check itself fails (network down) after a failed layer: the banner says the leftovers are unknown.
setup
out="$(run TF_FAIL_APPLY=cloud CONFLUENT_LIST_FAILS=1)" && fail "(i) failed check passed: $out"
case "$(banner "$out")" in *"STILL BILLING (estimate: Confluent Cloud about \$1 to \$2/h)"*"Left: unknown, the leftover check failed"*) ;; *) fail "(i) banner: $out";; esac

# (j) a failed create: CREATE FAILED lists what is half-built (Terraform state) and why.
setup
out="$(env PATH="$BIN:$PATH" CALLS="$TMP/calls" STACK=hybrid OVERLAY="$TMP/ov" ENV_DIR="$TMP/env" CI=1 PRESENTER_CIDR=203.0.113.1/32 \
  STATE_LAYERS="cloud aws" bash "$STACK_SH" up 2>&1)" && fail "(j) create without CONFIRM passed: $out"
b="$(banner "$out")"
case "$b" in *"CREATE FAILED: stack hybrid is half-built and STILL BILLING"*"Half-built (Terraform state): cloud(2 resources) aws(2 resources)"*) ;; *) fail "(j) banner: $b";; esac
case "$b" in *"Cause: stack-up creates billed resources: add CONFIRM=yes"*"./demo create --yes (it resumes), or ./demo destroy --yes"*) ;; *) fail "(j) cause/fix: $b";; esac

# (k) the background cloud retry and the foreground layers refresh the AWS profile at the same time: no layer may
#     fail on it (it used one shared temporary file). aws fails hard, cloud retries once, the VM is still terminated.
for i in 1 2 3; do
  setup
  out="$(run TF_FAIL_APPLY=aws TF_TRANSIENT_DIR=cloud TF_TRANSIENT_FAILS=1)" && fail "(k) aws failure passed"
  case "$out" in *"could not install AWS refresh profile"*) fail "(k) AWS profile refresh race: $out";; esac
  case "$(banner "$out")" in *"Not destroyed (Terraform state kept): aws vm(partial)"*) ;; *) fail "(k) run $i banner: $out";; esac
done

# (l) colour: bold red for a failure, bold green for DESTROY COMPLETE when stdout is a terminal; plain text with the
#     ### frame otherwise (a pipe, or NO_COLOR set).
colour() { # colour <exit code> <headline> <tty: 1|0> [VAR=value...] : print_banner as run_logged calls it
  local code="$1" head="$2" tty="$3"; shift 3
  env "$@" STACK=hybrid OVERLAY="$TMP/ov" ENV_DIR="$TMP/env" STACK_SOURCE_ONLY=1 CODE="$code" HEAD="$head" TTY="$tty" bash -c '
    . "$0"; write_outcome "$CODE" "$HEAD"
    color=0; if [ "$TTY" = 1 ] && [ -z "${NO_COLOR:-}" ]; then color=1; fi   # the run_logged rule, terminal simulated
    print_banner "$color"' "$STACK_SH"
}
setup
esc=$'\033'
out="$(colour 1 "DESTROY INCOMPLETE: x" 1)"
case "$out" in *"${esc}[1;31m$RULE${esc}[0m"*"${esc}[1;31mDESTROY INCOMPLETE: x${esc}[0m"*) ;; *) fail "(l) failure banner not red: $(printf '%q' "$out")";; esac
out="$(colour 0 "DESTROY COMPLETE: nothing left billing for stack hybrid" 1)"
case "$out" in *"${esc}[1;32mDESTROY COMPLETE: nothing left billing for stack hybrid${esc}[0m"*) ;; *) fail "(l) success banner not green: $(printf '%q' "$out")";; esac
for plain in "$(colour 1 "DESTROY INCOMPLETE: x" 0)" "$(colour 1 "DESTROY INCOMPLETE: x" 1 NO_COLOR=1)"; do
  case "$plain" in *"$esc"*) fail "(l) colour codes without a terminal or with NO_COLOR: $(printf '%q' "$plain")";; esac
  [ "$plain" = "$(printf '\n%s\n%s\n%s' "$RULE" "DESTROY INCOMPLETE: x" "$RULE")" ] || fail "(l) plain banner: $(printf '%q' "$plain")"
done
grep -Fq 'if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then color=1; fi' "$STACK_SH" || fail "(l) run_logged colour rule changed"

# (e) Terraform state and workspaces are never deleted by stack.sh.
grep -Eq 'workspace[[:space:]]+delete' "$STACK_SH" && fail "(e) stack.sh deletes a Terraform workspace"
grep -Eq 'rm[[:space:]][^#]*tfstate' "$STACK_SH" && fail "(e) stack.sh removes a terraform.tfstate file"
grep -Eq 'rm[[:space:]][^#]*terraform\.tfstate\.d' "$STACK_SH" && fail "(e) stack.sh removes terraform.tfstate.d"
printf 'test-stack-down: PASS (credential preflight, keep-going layers, state kept on failure, ECR errors loud, transient retries, final banner last)\n'
