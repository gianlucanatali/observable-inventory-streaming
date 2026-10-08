#!/usr/bin/env bash
# Cloud stack lifecycle. Run via make, MODE=cloud STACK=<name>:
#   make stack-preflight            read-only checks: tools, credentials present, AWS identity, ssh key, terraform validate
#   make stack-up   CONFIRM=yes     Terraform account, images (hybrid), cloud + vm, RUM app (hybrid, dd-rum), docker context,
#                                   images (reused from ECR by content tag, else built on the VM) in parallel with Terraform aws,
#                                   compose up, seed, connectors, layers, schema priming, Flink statements, Terraform datadog,
#                                   verify. LAYERS=core (default) | all | releases,restock,...
#                                   REBUILD_IMAGES=1 builds and pushes every image even when its content tag is in ECR.
#   make stack-status               Terraform outputs (non-sensitive), layers, containers
#   make stack-down CONFIRM=yes     compose down -v, terraform destroy datadog, aws, vm, images (unless KEEP_IMAGES=true),
#                                   with cloud in parallel; leftover check
#   make stack-leftovers            read-only: tagged AWS resources confirmed with their service APIs, Confluent environments
# Internal (layer.sh, MODE=cloud): stack.sh layer-tf <layer> on|off ; stack.sh layer-post <layer> on|off
#
# Every Terraform step: init (once), workspace select/new <stack>, plan to a file, print the plan summary, ask "yes"
# (YES=1 skips the question), apply the saved plan. The variables always come from the stack's layer list
# (.state/stack-<stack>.layers), so applying one dir never flips another layer back to its default.
# Credentials: <repo>/.env is sourced into the environment (CONFLUENT_CLOUD_API_KEY/SECRET, DD_API_KEY, DD_APP_KEY),
# AWS through AWS_PROFILE (default dd-demo). Nothing secret is printed; outputs marked sensitive go to mode-600 files.
set -euo pipefail
umask 077   # plan files and env files hold secrets
: "${STACK:?stack.sh: STACK must be set (make MODE=cloud STACK=<name> ...)}"
: "${OVERLAY:?stack.sh: OVERLAY must be set; run via make}"
TOPOLOGY="${TOPOLOGY:-hybrid}"
export TOPOLOGY

ROOT="$(cd "$OVERLAY/.." && pwd)"
if [ -n "${ENV_DIR:-}" ]; then
  ENV_DIR="$ENV_DIR"
elif [ -f "$OVERLAY/.env" ]; then
  ENV_DIR="$OVERLAY"
elif [ -f "$ROOT/.env" ]; then
  ENV_DIR="$ROOT"
else
  ENV_DIR="$OVERLAY"
fi
export ENV_DIR
TF="$OVERLAY/terraform"
AWS_TF="$TF/aws"
STATE_DIR="$OVERLAY/.state"
LAYERS_FILE="$STATE_DIR/stack-$STACK.layers"          # desired optional layers, one per line (core is implicit)
DEFER_FILE="$STATE_DIR/stack-$STACK.flink-deferred"   # Flink files held back until their inputs have schemas
DD_APPLIED="$STATE_DIR/stack-$STACK.datadog-applied"  # marker: the datadog dir has outputs for this stack
DD_RUM_APPLIED="$STATE_DIR/stack-$STACK.datadog-rum-applied"  # marker: only the RUM application was applied (early, hybrid)
IMAGE_TAGS_FILE="$STATE_DIR/stack-$STACK.image-tags"      # image=c-<hash> per image: the tags terraform/aws deploys
IMAGES_PUSHED_FILE="$STATE_DIR/stack-$STACK.images-pushed" # images built and pushed by the last stack-up (one per line)
SSM_DIGEST_FILE="$STATE_DIR/stack-$STACK.ssm-digest"     # sha256 of the last synced SSM values (never the values)
ENV_CLOUD="$ENV_DIR/.env.cloud-$STACK"
CTX="dd-demo-$STACK"
ALL_LAYERS="releases restock offers dd-streams dd-synthetics dd-rum"   # what LAYERS=all turns on
VALID_LAYERS="$ALL_LAYERS"                                          # what may be named explicitly
# Control Center is optional and not part of "all" (no longer in the workshop); name it in layers: to turn it on.
# No LAYERS (no layers key in demo.yaml) means core only.
[ "$TOPOLOGY" = hybrid ] && VALID_LAYERS="$VALID_LAYERS control-center"
ALL_FLINK="sellable offers demand procurement restock"
OWNER="${OWNER:-$(id -un)}"   # demo.yaml owner reaches here as OWNER; the environment wins, then the yaml key, then id -un
# AWS tag value rules (letters, digits, _ . : / = + - @, up to 256); no spaces so the value stays one word.
{ [[ "$OWNER" =~ ^[A-Za-z0-9_.:/=+@-]+$ ]] && [ "${#OWNER}" -le 256 ]; } \
  || { echo "stack.sh: OWNER '$OWNER' is not a valid owner tag value: use 1 to 256 characters from letters, digits and _ . : / = + - @ (no spaces); set owner in demo.yaml or OWNER in the environment" >&2; exit 1; }
AWS_SOURCE_PROFILE="${AWS_PROFILE:-dd-demo}"
AWS_PROFILE="${AWS_SOURCE_PROFILE}-auto"
AWS_CONFIG_FILE="$STATE_DIR/aws-config"
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$STATE_DIR"; printf '*\n' > "$STATE_DIR/.gitignore"

OUTCOME_FILE="$STATE_DIR/stack-$STACK.outcome"   # up/down: exit=<n>, then the final banner (run_logged and ./demo print it last)
CAUSE_FILE=""   # when set, die and note_cause append the failure reason here (the banner quotes its first line)
LEFT_FILE=""    # when set, the leftover check appends one "<provider> <resource>" line per billable leftover
note_cause() { if [ -n "$CAUSE_FILE" ]; then printf '%s\n' "$*" >> "$CAUSE_FILE"; fi; }
# Red only when stderr was a terminal and NO_COLOR is unset (no-color.org). run_logged decides once (STACK_COLOR), because
# below it stderr is the tee pipe; the log copy is stripped of escape codes afterwards, so grep on "stack.sh:" keeps working.
use_color() { [ -z "${NO_COLOR:-}" ] && { [ "${STACK_COLOR:-}" = 1 ] || { [ -z "${STACK_COLOR:-}" ] && [ -t 2 ]; }; }; }
die() {
  if use_color; then printf '\033[1;31m✗\033[0m stack.sh: %s\n' "$*" >&2; else echo "stack.sh: $*" >&2; fi
  note_cause "$*"; exit 1
}
strip_ansi() { # strip_ansi <file> : remove colour codes (and the die marker) in place; the log stays plain text
  local tmp; tmp="$(mktemp "$1.XXXXXX")" || return 0
  sed -e "s/$(printf '\033')\[[0-9;]*m✗$(printf '\033')\[[0-9;]*m //" -e "s/$(printf '\033')\[[0-9;]*[A-Za-z]//g" "$1" > "$tmp" && cat "$tmp" > "$1"
  rm -f "$tmp"
}
first_line() { # first_line <file> : first non-empty line, trimmed, at most 220 characters; nothing when the file is empty
  if [ -s "$1" ]; then awk 'NF { sub(/^[ \t]+/, ""); print substr($0, 1, 220); exit }' "$1"; fi
}
configure_aws_refresh_profile() {
  AWS_PROFILE="${AWS_SOURCE_PROFILE}-auto"
  AWS_CONFIG_FILE="$STATE_DIR/aws-config"
  # A unique temporary file: the background cloud destroy and the foreground layers both call this at the same time.
  local tmp; tmp="$(mktemp "$AWS_CONFIG_FILE.XXXXXX")" || die "could not create a temporary AWS refresh profile next to $AWS_CONFIG_FILE"
  printf '[profile %s]\ncredential_process = env -u AWS_CONFIG_FILE -u AWS_PROFILE aws configure export-credentials --profile %s --format process\nregion = %s\n' \
    "$AWS_PROFILE" "$AWS_SOURCE_PROFILE" "${AWS_REGION:-eu-west-1}" > "$tmp" \
    || die "could not write AWS refresh profile $tmp"
  chmod 600 "$tmp" || die "could not chmod AWS refresh profile $tmp"
  mv "$tmp" "$AWS_CONFIG_FILE" || die "could not install AWS refresh profile $AWS_CONFIG_FILE"
  export AWS_CONFIG_FILE AWS_PROFILE
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_CREDENTIAL_EXPIRATION
}
T0=$(date +%s)
step() { # step <label> <command...>
  local label="$1" s; shift
  echo; echo "== [$label]"
  s=$(date +%s)
  "$@" || die "step '$label' FAILED (command: $*)"
  echo "   [$label] took $(( $(date +%s) - s )) s (stack $STACK, total $(( $(date +%s) - T0 )) s)"
}
mk() { make --no-print-directory -C "$OVERLAY" MODE=cloud STACK="$STACK" TOPOLOGY="$TOPOLOGY" "$@"; }

run_logged() { # run_logged <command> <function> [args...]
  local command="$1" log_file latest_link status statuses=() color=0
  shift
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then color=1; fi   # decided here: below, stdout is the tee pipe
  if [ -z "${STACK_COLOR:-}" ]; then   # an outer run_logged already decided: its stderr was the terminal
    if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then STACK_COLOR=1; else STACK_COLOR=0; fi
  fi; export STACK_COLOR
  case "$command" in up|down) rm -f "$OUTCOME_FILE" || die "could not remove the old banner $OUTCOME_FILE";; esac
  mkdir -p "$LOG_DIR" || die "could not create log directory $LOG_DIR"
  chmod 700 "$LOG_DIR" || die "could not protect log directory $LOG_DIR"
  log_file="$LOG_DIR/$STACK-$command-$(date +%Y%m%d-%H%M%S).log"
  latest_link="$LOG_DIR/$STACK-latest.log"
  : > "$log_file" || die "could not create command log $log_file"
  chmod 600 "$log_file" || die "could not protect command log $log_file"
  ln -sfn "$(basename "$log_file")" "$latest_link" || die "could not update latest log link $latest_link"
  printf '== log started: %s\n' "$log_file" | tee -a "$log_file"
  if [ "${STACK_LOG_DRY_RUN:-}" = 1 ]; then
    printf '   dry-run: command %s was not executed\n' "$command" | tee -a "$log_file"
    status=0
  elif "$@" 2>&1 | tee -a "$log_file"; then
    status=0
  else
    statuses=("${PIPESTATUS[@]}")
    status="${statuses[0]}"
    [ "${statuses[1]}" = 0 ] || status="${statuses[1]}"
  fi
  strip_ansi "$log_file"
  printf '== log finished: %s (exit %s)\n' "$log_file" "$status" | tee -a "$log_file"
  if case "$command" in up|down) true;; *) false;; esac && [ -s "$OUTCOME_FILE" ]; then
    # The final banner is the last thing printed. Under ./demo (DEMO_BANNER=1) ./demo prints it after make exits instead.
    sed 1d "$OUTCOME_FILE" >> "$log_file" || die "could not copy the final banner into $log_file"
    [ "${DEMO_BANNER:-}" = 1 ] || print_banner "$color"
    [ "$status" = 0 ] || notify_failure "$(sed -n 3p "$OUTCOME_FILE")"
  fi
  return "$status"
}

write_outcome() { # write_outcome <exit code> <banner line...> : the final banner of up/down, framed by a rule
  local code="$1" rule='############################################################'
  shift
  { echo "exit=$code"; echo "$rule"; printf '%s\n' "$@"; echo "$rule"; } > "$OUTCOME_FILE" \
    || { echo "stack.sh: could not write the final banner $OUTCOME_FILE" >&2; exit 1; }
}
print_banner() { # print_banner <1 for colour> : the banner of $OUTCOME_FILE, red on failure, green on success
  local on="" off=""
  if [ "$1" = 1 ]; then
    off=$'\033[0m'
    if [ "$(sed -n 1p "$OUTCOME_FILE")" = exit=0 ]; then on=$'\033[1;32m'; else on=$'\033[1;31m'; fi
  fi
  echo
  sed 1d "$OUTCOME_FILE" | while IFS= read -r line; do printf '%s%s%s\n' "$on" "$line" "$off"; done
}
notify_failure() { # notify_failure <headline> : macOS notification, only on a Mac outside CI (STACK_NOTIFY=0 turns it off)
  [ "$(uname -s)" = Darwin ] && [ -z "${CI:-}" ] && [ "${STACK_NOTIFY:-1}" != 0 ] && command -v osascript >/dev/null || return 0
  local text; text="$(printf '%s' "$1" | tr -d '"\\')"
  osascript -e "display notification \"$text\" with title \"dd-demo stack $STACK\" sound name \"Basso\"" >/dev/null 2>&1 \
    || echo "stack.sh: note: the macOS notification could not be shown (the banner above is the result)" >&2
}

load_env() { # provider credentials from ENV_DIR/.env, never echoed
  [ -f "$ENV_DIR/.env" ] || die "$ENV_DIR/.env is missing (needs CONFLUENT_CLOUD_API_KEY, CONFLUENT_CLOUD_API_SECRET, DD_API_KEY, DD_APP_KEY)"
  set -a; # shellcheck disable=SC1091
  . "$ENV_DIR/.env"; set +a
  ENABLE_JEV=false
  [ -n "${JEV_API_KEY:-}" ] && ENABLE_JEV=true
  # Do not freeze aws-login's 15-minute credentials in environment variables. The generated profile's
  # credential_process refreshes them for every AWS provider/CLI process from the longer login session.
  configure_aws_refresh_profile
}

# --- layer list ---------------------------------------------------------------------------------------------
has() { [ -f "$LAYERS_FILE" ] && grep -qx "$1" "$LAYERS_FILE"; }
b() { if has "$1"; then echo true; else echo false; fi; }
set_layers() { # set_layers all|core|<comma list>
  local l want="$1"
  [ "$want" = all ] && want="${ALL_LAYERS// /,}"
  [ "$want" = core ] && want=""
  : > "$LAYERS_FILE"
  for l in ${want//,/ }; do
    case "$l" in core) ;; *) case " $VALID_LAYERS " in *" $l "*) echo "$l" >> "$LAYERS_FILE";; *) die "unknown layer '$l' (valid: core, $VALID_LAYERS, or all = $ALL_LAYERS)";; esac;; esac
  done
}
layer_set() { # layer_set <layer> on|off
  local rest; rest="$( [ -f "$LAYERS_FILE" ] && grep -vx "$1" "$LAYERS_FILE" || true)"
  { [ -n "$rest" ] && printf '%s\n' "$rest"; [ "$2" = on ] && echo "$1"; true; } > "$LAYERS_FILE"
}
defer() { # defer <file...> | defer --none
  if [ "$1" = --none ]; then : > "$DEFER_FILE"; return; fi
  local rest f; rest="$( [ -f "$DEFER_FILE" ] && cat "$DEFER_FILE" || true)"
  { [ -n "$rest" ] && printf '%s\n' "$rest"; for f in "$@"; do echo "$f"; done; } | sort -u | grep -v '^$' > "$DEFER_FILE" || true
}
undefer() { # undefer <file...>
  local f; for f in "$@"; do [ -f "$DEFER_FILE" ] && { grep -vx "$f" "$DEFER_FILE" > "$DEFER_FILE.tmp" || true; mv "$DEFER_FILE.tmp" "$DEFER_FILE"; }; done
}
deferred_hcl() { # ["a","b"]
  local f out=""
  for f in $( [ -f "$DEFER_FILE" ] && cat "$DEFER_FILE" ); do out="$out\"$f\","; done
  echo "[${out%,}]"
}
flink_dml_deferred_hcl() { # FLINK_DML_DEFERRED="a b" -> ["a","b"]
  local f out=""
  for f in ${FLINK_DML_DEFERRED:-}; do out="$out\"$f\","; done
  echo "[${out%,}]"
}

# --- terraform ------------------------------------------------------------------------------------------------
presenter_cidr() {
  [ -n "${PRESENTER_CIDR:-}" ] || die "no allowed CIDR for public ingress: set allowed_cidr in demo.yaml (your public IPv4 as a.b.c.d/32) and rerun; ./demo create, make stack-preflight/stack-up and make layer-on/off detect it when allowed_cidr is empty"
  [[ "$PRESENTER_CIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] \
    || die "allowed CIDR '$PRESENTER_CIDR' is not a single IPv4 address in /32 form (for example 203.0.113.7/32); fix allowed_cidr in demo.yaml"
  echo "$PRESENTER_CIDR"
}
IP_LOOKUP_URL="${IP_LOOKUP_URL:-https://checkip.amazonaws.com}"   # same lookup as ./demo create (overridable for tests)
resolve_presenter_cidr() { # empty allowed_cidr: use the caller's current public IPv4 as /32, exactly like ./demo create
  [ -z "${PRESENTER_CIDR:-}" ] || return 0
  local ip
  ip="$(curl -fsS --max-time 5 "$IP_LOOKUP_URL" | head -c 64 | tr -d '[:space:]')" \
    || die "allowed_cidr is not set and your public IPv4 could not be detected via $IP_LOOKUP_URL (curl failed); set allowed_cidr in demo.yaml to your public IPv4 as a.b.c.d/32 (nothing was changed)"
  [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
    || die "allowed_cidr is not set and $IP_LOOKUP_URL did not return an IPv4 address ('$ip'); set allowed_cidr in demo.yaml to your public IPv4 as a.b.c.d/32 (nothing was changed)"
  PRESENTER_CIDR="$ip/32"; TF_VAR_presenter_cidr="$PRESENTER_CIDR"
  export PRESENTER_CIDR TF_VAR_presenter_cidr
  echo "stack.sh: allowed_cidr not set; using your current IP $PRESENTER_CIDR"
}
tf() { terraform -chdir="$TF/$1" "${@:2}"; }
tf_out() { tf_workspace "$1"; tf "$1" output -raw "$2"; }   # always the stack's own workspace
tf_out_json() { tf_workspace "$1"; tf "$1" output -json "$2"; }
remote_vm_cost_inputs() { # print validated VM instance type then root EBS GB from the already-applied env_file output
  local env_file line key value instance_type="" root_ebs_gb="" saw_instance=0 saw_ebs=0
  env_file="$(tf_out vm env_file)" || die "could not read VM env_file for terraform/aws cost meter"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || die "malformed VM cost input: empty env_file line"
    key="${line%%=*}"; value="${line#*=}"
    [ "$key" != "$line" ] && [ -n "$value" ] || die "malformed VM cost input: expected KEY=VALUE"
    case "$key" in
      COST_EC2_INSTANCE_TYPE)
        [ "$saw_instance" = 0 ] || die "duplicate VM cost input: COST_EC2_INSTANCE_TYPE"
        [[ "$value" =~ ^[a-z][a-z0-9]*\.[a-z0-9]+$ ]] || die "malformed VM cost input: COST_EC2_INSTANCE_TYPE"
        instance_type="$value"; saw_instance=1;;
      COST_EBS_GB)
        [ "$saw_ebs" = 0 ] || die "duplicate VM cost input: COST_EBS_GB"
        [[ "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "malformed VM cost input: COST_EBS_GB"
        root_ebs_gb="$value"; saw_ebs=1;;
      *) die "malformed VM cost input: unexpected key '$key'";;
    esac
  done <<< "$env_file"
  [ "$saw_instance" = 1 ] || die "missing VM cost input: COST_EC2_INSTANCE_TYPE"
  [ "$saw_ebs" = 1 ] || die "missing VM cost input: COST_EBS_GB"
  printf '%s\n%s\n' "$instance_type" "$root_ebs_gb"
}
tf_workspace() { # select or create the stack's workspace
  local workspace="$STACK"
  [ "$1" = account ] && workspace=account
  tf "$1" workspace select "$workspace" >/dev/null 2>&1 || tf "$1" workspace new "$workspace" >/dev/null \
    || die "could not select or create workspace '$workspace' in terraform/$1"
}
tf_init() { [ -d "$TF/$1/.terraform" ] || tf "$1" init -input=false >/dev/null || die "terraform init failed in terraform/$1"; tf_workspace "$1"; }
tf_has_state() { tf_init "$1"; tf_workspace "$1"; [ -n "$(tf "$1" state list 2>/dev/null)" ]; }

vars_of() { # vars_of <dir> : prints one -var argument per line
  case "$1" in
    cloud)
      printf '%s\n' "-var=stack=$STACK" "-var=enable_restock=$(b restock)" "-var=enable_offers=$(b offers)" "-var=enable_control_center=$(b control-center)" \
        "-var=enable_dd_streams=$(b dd-streams)" "-var=flink_deferred=$(deferred_hcl)" \
        "-var=flink_dml_deferred=$(flink_dml_deferred_hcl)";;
    vm)
      local cidr; if [ "${TF_DESTROY:-}" = 1 ]; then cidr="${PRESENTER_CIDR:-127.0.0.1/32}"; else cidr="$(presenter_cidr)" || exit 1; fi
      printf '%s\n' "-var=stack=$STACK" "-var=aws_profile=" "-var=owner=$OWNER" "-var=presenter_cidr=$cidr" \
        "-var=enable_dd_synthetics=$(b dd-synthetics)" "-var=enable_control_center=$(b control-center)";;
    images)    # per-stack ECR repositories; kept by stack-down when KEEP_IMAGES=true
      printf '%s\n' "-var=stack=$STACK" "-var=owner=$OWNER" "-var=aws_profile=" "-var=region=$(aws_region)";;
    account)   # account-wide CCM for AWS: not per stack, workspace "account"
      printf '%s\n' "-var=aws_profile=" "-var=owner=$OWNER";;
    aws)
      if [ "${TF_DESTROY:-}" = 1 ]; then
        # Destroy plans act on state, never on these values. Fixed inert inputs let a rerun of stack-down destroy
        # terraform/aws even after vm or cloud are gone (their outputs no longer exist). Validation-safe placeholders.
        printf '%s\n' "-var=stack=$STACK" "-var=owner=$OWNER" "-var=aws_profile=" "-var=presenter_cidr=${PRESENTER_CIDR:-127.0.0.1/32}" \
          "-var=on_prem_security_group_id=sg-00000000000000000" "-var=on_prem_private_ip=192.0.2.1" "-var=on_prem_public_ip=192.0.2.2" \
          "-var=remote_ec2_instance_type=t4g.micro" "-var=remote_ebs_gb=1" "-var=datadog_synthetics_cidrs=[]" \
          "-var=kafka_bootstrap=destroy.invalid:9092" "-var=schema_registry_url=https://destroy.invalid" \
          "-var=confluent_environment_id=env-destroy" "-var=kafka_cluster_id=lkc-destroy" "-var=flink_compute_pool_id=lfcp-destroy" \
          "-var=enable_releases=$(b releases)" "-var=enable_offers=$(b offers)" "-var=enable_jev=$ENABLE_JEV" \
          "-var=enable_dd_rum=false" "-var=rum_application_id=" "-var=image_tags=$(image_tags_hcl destroy)"
        return 0
      fi
      local cidr vm_instance vm_sg vm_private_ip vm_public_ip vm_cost_inputs vm_instance_type vm_root_ebs_gb cloud_env cloud_cluster cloud_bootstrap cloud_sr cloud_flink synthetics_cidrs rum_enabled rum_application_id
      cidr="$(presenter_cidr)" || exit 1   # vars_of runs in $(...) || die: set -e is off here, so check by hand
      vm_instance="$(tf_out vm instance_id)" || die "could not read VM instance_id for terraform/aws"
      vm_sg="$(aws ec2 describe-instances --instance-ids "$vm_instance" --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text --region "$(aws_region)")" || die "could not read VM security-group ID for terraform/aws"
      vm_private_ip="$(aws ec2 describe-instances --instance-ids "$vm_instance" --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text --region "$(aws_region)")" || die "could not read VM private IP for terraform/aws"
      vm_public_ip="$(tf_out vm public_ip)" || die "could not read VM public IP for terraform/aws"
      vm_cost_inputs="$(remote_vm_cost_inputs)" || die "could not parse VM env_file for terraform/aws cost meter"
      [[ "$vm_cost_inputs" == *$'\n'* ]] || die "malformed VM cost input: expected instance type and root EBS size"
      vm_instance_type="${vm_cost_inputs%%$'\n'*}"
      vm_root_ebs_gb="${vm_cost_inputs#*$'\n'}"
      cloud_env="$(tf_out cloud environment_id)" || die "could not read cloud environment_id for terraform/aws"
      cloud_cluster="$(tf_out cloud kafka_cluster_id)" || die "could not read cloud kafka_cluster_id for terraform/aws"
      cloud_bootstrap="$(tf_out cloud kafka_bootstrap)" || die "could not read cloud kafka_bootstrap for terraform/aws"
      cloud_sr="$(tf_out cloud schema_registry_url)" || die "could not read cloud schema_registry_url for terraform/aws"
      cloud_flink="$(tf_out cloud flink_compute_pool_id)" || die "could not read cloud flink_compute_pool_id for terraform/aws"
      synthetics_cidrs="[]"
      if has dd-synthetics; then synthetics_cidrs="$(tf_out_json vm dd_synthetics_cidrs)" || die "could not read VM output dd_synthetics_cidrs for terraform/aws"; fi
      rum_enabled=false
      rum_application_id=""
      if has dd-rum && { [ -f "$DD_APPLIED" ] || [ -f "$DD_RUM_APPLIED" ]; }; then
        rum_enabled=true
        rum_application_id="$(tf_out datadog rum_application_id)" || die "could not read Datadog RUM application ID for terraform/aws"
        [ -n "$rum_application_id" ] && [ "$rum_application_id" != null ] || die "Datadog RUM application ID is missing while dd-rum is enabled"
      fi
      printf '%s\n' "-var=stack=$STACK" "-var=owner=$OWNER" "-var=aws_profile=" "-var=presenter_cidr=$cidr" \
        "-var=on_prem_security_group_id=$vm_sg" "-var=on_prem_private_ip=$vm_private_ip" "-var=on_prem_public_ip=$vm_public_ip" \
        "-var=remote_ec2_instance_type=$vm_instance_type" "-var=remote_ebs_gb=$vm_root_ebs_gb" \
        "-var=datadog_synthetics_cidrs=$synthetics_cidrs" \
        "-var=kafka_bootstrap=$cloud_bootstrap" "-var=schema_registry_url=$cloud_sr" \
        "-var=confluent_environment_id=$cloud_env" "-var=kafka_cluster_id=$cloud_cluster" \
        "-var=flink_compute_pool_id=$cloud_flink" "-var=enable_releases=$(b releases)" "-var=enable_offers=$(b offers)" "-var=enable_jev=$ENABLE_JEV" \
        "-var=enable_dd_rum=$rum_enabled" "-var=rum_application_id=$rum_application_id" "-var=image_tags=$(image_tags_hcl)";;
    datadog)
      printf '%s\n' "-var=stack=$STACK" "-var=enable_releases=$(b releases)" "-var=enable_restock=$(b restock)" \
        "-var=enable_offers=$(b offers)" "-var=enable_dd_streams=$(b dd-streams)" \
        "-var=enable_dd_synthetics=$(b dd-synthetics)" "-var=enable_dd_rum=$(b dd-rum)"
      hybrid_enabled && printf '%s\n' "-var=enable_fargate=true"
      if has dd-synthetics && [ "${DATADOG_RUM_ONLY:-}" = 1 ]; then
        :   # targeted RUM-only apply before terraform/aws exists: the Synthetics tests are not planned, the URL stays null
      elif has dd-synthetics && [ "${TF_DESTROY:-}" = 1 ]; then
        printf '%s\n' "-var=ingress_base_url=http://destroy.invalid"   # destroy acts on state; aws/vm may already be gone
      elif has dd-synthetics; then
        if hybrid_enabled; then
          tf_has_state aws || die "layer dd-synthetics needs the AWS ALB (terraform/aws) of stack $STACK"
          local synth_url; synth_url="$(tf_out aws alb_url)" || die "terraform/aws output alb_url missing (dd-synthetics target)"
          printf '%s\n' "-var=ingress_base_url=$synth_url"
        else
          tf_has_state vm || die "layer dd-synthetics needs the VM (terraform/vm) of stack $STACK"
          local synth_url; synth_url="$(tf_out vm ingress_base_url)" || die "terraform/vm output ingress_base_url missing (dd-synthetics target)"
          printf '%s\n' "-var=ingress_base_url=$synth_url"
        fi
      fi
      # The demo home links the stack's shop and control panel; best effort, an empty value renders a hint instead.
      if [ "${TF_DESTROY:-}" != 1 ] && [ "${DATADOG_RUM_ONLY:-}" != 1 ]; then
        local shop_url=""
        if hybrid_enabled; then
          if tf_has_state aws; then shop_url="$(tf_out aws alb_url)" || die "terraform/aws output alb_url missing (shop link of the demo home)"; fi
        elif tf_has_state vm; then
          shop_url="$(tf_out vm ingress_base_url)" || die "terraform/vm output ingress_base_url missing (shop link of the demo home)"
        fi
        printf '%s\n' "-var=shop_url=$shop_url"
      fi;;
  esac
}
datadog_streams_env() { # the datadog dir reads the Confluent integration key from TF_VAR_* (never from files)
  if has dd-streams && [ "${TF_DESTROY:-}" = 1 ]; then
    # Destroy deletes by ID from state and never sends these; terraform/cloud may already be gone on a rerun.
    TF_VAR_confluent_api_key=destroy-placeholder TF_VAR_confluent_api_secret=destroy-placeholder TF_VAR_confluent_cluster_id=lkc-destroy
    export TF_VAR_confluent_api_key TF_VAR_confluent_api_secret TF_VAR_confluent_cluster_id
  elif has dd-streams; then
    TF_VAR_confluent_api_key="$(tf_out cloud datadog_confluent_api_key)" || die "cloud output datadog_confluent_api_key missing (apply terraform/cloud with dd-streams on first)"
    TF_VAR_confluent_api_secret="$(tf_out cloud datadog_confluent_api_secret)" || die "cloud output datadog_confluent_api_secret missing"
    TF_VAR_confluent_cluster_id="$(tf_out cloud kafka_cluster_id)" || die "cloud output kafka_cluster_id missing"
    export TF_VAR_confluent_api_key TF_VAR_confluent_api_secret TF_VAR_confluent_cluster_id
  fi
}

confirm() { # confirm <question>
  [ "${CONFIRM:-}" = yes ] && { echo "   (CONFIRM=yes: $1 -> yes)"; return 0; }
  [ "${YES:-}" = 1 ] && { echo "   (YES=1: $1 -> yes)"; return 0; }
  local a; read -r -p "   $1 Type yes to continue: " a
  [ "$a" = yes ] || { echo "stack.sh: not confirmed: $1" >&2; return 1; }
}

apply_log_has_expired_token() { # apply_log_has_expired_token <terraform stderr file> : true when AWS rejected an expired token
  grep -q 'ExpiredToken' "$1"
}

# Network errors that say nothing about the configuration: a fresh plan after a short wait usually goes through.
# Only these are retried, and only when every Terraform "Error:" of the run is one of them.
TF_TRANSIENT_RE='no such host|connection reset by peer|TLS handshake timeout|RequestLimitExceeded'
TF_RETRY_DELAYS="${TF_RETRY_DELAYS:-10 30}"   # seconds before attempt 2 and attempt 3
first_tf_error() { # first_tf_error <terraform stderr file> : the first "Error:" line, without colours and box characters
  sed $'s/\x1b\\[[0-9;]*m//g' "$1" | awk '/Error:/ { sub(/^.*Error:/, "Error:"); print substr($0, 1, 200); exit }'
}
tf_retry_transient() { # tf_retry_transient <dir> <phase> <attempt> <stderr file> : waits and returns 0 when a retry is due
  local dir="$1" phase="$2" attempt="$3" err="$4" counts delay
  [ "$attempt" -lt 3 ] || return 1
  counts="$(sed $'s/\x1b\\[[0-9;]*m//g' "$err" | awk -v re="$TF_TRANSIENT_RE" '/Error:/ { n++; if ($0 ~ re) t++ } END { print n + 0, t + 0 }')" \
    || die "could not read the terraform errors of terraform/$dir ($err)"
  set -- $counts
  [ "$1" -gt 0 ] && [ "$1" = "$2" ] || return 1
  delay="$(printf '%s\n' $TF_RETRY_DELAYS | sed -n "${attempt}p")"
  [[ "$delay" =~ ^[0-9]+$ ]] || die "TF_RETRY_DELAYS needs two whole numbers of seconds, got '$TF_RETRY_DELAYS'"
  echo "WARNING: terraform $phase in terraform/$dir failed on a transient network error (attempt $attempt/3): $(first_tf_error "$err")" >&2
  echo "WARNING: retrying terraform/$dir with a fresh plan in ${delay}s (attempt $((attempt + 1))/3)" >&2
  sleep "$delay"
}

# TF_REPLACE: space-separated <dir>:<resource address> entries planned as replacements (terraform plan -replace) in
# that dir, for a change the provider cannot apply in place and no replace_triggered_by covers yet, e.g.
# TF_REPLACE='cloud:confluent_flink_statement.dml["sellable-1"]'. Prints one -replace=<address> per line for <dir>,
# or with "others" the entries of the other dirs: tf_apply drops a dir's entries after its first successful apply,
# so a later apply of the same dir in one run does not replace them again. read -a splits without globbing.
tf_replace_args() { # tf_replace_args <dir> [others]
  local dir="$1" others="${2:-}" entries=() e
  [ -n "${TF_REPLACE:-}" ] || return 0
  read -r -a entries <<< "$TF_REPLACE"
  for e in "${entries[@]}"; do
    case "$e" in
      ?*:?*) if [ "${e%%:*}" = "$dir" ]; then [ -n "$others" ] || printf -- '-replace=%s\n' "${e#*:}"; else [ -z "$others" ] || printf '%s\n' "$e"; fi;;
      *) echo "stack.sh: TF_REPLACE entry '$e' is not <dir>:<resource address>, e.g. cloud:confluent_flink_statement.dml[\"sellable-1\"]" >&2; return 1;;
    esac
  done
}

tf_apply() { # tf_apply <dir> [destroy] [attempt]
  local dir="$1" mode="${2:-}" attempt="${3:-1}" plan="$STATE_DIR/$STACK-$1.tfplan" vars=() v plan_args="-input=false"
  local err="$STATE_DIR/$STACK-$1-attempt-$attempt.err"
  load_env; tf_init "$dir"
  [ "$dir" = datadog ] && datadog_streams_env
  local vf; vf="$(vars_of "$dir")" || die "could not build the variables for terraform/$dir"
  while IFS= read -r v; do vars+=("$v"); done <<< "$vf"
  echo "   terraform/$dir workspace $STACK: ${vars[*]}"
  [ "$mode" = destroy ] && plan_args="-destroy -input=false"
  # TF_DESTROY_TARGET: a destroy limited to one resource address (and what depends on it), e.g. the VM alone.
  [ "$mode" = destroy ] && [ -n "${TF_DESTROY_TARGET:-}" ] && plan_args="$plan_args -target=$TF_DESTROY_TARGET"
  local replace_args=() rf r
  if [ "$mode" != destroy ]; then
    rf="$(tf_replace_args "$dir")" || die "TF_REPLACE: see the error above"
    while IFS= read -r r; do [ -z "$r" ] || replace_args+=("$r"); done <<< "$rf"
    [ "${#replace_args[@]}" -eq 0 ] || echo "   terraform/$dir: TF_REPLACE ${replace_args[*]}"
  fi
  # shellcheck disable=SC2086  # plan_args: fixed flags, split on purpose
  if ! tf "$dir" plan $plan_args ${replace_args[@]+"${replace_args[@]}"} -out="$plan" "${vars[@]}" >/dev/null 2> "$err"; then
    cat "$err" >&2
    if [ "$dir" = cloud ] && [ "$mode" != destroy ] && grep -q 'This server does not host this topic-partition' "$err" && [ "$attempt" -lt 3 ]; then
      rm -f "$plan" "$err"
      echo "WARNING: Confluent returned the transient topic-read 404; retrying terraform/cloud with a fresh plan (attempt $((attempt + 1))/3)" >&2
      sleep 20
      tf_apply "$dir" "$mode" "$((attempt + 1))"
      return
    fi
    if tf_retry_transient "$dir" plan "$attempt" "$err"; then
      rm -f "$plan" "$err"
      tf_apply "$dir" "$mode" "$((attempt + 1))"
      return
    fi
    note_cause "terraform/$dir: $(first_tf_error "$err")"
    rm -f "$plan" "$err"
    die "terraform plan${mode:+ -$mode} failed in terraform/$dir (re-run: terraform -chdir=terraform/$dir plan ${vars[*]})"
  fi
  [ ! -s "$err" ] || cat "$err" >&2
  rm -f "$err"
  # Summary only: resource addresses and actions, no attribute values.
  tf "$dir" show -no-color "$plan" | grep -E '^  # .* (will|must) be|^Plan:|^No changes|^Changes to Outputs' | sed 's/^/   /' || true
  if tf "$dir" show -no-color "$plan" | grep -q '^No changes'; then rm -f "$plan"; echo "   terraform/$dir: nothing to do"; return 0; fi
  confirm "Apply this plan to terraform/$dir (stack $STACK)?" || { rm -f "$plan"; exit 1; }
  if ! tf "$dir" apply -input=false "$plan" 2> "$err"; then
    cat "$err" >&2
    if [ "$dir" = cloud ] && [ "$mode" != destroy ] && grep -q 'This server does not host this topic-partition' "$err" && [ "$attempt" -lt 3 ]; then
      rm -f "$plan" "$err"
      echo "WARNING: Confluent returned the transient topic-read 404; retrying terraform/cloud with a fresh plan (attempt $((attempt + 1))/3)" >&2
      sleep 20
      tf_apply "$dir" "$mode" "$((attempt + 1))"
      return
    fi
    if apply_log_has_expired_token "$err"; then
      rm -f "$plan" "$err"
      die "AWS login expired during apply of terraform/$dir: run aws login --profile $AWS_SOURCE_PROFILE, then ./demo create again (stack-down: rerun stack-down); resources being created when it failed are left tainted and are rebuilt, which costs extra minutes"
    fi
    if tf_retry_transient "$dir" "${mode:-apply}" "$attempt" "$err"; then
      rm -f "$plan" "$err"
      tf_apply "$dir" "$mode" "$((attempt + 1))"
      return
    fi
    note_cause "terraform/$dir: $(first_tf_error "$err")"
    rm -f "$plan" "$err"
    die "terraform ${mode:-apply} failed in terraform/$dir"
  fi
  [ ! -s "$err" ] || cat "$err" >&2
  rm -f "$err"
  rm -f "$plan"
  if [ "${#replace_args[@]}" -gt 0 ]; then
    rf="$(tf_replace_args "$dir" others)" || die "TF_REPLACE: see the error above"
    TF_REPLACE="$(printf '%s' "$rf" | tr '\n' ' ')"
  fi
  [ "$dir" = datadog ] && { if [ "$mode" = destroy ]; then rm -f "$DD_APPLIED" "$DD_RUM_APPLIED"; else touch "$DD_APPLIED"; fi; }
  return 0
}

tf_apply_datadog_rum() { # create only the RUM application, before terraform/aws registers the storefront task definition
  # Hybrid + dd-rum: the storefront then gets its RUM settings in its first deployment, and the later full datadog apply
  # finds the application in state (no change). Outputs that depend only on the targeted resource are written.
  local plan="$STATE_DIR/$STACK-datadog-rum.tfplan" vars=() v vf
  load_env; tf_init datadog
  datadog_streams_env
  vf="$(DATADOG_RUM_ONLY=1 vars_of datadog)" || die "could not build the variables for the terraform/datadog RUM apply"
  while IFS= read -r v; do vars+=("$v"); done <<< "$vf"
  echo "   terraform/datadog workspace $STACK: RUM application only (-target)"
  tf datadog plan -input=false -out="$plan" -target=datadog_rum_application.shop "${vars[@]}" >/dev/null \
    || { rm -f "$plan"; die "terraform datadog RUM plan failed"; }
  tf datadog show -no-color "$plan" | grep -E '^  # .* (will|must) be|^Plan:|^No changes|^Changes to Outputs' | sed 's/^/   /' || true
  confirm "Apply the Datadog RUM application plan (stack $STACK)?" || { rm -f "$plan"; exit 1; }
  tf datadog apply -input=false "$plan" || { rm -f "$plan"; die "terraform datadog RUM apply failed"; }
  rm -f "$plan"
  tf_out datadog rum_application_id >/dev/null || die "terraform/datadog output rum_application_id missing after the RUM apply"
  touch "$DD_RUM_APPLIED"
}

tf_bootstrap_cloud() { # make apply-time cluster CRNs known before the full new-stack plan
  local plan="$STATE_DIR/$STACK-cloud-bootstrap.tfplan" vars=() v vf
  load_env; tf_init cloud
  tf cloud state show confluent_kafka_cluster.main >/dev/null 2>&1 && return 0
  vf="$(vars_of cloud)" || die "could not build the variables for terraform/cloud bootstrap"
  while IFS= read -r v; do vars+=("$v"); done <<< "$vf"
  echo "   terraform/cloud workspace $STACK: bootstrap environment + Kafka cluster"
  tf cloud plan -input=false -out="$plan" \
    -target=confluent_environment.main -target=confluent_kafka_cluster.main "${vars[@]}" >/dev/null \
    || { rm -f "$plan"; die "terraform cloud bootstrap plan failed"; }
  tf cloud show -no-color "$plan" | grep -E '^  # .* (will|must) be|^Plan:|^No changes|^Changes to Outputs' | sed 's/^/   /' || true
  confirm "Apply the cloud bootstrap plan (stack $STACK)?" || { rm -f "$plan"; exit 1; }
  tf cloud apply -input=false "$plan" || { rm -f "$plan"; die "terraform cloud bootstrap apply failed"; }
  rm -f "$plan"
}

write_env_file() { # <repo>/.env.cloud-<stack> = cloud + vm env_file (+ datadog env_file: RUM ids), mode 600, never printed
  local tmp="$ENV_CLOUD.tmp"
  tf_out cloud env_file > "$tmp" || { rm -f "$tmp"; die "terraform/cloud output env_file failed (workspace $STACK)"; }
  echo >> "$tmp"
  tf_out vm env_file >> "$tmp" || { rm -f "$tmp"; die "terraform/vm output env_file failed (workspace $STACK)"; }
  echo >> "$tmp"
  local key_tmp secret_tmp
  key_tmp="$tmp.cost-key"; secret_tmp="$tmp.cost-secret"
  tf_out account cost_meter_confluent_api_key > "$key_tmp" || { rm -f "$tmp" "$key_tmp"; die "terraform/account output cost_meter_confluent_api_key failed"; }
  tf_out account cost_meter_confluent_api_secret > "$secret_tmp" || { rm -f "$tmp" "$key_tmp" "$secret_tmp"; die "terraform/account output cost_meter_confluent_api_secret failed"; }
  printf 'COST_METER_API_KEY=' >> "$tmp"; cat "$key_tmp" >> "$tmp"; echo >> "$tmp"
  printf 'COST_METER_API_SECRET=' >> "$tmp"; cat "$secret_tmp" >> "$tmp"; echo >> "$tmp"
  rm -f "$key_tmp" "$secret_tmp"
  if [ -f "$DD_APPLIED" ]; then
    tf_out datadog env_file >> "$tmp" || { rm -f "$tmp"; die "terraform/datadog output env_file failed (workspace $STACK)"; }
    echo >> "$tmp"
  elif [ -f "$DD_RUM_APPLIED" ] && has dd-rum; then   # early RUM-only apply: the same two lines env_file writes
    printf 'DD_RUM_APPLICATION_ID=' >> "$tmp"
    tf_out datadog rum_application_id >> "$tmp" || { rm -f "$tmp"; die "terraform/datadog output rum_application_id failed (workspace $STACK)"; }
    printf '\nDD_RUM_CLIENT_TOKEN=' >> "$tmp"
    tf_out datadog rum_client_token >> "$tmp" || { rm -f "$tmp"; die "terraform/datadog output rum_client_token failed (workspace $STACK)"; }
    echo >> "$tmp"
  fi
  mv "$tmp" "$ENV_CLOUD" || die "could not write $ENV_CLOUD"
  chmod 600 "$ENV_CLOUD" || die "could not chmod $ENV_CLOUD"
  echo "   wrote $ENV_CLOUD (mode 600, values not shown)"
  if hybrid_enabled && tf_has_state aws; then
    # A failed first apply leaves resources in state but no outputs; stack-up writes the endpoints after terraform aws.
    if aws_out alb_url >/dev/null 2>&1; then write_hybrid_endpoints
    else echo "   terraform/aws has no outputs yet (earlier apply did not finish): hybrid endpoints written after terraform aws"; fi
  fi
}
write_hybrid_endpoints() {
  local tmp="$ENV_CLOUD.tmp" alb redis cluster rule target_groups tg100 tg110 tg120
  alb="$(aws_out alb_url)" || die "terraform/aws output alb_url failed (workspace $STACK)"
  redis="$(aws_out redis_endpoint)" || die "terraform/aws output redis_endpoint failed (workspace $STACK)"
  cluster="$(aws_cluster)"
  rule="$(aws_out alb_inventory_rule_arn)" || die "terraform/aws output alb_inventory_rule_arn failed (workspace $STACK)"
  tf_workspace aws
  target_groups="$(tf aws output -json inventory_target_group_arns)" || die "terraform/aws output inventory_target_group_arns failed"
  tg100="$(printf '%s' "$target_groups" | jq -er '."100"')" || die "inventory target group 100 output missing"
  tg110="$(printf '%s' "$target_groups" | jq -er '."110"')" || die "inventory target group 110 output missing"
  tg120="$(printf '%s' "$target_groups" | jq -er '."120"')" || die "inventory target group 120 output missing"
  awk '!/^(HYBRID_ONLINE_URL|ELASTICACHE_REDIS_URL|ALB_INVENTORY_RULE_ARN|ALB_INVENTORY_(100|110|120)_TARGET_GROUP_ARN|COST_ECS_CLUSTER_NAME|COST_REGION|COST_ALB_COUNT|COST_ELASTICACHE_NODE_TYPE)=/' "$ENV_CLOUD" > "$tmp" || { rm -f "$tmp"; die "could not prepare $ENV_CLOUD for hybrid endpoints"; }
  printf 'HYBRID_ONLINE_URL=%s\nELASTICACHE_REDIS_URL=redis://%s:6379/0\nALB_INVENTORY_RULE_ARN=%s\nALB_INVENTORY_100_TARGET_GROUP_ARN=%s\nALB_INVENTORY_110_TARGET_GROUP_ARN=%s\nALB_INVENTORY_120_TARGET_GROUP_ARN=%s\nCOST_ECS_CLUSTER_NAME=%s\nCOST_REGION=%s\nCOST_ALB_COUNT=1\nCOST_ELASTICACHE_NODE_TYPE=cache.t4g.small\n' "$alb" "$redis" "$rule" "$tg100" "$tg110" "$tg120" "$cluster" "$(aws_region)" >> "$tmp"
  mv "$tmp" "$ENV_CLOUD" || die "could not update $ENV_CLOUD with hybrid endpoints"
  chmod 600 "$ENV_CLOUD" || die "could not chmod $ENV_CLOUD"
  echo "   wrote hybrid online endpoints to $ENV_CLOUD (mode 600, values not shown)"
}

# --- AWS-native online side ---------------------------------------------------------------------
# The AWS dir is optional: rehearsal keeps the existing single-VM compose path.  The hybrid stack
# uses the VM only as an ARM64 build/push host; application traffic runs on ECS.
hybrid_enabled() { [ "$TOPOLOGY" = hybrid ] && [ -d "$AWS_TF" ]; }
aws_out() { tf_out aws "$1"; }
aws_region() { printf '%s\n' "${AWS_REGION:-eu-west-1}"; }
aws_cluster() { aws_out ecs_cluster_name 2>/dev/null || printf 'dd-demo-%s\n' "$STACK"; }
aws_alb_url() {
  aws_out alb_url 2>/dev/null || aws_out alb_dns_name 2>/dev/null | sed 's#^#http://#';
}

keep_images() { # true when demo.yaml keep_images is true (KEEP_IMAGES from the Makefile); anything but true/false fails
  case "${KEEP_IMAGES:-false}" in
    true) return 0;;
    false) return 1;;
    *) die "KEEP_IMAGES must be true or false (demo.yaml keep_images), got '${KEEP_IMAGES}'";;
  esac
}
refuse_legacy_ecr_state() { # stacks created before the per-stack image repositories hold the same repository names in terraform/aws
  local state
  tf_has_state aws || { echo "   terraform/aws has no state: nothing to check"; return 0; }
  state="$(tf aws state list)" || die "could not list terraform/aws state while checking for old ECR repositories"
  if printf '%s\n' "$state" | grep -q '^aws_ecr_repository\.'; then
    die "terraform/aws of stack $STACK still owns its ECR repositories (older layout); terraform/images would create the same names. Run ./demo destroy (it deletes them), then ./demo create"
  fi
  echo "   no ECR repository in terraform/aws state"
}

# Legacy only (stacks created before the per-stack image repositories): terraform/aws owned the repositories. New stacks keep them in
# terraform/images, which has force_delete and is destroyed by down_images.
delete_ecr_repositories() {
  local repository region repository_urls repositories err state
  region="$(aws_region)"
  if ! repository_urls="$(tf_out_json aws ecr_repositories)"; then
    # A rerun after a partial destroy may have lost the output; that is fine only when no repository is left in state.
    state="$(tf aws state list)" || die "could not list terraform/aws state while looking for ECR repositories"
    if printf '%s\n' "$state" | grep -q '^aws_ecr_repository\.'; then
      die "could not read ECR repository output before stack-down, but terraform/aws state still holds ECR repositories"
    fi
    echo "   no ECR repositories left in terraform/aws state"
    return 0
  fi
  repositories="$(printf '%s' "$repository_urls" | jq -r 'to_entries[] | .value | sub("^[^/]+/"; "")')" \
    || die "could not parse the ECR repository output of terraform/aws"
  for repository in $repositories; do
    # Only "repository not found" means skip; any other error (expired login, denied, network) fails the aws layer.
    if err="$(aws ecr describe-repositories --repository-names "$repository" --region "$region" 2>&1 >/dev/null)"; then
      aws ecr delete-repository --repository-name "$repository" --force --region "$region" >/dev/null \
        || die "could not force-delete populated ECR repository $repository"
      echo "   deleted ECR repository $repository (including images)"
    elif printf '%s' "$err" | grep -q 'RepositoryNotFoundException'; then
      echo "   ECR repository $repository already gone"
    else
      die "could not check ECR repository $repository (AWS error, not a missing repository): $err"
    fi
  done
}

# Stack service accounts deliberately cannot delete Schema Registry subjects.
# The containing environment removes them, so detach only schema resources from
# state immediately before the approved environment destroy.
detach_cloud_schemas_before_destroy() {
  local resource state
  tf_workspace cloud
  state="$(tf cloud state list)" || die "could not list terraform/cloud state before schema detach"
  while IFS= read -r resource; do
    case "$resource" in
      confluent_schema.*)
        tf cloud state rm "$resource" >/dev/null || die "could not detach $resource before Confluent environment destroy"
        echo "   detached $resource (subject is removed with the Confluent environment)";;
    esac
  done <<< "$state"
}

# Secret values are read only at deploy time, never echoed or passed as CLI output.  Keep this
# allowlist narrow: endpoints and IDs from .env.cloud are not secrets and must not enter SSM.
ssm_secret_key() {
  case "$1" in
    DD_API_KEY|PROJECTOR_KAFKA_API_KEY|PROJECTOR_KAFKA_API_SECRET|PROJECTOR_SR_API_KEY|PROJECTOR_SR_API_SECRET|STOREFRONT_KAFKA_API_KEY|STOREFRONT_KAFKA_API_SECRET|STOREFRONT_SR_API_KEY|STOREFRONT_SR_API_SECRET|OFFERS_KAFKA_API_KEY|OFFERS_KAFKA_API_SECRET|OFFERS_SR_API_KEY|OFFERS_SR_API_SECRET|DEMO_CONTROL_KAFKA_API_KEY|DEMO_CONTROL_KAFKA_API_SECRET|DEMO_CONTROL_SR_API_KEY|DEMO_CONTROL_SR_API_SECRET|CONTROL_PASSWORD|SCENARIO_API_TOKEN|PG_WRITER_PASSWORD|PG_PROCUREMENT_PASSWORD|COST_METER_API_KEY|COST_METER_API_SECRET|JEV_API_KEY|DD_RUM_CLIENT_TOKEN) return 0;;
    *) return 1;;
  esac
}
SSM_CHANGED=0   # set by sync_ssm_secrets: 1 when the synced values differ from the previous sync of this stack
sync_ssm_secrets() {
  local file line key value count=0 name digest_input="" digest old=""
  # Values may live in any of the three untracked deploy-time env files. The
  # exact allowlist below is still the security boundary for what reaches SSM.
  for file in "$ENV_CLOUD" "$ENV_DIR/.env.secrets" "$ENV_DIR/.env"; do
    [ -f "$file" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|\#*) continue;; esac
      key="${line%%=*}"; value="${line#*=}"
      ssm_secret_key "$key" || continue
      # Cost-meter credentials come only from account Terraform's .env.cloud output;
      # stale .env.secrets lines must not override the account identity.
      if [ "$file" != "$ENV_CLOUD" ] && { [ "$key" = COST_METER_API_KEY ] || [ "$key" = COST_METER_API_SECRET ]; }; then continue; fi
      [ "$key" != "$line" ] || die "malformed secret entry '$key' in $file"
      name="/dd-demo/$STACK/$key"
      aws ssm put-parameter --name "$name" --type SecureString --value "$value" --overwrite \
        --region "$(aws_region)" >/dev/null || die "could not sync SSM parameter $name"
      count=$((count + 1))
      digest_input="$digest_input$name=$value"$'\n'
    done < "$file"
  done
  echo "   synced $count secret values to SSM SecureString (values not shown)"
  # Tasks read SSM only when they start. A one-way digest (mode 600, never printed) tells roll_out_images whether the
  # running tasks hold older values; no earlier digest means no task started before this sync (or a pre-0031 stack).
  digest="$(printf '%s' "$digest_input" | sha256_hex)" || die "could not digest the synced secret values"
  [ -f "$SSM_DIGEST_FILE" ] && old="$(cat "$SSM_DIGEST_FILE")"
  SSM_CHANGED=0
  if [ -n "$old" ] && [ "$old" != "$digest" ]; then SSM_CHANGED=1; echo "   secret values changed since the last sync: running tasks restart in the roll-out"; fi
  printf '%s\n' "$digest" > "$SSM_DIGEST_FILE" || die "could not write $SSM_DIGEST_FILE"
}

delete_stack_ssm_parameters() {
  local path names_raw name batch_size=10 batch=() count=0
  path="/dd-demo/$STACK/"
  names_raw="$(aws ssm describe-parameters --parameter-filters "Key=Path,Option=Recursive,Values=$path" --query 'Parameters[].Name' --output text --region "$(aws_region)")" \
    || die "could not list SSM parameter names under $path"
  [ -n "$names_raw" ] && [ "$names_raw" != "None" ] || { echo "   no SSM parameters under $path"; return 0; }
  for name in $names_raw; do
    case "$name" in "$path"*) ;; *) die "refusing out-of-scope SSM parameter name '$name'";; esac
    batch+=("$name")
    if [ "${#batch[@]}" -eq "$batch_size" ]; then
      aws ssm delete-parameters --names "${batch[@]}" --region "$(aws_region)" >/dev/null \
        || die "could not delete SSM parameters under $path"
      count=$((count + ${#batch[@]})); batch=()
    fi
  done
  if [ "${#batch[@]}" -gt 0 ]; then
    aws ssm delete-parameters --names "${batch[@]}" --region "$(aws_region)" >/dev/null \
      || die "could not delete SSM parameters under $path"
    count=$((count + ${#batch[@]}))
  fi
  echo "   deleted $count SSM parameters under $path (names only)"
}

# --- images: content tags, reuse from ECR, one builder function ------------------------------------
# Every image is tagged c-<hash of its build inputs>. When ECR already holds that tag the build is skipped: ECS pulls
# it, and the VM pulls it instead of rebuilding. Only the missing images go to build_and_push_images.
app_images() {
  # Keep this list explicit so infrastructure-only images are never pushed accidentally.
  printf '%s\n' inventory-api storefront stock-projector offer-worker demo-control cost-meter
}
log_router_image() {
  printf '%s\n' log-router
}
vm_images() { # images the on-prem VM runs through compose (built locally today, pulled from ECR when unchanged)
  printf '%s\n' connect jr freshness-probe scenario smoke supplier-sim
}
ecs_images() { app_images; log_router_image; }
all_images() { ecs_images; vm_images; }   # must match local.images in terraform/images/main.tf
is_vm_image() { case " $(vm_images | tr '\n' ' ') " in *" $1 "*) return 0;; *) return 1;; esac; }
ecs_services() {
  printf '%s\n' inventory-api-100 inventory-api-110 inventory-api-120 storefront stock-projector offer-worker demo-control cost-meter
}
ecs_service_image() { case "$1" in inventory-api-*) echo inventory-api;; *) echo "$1";; esac; }
compose_service_of() { # the compose service whose build: section builds <image> (log-router has none)
  case "$1" in
    inventory-api) echo inventory-api-100;;
    jr) echo jr-sales-s01;;
    log-router) echo "";;
    *) echo "$1";;
  esac
}
local_image() { printf 'dd-%s:%s\n' "$1" "${IMAGE_TAG:-dev}"; }   # the name compose gives the image (compose.yaml image:)

image_inputs() { # image_inputs <image> : absolute paths of the build inputs (the Dockerfile COPY sources, as a superset)
  local o="$OVERLAY"
  case "$1" in
    inventory-api)   printf '%s\n' "$o/inventory-api";;
    storefront)      printf '%s\n' "$o/storefront" "$o/contracts/avro";;
    stock-projector) printf '%s\n' "$o/stock-projector" "$o/contracts/avro";;
    offer-worker)    printf '%s\n' "$o/offer-worker" "$o/contracts/avro" "$o/storefront/backend/app/products.json";;
    demo-control)    printf '%s\n' "$o/demo-control" "$o/contracts" "$o/scenario/scenario";;
    cost-meter|log-router|connect|freshness-probe|scenario|smoke|supplier-sim) printf '%s\n' "$o/$1";;
    jr)
      : "${JR_CONTEXT:?stack.sh: JR_CONTEXT must be set (the Makefile exports it)}"
      : "${JR_DOCKERFILE:?stack.sh: JR_DOCKERFILE must be set (the Makefile exports it)}"
      local ctx dockerfile_dir
      ctx="$(cd "$o/compose/$JR_CONTEXT" && pwd)" || { echo "stack.sh: jr build context $o/compose/$JR_CONTEXT is missing" >&2; return 1; }
      dockerfile_dir="$(cd "$ctx/$(dirname "$JR_DOCKERFILE")" && pwd)" || { echo "stack.sh: jr Dockerfile directory of $JR_DOCKERFILE is missing" >&2; return 1; }
      printf '%s\n' "$ctx" "$dockerfile_dir/$(basename "$JR_DOCKERFILE")";;
    *) echo "stack.sh: image_inputs: unknown image '$1'" >&2; return 1;;
  esac
}
image_build_args() { # build arguments that change the image, as in compose.yaml
  case "$1" in inventory-api) printf 'arg CATALOGUE_PRODUCTS=%s\n' "${CATALOGUE_PRODUCTS:-3500}";; esac
}
sha256_hex() { if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi; }
image_input_manifest() { # image_input_manifest <image> : "<blob> <path>" for every input file (tracked or untracked, not ignored)
  local image="$1" path label list files existing blobs f
  printf 'scheme 1 image %s platform linux/arm64\n' "$image"
  image_build_args "$image"
  list="$(image_inputs "$image")" || return 1
  for path in $list; do
    label="${path#"$ROOT"/}"
    if [ -f "$path" ]; then
      blobs="$(git hash-object -- "$path")" || { echo "stack.sh: git hash-object failed for $path" >&2; return 1; }
      printf '%s %s\n' "$blobs" "$label"
    elif [ -d "$path" ]; then
      files="$(cd "$path" && git ls-files -co --exclude-standard -- .)" \
        || { echo "stack.sh: git ls-files failed in $path (content tags need a git checkout of the repository)" >&2; return 1; }
      existing=""
      while IFS= read -r f; do
        if [ -n "$f" ] && [ -f "$path/$f" ]; then existing="$existing$f"$'\n'; fi   # deleted tracked files are skipped
      done <<< "$files"
      [ -n "$existing" ] || { echo "stack.sh: no build input files under $path for image $image" >&2; return 1; }
      blobs="$(printf '%s' "$existing" | sed "s#^#$path/#" | git hash-object --stdin-paths)" \
        || { echo "stack.sh: git hash-object failed under $path" >&2; return 1; }
      paste -d' ' <(printf '%s\n' "$blobs") <(printf '%s' "$existing" | sed "s#^#$label/#")
    else
      echo "stack.sh: build input $path of image $image is missing" >&2; return 1
    fi
  done
}
image_content_tag() { # image_content_tag <image> : c-<20 hex>; changes exactly when a build input changes
  local manifest
  manifest="$(image_input_manifest "$1")" || return 1
  printf 'c-%s\n' "$(printf '%s\n' "$manifest" | LC_ALL=C sort | sha256_hex | cut -c1-20)"
}
write_image_tags() { # computes every tag once per stack-up; terraform/aws and the image job read this file
  local tmp="$IMAGE_TAGS_FILE.tmp" image tag
  : > "$tmp" || die "could not write $tmp"
  for image in $(all_images); do
    tag="$(image_content_tag "$image")" || { rm -f "$tmp"; die "could not compute the content tag of image $image"; }
    [[ "$tag" =~ ^c-[0-9a-f]{20}$ ]] || { rm -f "$tmp"; die "malformed content tag '$tag' for image $image"; }
    printf '%s=%s\n' "$image" "$tag" >> "$tmp"
    echo "   $image $tag"
  done
  mv "$tmp" "$IMAGE_TAGS_FILE" || die "could not install $IMAGE_TAGS_FILE"
}
image_tag() { # image_tag <image> : the tag of the last stack-up (the one terraform/aws deploys)
  local tag
  [ -f "$IMAGE_TAGS_FILE" ] || die "$IMAGE_TAGS_FILE is missing: the image tags are written by stack-up (run ./demo create)"
  tag="$(sed -n "s/^$1=//p" "$IMAGE_TAGS_FILE")"
  [ -n "$tag" ] || die "no tag for image $1 in $IMAGE_TAGS_FILE (rerun ./demo create)"
  printf '%s\n' "$tag"
}
image_tags_hcl() { # image_tags_hcl [destroy] : {"inventory-api"="c-...",...} for -var=image_tags
  local image out="" tag
  for image in $(ecs_images); do
    if [ "${1:-}" = destroy ]; then tag=destroy; else tag="$(image_tag "$image")" || return 1; fi
    out="$out\"$image\"=\"$tag\","
  done
  echo "{${out%,}}"
}
image_repo_url() { # image_repo_url <image> : from IMAGE_REPOS_JSON (terraform/images output, read once)
  local url
  url="$(printf '%s' "$IMAGE_REPOS_JSON" | jq -r --arg image "$1" '.[$image] // empty')" \
    || die "could not parse the repository map of terraform/images"
  [ -n "$url" ] || die "terraform/images has no ECR repository for image $1 (add it to local.images)"
  printf '%s\n' "$url"
}
ecr_has_tag() { # ecr_has_tag <image> <tag> : 0 present, 1 absent; any other AWS error fails loudly
  local err
  if err="$(aws ecr describe-images --repository-name "dd-demo-$STACK/$1" --image-ids "imageTag=$2" \
      --region "$(aws_region)" 2>&1 >/dev/null)"; then return 0; fi
  if printf '%s' "$err" | grep -q 'ImageNotFoundException'; then return 1; fi
  die "could not check image $1:$2 in ECR (AWS error, not a missing image): $err"
}
ecr_login() {
  local registry
  registry="$(image_repo_url inventory-api | cut -d/ -f1)"
  aws ecr get-login-password --region "$(aws_region)" | \
    docker --context "$CTX" login --username AWS --password-stdin "$registry" >/dev/null \
    || die "could not authenticate the VM's Docker client to ECR"
}
pull_vm_images() { # pull_vm_images <image...> : unchanged VM images come from ECR, tagged with their compose name
  local image repo tag pids=() names=() i failed=""
  for image in "$@"; do
    repo="$(image_repo_url "$image")"; tag="$(image_tag "$image")"
    ( docker --context "$CTX" pull -q "$repo:$tag" >/dev/null \
        && docker --context "$CTX" tag "$repo:$tag" "$(local_image "$image")" ) &
    pids+=("$!"); names+=("$image")
  done
  i=0
  while [ "$i" -lt "${#pids[@]}" ]; do
    if wait "${pids[$i]}"; then echo "   pulled ${names[$i]}:$(image_tag "${names[$i]}") from ECR (as $(local_image "${names[$i]}"))"
    else failed="$failed ${names[$i]}"; fi
    i=$((i + 1))
  done
  [ -z "$failed" ] || die "could not pull from ECR:$failed (docker --context $CTX pull <repo>:<tag>)"
}

# The image builder: builds the given images and pushes each as <repo>:<content tag>. Today it builds on the stack's
# ARM64 VM. A different builder (for example AWS CodeBuild) can replace this one function: the tags, the ECR check,
# the repositories and the task-definition tags do not depend on where the build runs.
build_and_push_images() { # build_and_push_images <image...>
  local image services=() repo tag build_log_router=0
  for image in "$@"; do
    if [ "$image" = "$(log_router_image)" ]; then build_log_router=1; else services+=("$(compose_service_of "$image")"); fi
  done
  if [ "${#services[@]}" -gt 0 ]; then
    # Build only: the runtime-only endpoints from terraform/aws may not exist yet (this runs while it applies).
    # shellcheck disable=SC2086
    env ELASTICACHE_REDIS_URL=redis://build.invalid:6379/0 HYBRID_ONLINE_URL=http://build.invalid \
      $DC --profile releases --profile restock --profile offers --profile tools --profile jr --profile control-center \
      --profile cloud-online build "${services[@]}" || die "docker compose build failed for: ${services[*]}"
  fi
  if [ "$build_log_router" = 1 ]; then
    docker --context "$CTX" build --platform linux/arm64 -t "$(local_image log-router)" "$OVERLAY/log-router" >/dev/null \
      || die "could not build ARM64 FireLens log-router image"
  fi
  for image in "$@"; do
    if is_vm_image "$image" && ! keep_images; then
      # Only a later stack would pull it, and with keep_images false the repositories go with this stack.
      echo "   built $image on the VM (not pushed: keep_images is false, nothing would reuse it)"
      continue
    fi
    repo="$(image_repo_url "$image")"; tag="$(image_tag "$image")"
    docker --context "$CTX" tag "$(local_image "$image")" "$repo:$tag" || die "could not tag ARM64 image $image"
    docker --context "$CTX" push "$repo:$tag" >/dev/null || die "could not push ARM64 image $image:$tag to ECR"
    echo "$image" >> "$IMAGES_PUSHED_FILE"
    echo "   pushed $image:$tag to ECR (credentials not printed)"
  done
}

images_ready() { # every image of this stack exists in ECR under its content tag, and every VM image on the VM
  local image tag missing=() pulls=()
  : > "$IMAGES_PUSHED_FILE" || die "could not write $IMAGES_PUSHED_FILE"
  ecr_login
  for image in $(all_images); do
    tag="$(image_tag "$image")"
    if [ "${REBUILD_IMAGES:-}" != 1 ] && ecr_has_tag "$image" "$tag"; then
      if is_vm_image "$image"; then pulls+=("$image"); echo "   $image:$tag skipped: image unchanged (in ECR; pulled for the VM)"
      else echo "   $image:$tag skipped: image unchanged (in ECR)"; fi
    else
      missing+=("$image"); echo "   $image:$tag not in ECR$( [ "${REBUILD_IMAGES:-}" = 1 ] && echo ' (REBUILD_IMAGES=1)'): build and push"
    fi
  done
  if [ "${#pulls[@]}" -gt 0 ]; then pull_vm_images "${pulls[@]}"; fi
  if [ "${#missing[@]}" -gt 0 ]; then build_and_push_images "${missing[@]}"; else echo "   no image to build"; fi
}

IMAGES_PID=""
IMAGES_LOG=""
stop_images_job() { # EXIT trap while the image job runs: a failed foreground step must not leave it building
  if [ -n "$IMAGES_PID" ] && kill -0 "$IMAGES_PID" 2>/dev/null; then
    pkill -TERM -P "$IMAGES_PID" 2>/dev/null || echo "stack.sh: note: no child process of the image job to stop" >&2
    kill -TERM "$IMAGES_PID" 2>/dev/null || echo "stack.sh: note: the image job had already ended" >&2
    echo "stack.sh: stopped the background image job (its log: $IMAGES_LOG)" >&2
  fi
}
start_images_job() { # runs images_ready in the background; terraform aws applies meanwhile (they are independent)
  IMAGE_REPOS_JSON="$(tf_out_json images repositories)" || die "could not read the ECR repositories of terraform/images"
  export IMAGE_REPOS_JSON
  IMAGES_LOG="$LOG_DIR/$STACK-images-$(date +%Y%m%d-%H%M%S).log"
  mkdir -p "$LOG_DIR" || die "could not create $LOG_DIR"
  : > "$IMAGES_LOG" || die "could not create $IMAGES_LOG"
  chmod 600 "$IMAGES_LOG" || die "could not protect $IMAGES_LOG"
  ( images_ready ) > "$IMAGES_LOG" 2>&1 &
  IMAGES_PID=$!
  trap stop_images_job EXIT
  echo "   image job started (pid $IMAGES_PID); its log is copied here when it ends: $IMAGES_LOG"
}
wait_images_job() {
  local status=0
  [ -n "$IMAGES_PID" ] || die "wait_images_job: no image job was started"
  wait "$IMAGES_PID" || status=$?
  IMAGES_PID=""
  trap - EXIT
  sed 's/^/   | /' "$IMAGES_LOG" || die "could not read $IMAGES_LOG"
  [ "$status" = 0 ] || die "the image job failed (exit $status): see the '|' lines above or $IMAGES_LOG"
}

wait_ecs_services() { # wait_ecs_services <service...> : one services-stable wait per 10 services (the API limit)
  local cluster service batch=() names=()
  cluster="$(aws_cluster)"
  for service in "$@"; do names+=("dd-demo-$STACK-$service"); done
  while [ "${#names[@]}" -gt 0 ]; do
    batch=("${names[@]:0:10}")
    names=("${names[@]:10}")
    aws ecs wait services-stable --cluster "$cluster" --services "${batch[@]}" --region "$(aws_region)" \
      || die "ECS services did not reach steady state: ${batch[*]} (aws ecs describe-services --cluster $cluster --services <name>)"
  done
  echo "   ECS services reached steady state: $* (cluster $cluster)"
}
roll_out_images() { # new deployment only where this run pushed a new image (or secrets changed); then wait for all
  local cluster service image forced=""
  cluster="$(aws_cluster)"
  [ -f "$IMAGES_PUSHED_FILE" ] || die "$IMAGES_PUSHED_FILE is missing: the image job did not run"
  for service in $(ecs_services); do
    image="$(ecs_service_image "$service")"
    # A task that started before the push failed to pull and backs off; a new deployment starts it now.
    if [ "$SSM_CHANGED" = 1 ] || grep -qx -e "$image" -e "$(log_router_image)" "$IMAGES_PUSHED_FILE"; then
      aws ecs update-service --cluster "$cluster" --service "dd-demo-$STACK-$service" --force-new-deployment \
        --region "$(aws_region)" >/dev/null || die "could not force ECS deployment for dd-demo-$STACK-$service"
      forced="$forced $service"
    fi
  done
  if [ -n "$forced" ]; then echo "   new deployment forced (image pushed in this run, or secrets changed):$forced"
  else echo "   no image changed: no forced deployment (ECS runs the task definitions Terraform registered)"; fi
  # shellcheck disable=SC2046
  wait_ecs_services $(ecs_services)
}

# --- host -----------------------------------------------------------------------------------------------------
host_ip() { tf_out vm public_ip; }
docker_context() { # (re)create the SSH docker context of the stack once the host answers and cloud-init is done
  local ip i; ip="$(host_ip)"
  for i in $(seq 1 60); do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "ubuntu@$ip" true 2>/dev/null && break
    [ "$i" = 60 ] && die "ssh ubuntu@$ip did not answer within 5 minutes (security group allows only $(presenter_cidr)?)"
    sleep 5
  done
  echo "   ssh ok, waiting for cloud-init (Docker install)"
  ssh -o BatchMode=yes "ubuntu@$ip" 'cloud-init status --wait >/dev/null' || die "cloud-init failed on $ip"
  # docker compose build opens one SSH connection per image in parallel; sshd's default MaxStartups (10) drops the rest.
  ssh -o BatchMode=yes "ubuntu@$ip" 'printf "MaxStartups 100:30:200\nMaxSessions 100\n" | sudo tee /etc/ssh/sshd_config.d/90-dd-demo.conf >/dev/null && sudo sshd -t && sudo systemctl reload ssh' \
    || die "could not raise the sshd connection limits on $ip"
  # New login: the session above may predate `usermod -aG docker ubuntu` in user_data.
  ssh -o BatchMode=yes "ubuntu@$ip" 'docker version --format "   docker {{.Server.Version}} {{.Server.Arch}}"' \
    || die "cloud-init or docker failed on $ip; see ssh ubuntu@$ip sudo cat /var/log/dd-demo-bootstrap.log"
  if docker context inspect "$CTX" >/dev/null 2>&1; then
    docker context update "$CTX" --docker "host=ssh://ubuntu@$ip" >/dev/null || die "docker context update $CTX failed"
  else
    docker context create "$CTX" --docker "host=ssh://ubuntu@$ip" >/dev/null || die "docker context create $CTX failed"
  fi
  echo "   docker context $CTX -> ssh://ubuntu@$ip"
}

sync_configs() { # compose `configs: file:` are bind mounts on the Docker engine host: copy them there, same absolute paths
  local ip files f abs n=0; ip="$(host_ip)"
  files="$(awk '/^configs:/{c=1} c && /^[[:space:]]+file:/{print $2}' "$OVERLAY/compose/compose.yaml")"
  [ -n "$files" ] || die "no configs found in compose.yaml"
  for f in $files; do
    [ -f "$OVERLAY/compose/$f" ] || die "config file $OVERLAY/compose/$f missing"
    abs="$(cd "$OVERLAY/compose/$(dirname "$f")" && pwd)/$(basename "$f")"
    ssh -o BatchMode=yes "ubuntu@$ip" "sudo mkdir -p '$(dirname "$abs")' && sudo chown ubuntu '$(dirname "$abs")'" \
      || die "mkdir $(dirname "$abs") on $ip failed"
    scp -q -o BatchMode=yes "$abs" "ubuntu@$ip:$abs" || die "copy of $abs to $ip failed"
    n=$((n + 1))
  done
  echo "   copied $n config files to $ip (same absolute paths as on this machine)"
}

# --- schema priming (Confluent Cloud Flink infers tables from the topics' Schema Registry subjects) -------------
prime_carts() { # one ADD + ABANDON on a non-demo product: registers carts.events-value; the abandon leaves no risk
  # /api/cart needs scenario:current, which only `scenario reset` sets, and reset needs the Flink totals: set a placeholder
  # (NX keeps an existing one); the `reset` after the Flink step replaces it.
  # shellcheck disable=SC2086
  if hybrid_enabled; then
    $DC --profile tools run --rm -T --entrypoint python scenario - <<'PY'
import json, os, urllib.request
from redis import Redis
Redis.from_url(os.environ["REDIS_URL"]).set("scenario:current", "sc-prime", nx=True)
base = os.environ["BASE_URL"].rstrip("/")
def post(body):
    req = urllib.request.Request(base + "/api/cart", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=10))
cart = post({"product_id": "P0001", "event_type": "ADD"})["cart_id"]
post({"product_id": "P0001", "event_type": "ABANDON", "cart_id": cart})
print("   primed carts.events with", cart, "(ADD + ABANDON)")
PY
    return
  fi
  $DC exec -T redis redis-cli SET scenario:current sc-prime NX >/dev/null || return 1
  $DC exec -T storefront python - <<'PY'
import json, urllib.request
def post(body):
    req = urllib.request.Request("http://127.0.0.1:8000/api/cart", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=10))
cart = post({"product_id": "P0001", "event_type": "ADD"})["cart_id"]
post({"product_id": "P0001", "event_type": "ABANDON", "cart_id": cart})
print("   primed carts.events with", cart, "(ADD + ABANDON)")
PY
}
prime_procurement() { # a cancelled order for the probe product: registers procurement.orders-*; ignored by every rule
  $DC exec -T procurement-db psql -v ON_ERROR_STOP=1 -q -U postgres -d procurement -c \
    "INSERT INTO purchase_order (request_id, store_id, product_id, quantity_requested, requested_at_ms, cancelled_at)
     VALUES ('prime|S01|__probe__|0', 'S01', '__probe__', 1, (extract(epoch from now()) * 1000)::bigint, now())
     ON CONFLICT (request_id) DO NOTHING;" || return 1
  echo "   primed procurement.orders (cancelled order prime|S01|__probe__|0)"
}
wait_subjects() { # wait_subjects <timeout_s> <topic...> : until <topic>-value exists in Schema Registry
  # Schema Registry lists only the subjects the asking key may read: use sa-flink's key (read on every subject), from the
  # stack's Terraform outputs, passed to python through the environment only (never printed).
  SR_CHECK_URL="$(tf_out cloud schema_registry_url)" SR_CHECK_KEY="$(tf_out cloud schema_check_sr_api_key)" \
  SR_CHECK_SECRET="$(tf_out cloud schema_check_sr_api_secret)" python3 - "$@" <<'PY'
import base64, json, os, sys, time, urllib.request
timeout, topics = int(sys.argv[1]), sys.argv[2:]
auth = base64.b64encode(f"{os.environ['SR_CHECK_KEY']}:{os.environ['SR_CHECK_SECRET']}".encode()).decode()
deadline = time.time() + timeout
while True:
    req = urllib.request.Request(os.environ["SR_CHECK_URL"].rstrip("/") + "/subjects", headers={"Authorization": "Basic " + auth})
    subjects = set(json.load(urllib.request.urlopen(req, timeout=10)))
    missing = [t for t in topics if f"{t}-value" not in subjects]
    if not missing:
        print("   subjects present:", ", ".join(f"{t}-value" for t in topics)); sys.exit(0)
    if time.time() > deadline:
        print(f"stack.sh: readiness timeout after {timeout}s for table/topic: {', '.join(missing)} "
              "(missing subject <table>-value)", file=sys.stderr); sys.exit(1)
    time.sleep(5)
PY
}
flink_dml_pending() { # flink_dml_pending "<file groups>" "<terraform state list>": enabled files whose DML is not in state yet
  local groups="$1" state="$2" f present
  for f in $groups; do
    case "$f" in
      sellable) ;;
      offers) has offers || continue;;
      *) has restock || continue;;
    esac
    present=0
    if printf '%s\n' "$state" | grep -Eq "^confluent_flink_statement\.(dml|dml_late)\[\"$f-[0-9]+\"\]"; then present=1; fi
    # With the offers layer on, sellable's and offers' INSERTs run as one statement set.
    if [ "$present" = 0 ] && { [ "$f" = sellable ] || [ "$f" = offers ]; } \
      && printf '%s\n' "$state" | grep -Fq 'confluent_flink_statement.dml_late["offers-set"]'; then present=1; fi
    [ "$present" = 1 ] || printf '%s\n' "$f"
  done
}
apply_flink_statements() { # apply_flink_statements "<file groups>" "<created table topics>"
  local groups="$1" flink_tables="$2" state pending
  # Deferring a file whose DML already runs makes Terraform destroy it, and the INSERT pass recreates it:
  # a restart that replays history. Defer only the files whose DML is not in state (all of them on a first create).
  tf_workspace cloud
  state="$(tf cloud state list)" || die "apply_flink_statements: terraform state list failed in terraform/cloud"
  pending="$(flink_dml_pending "$groups" "$state" | tr '\n' ' ')"
  pending="${pending% }"
  if [ -z "$pending" ]; then
    echo "   Flink statements already running: skipping the CREATE-only pass (no restart)"
  else
    if printf '%s\n' "$state" | grep -Eq '^confluent_flink_statement\.(dml|dml_late)\['; then
      echo "   Flink statements partly running: CREATE-only pass defers only the new ones ($pending); the others keep running"
    fi
    FLINK_DML_DEFERRED="$pending"; export FLINK_DML_DEFERRED
    step "terraform cloud (Flink CREATE TABLE only)" tf_apply cloud
    # A successful CREATE statement API call precedes catalogue propagation. The output subjects are
    # created by CREATE TABLE, so each subject is a bounded query-path readiness signal for its table.
    # shellcheck disable=SC2086
    step "wait for Flink CREATE TABLE readiness" wait_subjects 300 $flink_tables
  fi
  FLINK_DML_DEFERRED=""; export FLINK_DML_DEFERRED
  step "terraform cloud (Flink INSERT statements)" tf_apply cloud
  unset FLINK_DML_DEFERRED
}
wait_sellable() { # the Flink aggregate reached Redis through the sink connector
  local i n
  for i in $(seq 1 60); do
    if hybrid_enabled; then
      n="$($DC --profile tools run --rm -T --entrypoint python scenario -c 'import os; from redis import Redis; print(sum(1 for _ in Redis.from_url(os.environ["REDIS_URL"]).scan_iter("sellable:P*", count=1000)))')"
    else
      n="$($DC exec -T redis redis-cli --scan --pattern 'sellable:P*' --count 1000 | wc -l | tr -d ' ')"
    fi
    [ "${n:-0}" -ge 200 ] && { echo "   $n sellable:* keys in Redis"; return 0; }
    sleep 5
  done
  die "after 5 minutes only ${n:-0} sellable:P* keys in Redis (expected 200): check the Flink statement ${CTX}-sellable-1 (${CTX}-offers-set with the offers layer) and connector sellable-redis"
}

recreate_storefront() { # compose re-reads .env.cloud-<stack> (RUM ids)
  if hybrid_enabled; then
    # terraform/aws registered a storefront task definition with (or without) the RUM settings, and ECS deploys a
    # changed task definition by itself: wait for it instead of forcing a second deployment.
    wait_ecs_services storefront
    return
  fi
  # shellcheck disable=SC2086
  $DC up -d --no-deps storefront && echo "   storefront recreated"
}

# --- commands -------------------------------------------------------------------------------------------------
# How long does the `aws login` session last, and can we read what is left of it?
#  - Documented: the CLI refreshes the cached credentials every 15 minutes; "the overall session will be valid for up to
#    the set session duration of the IAM principal (maximum of 12 hours), after which you must run aws login again".
#    https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html
#  - NOT observable: the `Expiration` that `aws configure export-credentials --format process` prints is the expiry of the
#    15-minute credentials it just minted (schema: https://docs.aws.amazon.com/sdkref/latest/guide/feature-process-credentials.html,
#    command: https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html). It does not say when the
#    login session ends. The session metadata sits in ~/.aws/login/cache, whose format is not documented, so we do not parse it.
#  So we cannot fail early on "too little session left". We state the documented upper bound and fail loudly when it ends
#  (aws_session_ready here, apply_log_has_expired_token during an apply). The real mitigation is `aws login` right before a build.
AWS_LOGIN_MAX_HOURS=12   # documented maximum session duration (URL above); the real limit can be lower (IAM principal setting)

credential_expiration() { # credential_expiration : reads the JSON on stdin, prints ONLY the Expiration field (never key material)
  jq -er '.Expiration // empty' || return 1
}

aws_session_ready() {
  local exp
  aws sts get-caller-identity --profile "$AWS_PROFILE" --query Arn --output text >/dev/null 2>&1 \
    || die "AWS login session unavailable; run: aws login --profile $AWS_SOURCE_PROFILE"
  echo "   AWS login session via refresh profile $AWS_PROFILE: ok"
  # Informational only: this is the short-credential expiry, not the end of the login session.
  if command -v jq >/dev/null && exp="$(env -u AWS_CONFIG_FILE -u AWS_PROFILE aws configure export-credentials --profile "$AWS_SOURCE_PROFILE" --format process 2>/dev/null | credential_expiration)"; then
    echo "   short-lived credentials valid until $exp (refreshed automatically; this is not the end of the login session)"
  fi
  echo "   AWS login session: it cannot be read how much is left. A login lasts at most $AWS_LOGIN_MAX_HOURS hours (AWS docs); if yours is older than that, or you are not sure, run: aws login --profile $AWS_SOURCE_PROFILE. A long build that outlives it fails with ExpiredToken."
}

preflight() {
  local ok=1 t d n
  # First check, before any other work: an empty or malformed CIDR must stop here, never after billed stages.
  presenter_cidr >/dev/null
  for t in terraform docker ssh curl make aws; do command -v "$t" >/dev/null || { echo "   MISSING tool: $t"; ok=0; }; done
  # The Mac drives the EC2 engine through an SSH context: client, compose and buildx plugins (brew install docker docker-compose docker-buildx).
  for t in compose buildx; do docker "$t" version >/dev/null 2>&1 || { echo "   MISSING docker $t plugin (cliPluginsExtraDirs in ~/.docker/config.json)"; ok=0; }; done
  [ -f "$ENV_DIR/.env" ] || { echo "   MISSING $ENV_DIR/.env"; ok=0; }
  if [ -f "$ENV_DIR/.env" ]; then
    for n in CONFLUENT_CLOUD_API_KEY CONFLUENT_CLOUD_API_SECRET DD_API_KEY DD_APP_KEY; do
      grep -q "^$n=." "$ENV_DIR/.env" || { echo "   MISSING in .env: $n"; ok=0; }
    done
    grep -q '^JEV_API_KEY=.' "$ENV_DIR/.env" || echo "   note: JEV_API_KEY not set: offers use the safe rule only"
  fi
  [ -f "$ENV_DIR/.env.secrets" ] || echo "   note: .env.secrets absent; stack-up creates it (make secrets)"
  [ -f "$HOME/.ssh/id_ed25519.pub" ] || { echo "   MISSING ~/.ssh/id_ed25519.pub (terraform/vm key pair; or set TF_VAR_ssh_public_key_path)"; ok=0; }
  aws_session_ready
  echo "   public ingress CIDR: $(presenter_cidr)"
  local tf_dirs="account cloud vm datadog"
  hybrid_enabled && tf_dirs="$tf_dirs aws images"
  if keep_images; then echo "   keep_images: true (stack-down keeps the ECR repositories of stack $STACK)"; fi   # also validates it
  for d in $tf_dirs; do
    tf "$d" init -backend=false -input=false >/dev/null 2>&1 && tf "$d" validate -no-color >/dev/null 2>&1 \
      && echo "   terraform/$d: valid" || { echo "   terraform/$d: INVALID (terraform -chdir=terraform/$d validate)"; ok=0; }
  done
  docker context inspect "$CTX" >/dev/null 2>&1 && echo "   note: docker context $CTX already exists (stack-up updates it)"
  [ "$ok" = 1 ] || die "preflight failed (see MISSING/INVALID above)"
  echo "   preflight ok for stack $STACK (owner $OWNER)"
}

up() {
  [ "${CONFIRM:-}" = yes ] || die "stack-up creates billed resources: add CONFIRM=yes (after the cost note and approval)"
  step "preflight" preflight
  step "terraform account (cost-meter identity and AWS CCM)" tf_apply account
  if hybrid_enabled; then
    step "check for per-stack ECR repositories of the old layout" refuse_legacy_ecr_state
    step "terraform images (ECR repositories of the stack, lifecycle policy)" tf_apply images
  fi
  set_layers "${LAYERS:-core}"
  echo "   layers: core $(tr '\n' ' ' < "$LAYERS_FILE")"
  # Flink inputs get their schemas from the apps' first records: hold every statement back until then.
  # A re-run on a stack whose statements already run must not destroy and recreate them.
  # shellcheck disable=SC2086
  # Capture first: with pipefail, grep -q closing the pipe early fails the pipeline (SIGPIPE) and reads as "none".
  local cloud_state=""
  if tf_has_state cloud; then tf_workspace cloud; cloud_state="$(tf cloud state list)"; fi
  if printf '%s\n' "$cloud_state" | grep -q '^confluent_flink_statement\.'; then
    echo "   Flink statements already exist in terraform/cloud: not deferred"; defer --none
  else
    defer --none; defer $ALL_FLINK
  fi
  step "terraform cloud bootstrap (new-stack CRNs)" tf_bootstrap_cloud
  step "terraform cloud (Confluent: cluster, topics, keys, compute pool; statements deferred)" tf_apply cloud
  step "terraform vm (EC2 host)" tf_apply vm
  if hybrid_enabled && has dd-rum; then
    step "terraform datadog (RUM application only, before the storefront's first deployment)" tf_apply_datadog_rum
  fi
  step "env file .env.cloud-$STACK" write_env_file
  step "docker context $CTX" docker_context
  step "secrets" mk secrets
  if hybrid_enabled; then
    step "image tags (content hash of the build inputs)" write_image_tags
    # Before terraform aws, so that the first tasks find every secret (the RUM token included).
    step "sync secrets to SSM SecureString" sync_ssm_secrets
    step "start the image job: reuse images from ECR, build only the missing ones (background)" start_images_job
    step "terraform aws (ECS, ALB, ElastiCache; images in parallel)" tf_apply aws
    step "hybrid endpoint env" write_hybrid_endpoints
    step "wait for the image job" wait_images_job
    step "roll out changed images and wait for all ECS services" roll_out_images
  else
    step "build images on the host" mk build
  fi
  step "copy compose config files to the host" sync_configs
  step "compose up (core)" mk _up
  step "seed the five stores" mk seed
  step "register core connectors" mk register-connector LAYERS=
  local l
  for l in releases restock offers; do
    if has "$l"; then
      if hybrid_enabled && { [ "$l" = releases ] || [ "$l" = offers ]; }; then
        echo "   layer $l runs on ECS for hybrid; no VM containers started"
      else
        step "layer $l (containers)" mk layer-on L="$l" LAYER_SKIP_TF=1
      fi
    fi
  done
  if has control-center; then
    step "layer control-center (containers)" mk layer-on L=control-center LAYER_SKIP_TF=1
  fi
  step "background sales on (also gives stock.movements its schema)" mk sales-on
  step "prime carts.events" prime_carts
  if has restock; then step "prime procurement.orders" prime_procurement; fi
  local topics="inventory.state stock.movements demo.config carts.events"
  has restock && topics="$topics procurement.orders"
  # shellcheck disable=SC2086
  step "wait for input schemas" wait_subjects 300 $topics
  defer --none
  local flink_tables="stock.sellable"
  has offers && flink_tables="$flink_tables carts.at-risk"
  has restock && flink_tables="$flink_tables stock.demand restock.forecast restock.requests"
  apply_flink_statements "$ALL_FLINK" "$flink_tables"
  step "wait for the Flink aggregate in Redis" wait_sellable
  step "reset to the seeded baseline (stops sales, sets the scenario)" mk reset
  # Verify at rest: with background sales on, Redis trails the sources by the pipeline delay and verify reports that as mismatches.
  step "verify source vs Redis" mk verify
  step "background sales on" mk sales-on
  step "terraform datadog (dashboard, monitors, layers)" tf_apply datadog
  step "env file (with RUM ids)" write_env_file
  # Hybrid: the RUM application was applied before terraform aws, so the storefront already runs with it.
  for l in dd-streams dd-synthetics dd-rum; do
    if has "$l"; then step "layer $l (record)" mk layer-on L="$l" LAYER_SKIP_TF=1; fi
  done
  if has dd-rum && ! hybrid_enabled; then step "storefront picks up RUM" recreate_storefront; fi
  if hybrid_enabled; then step "publish demo:links (control panel Links card)" mk links-publish; fi
  step "smoke test (browser + API)" mk smoke
  status
  echo; echo "== stack $STACK is up in $(( $(date +%s) - T0 )) s. At the end: make MODE=cloud STACK=$STACK stack-down CONFIRM=yes"
}

layer_tf() { # layer_tf <layer> on|off : Terraform side of layer.sh in MODE=cloud (before containers on, after containers off)
  local layer="$1" st="$2"
  [ "${CONFIRM:-}" = yes ] || die "layer $layer $st applies Terraform (billed resources): add CONFIRM=yes"
  presenter_cidr >/dev/null
  layer_set "$layer" "$st"
  case "$layer:$st" in
    restock:on)  defer demand procurement restock; tf_apply cloud; tf_apply datadog;;   # statements after priming (layer-post)
    offers:on) tf_apply cloud; sync_ssm_secrets; tf_apply aws; tf_apply datadog;;
    dd-streams:on) tf_apply cloud; tf_apply datadog;;
    offers:off) tf_apply aws; tf_apply datadog; tf_apply cloud;;
    restock:off|dd-streams:off) tf_apply datadog; tf_apply cloud;;
    control-center:on) tf_apply cloud; tf_apply vm;;
    control-center:off) tf_apply vm; tf_apply cloud;;
    dd-synthetics:on) tf_apply vm; tf_apply aws; tf_apply datadog;;
    dd-synthetics:off) tf_apply datadog; tf_apply aws; tf_apply vm;;
    releases:on) if hybrid_enabled; then tf_apply aws; fi; tf_apply datadog;;   # aws: 1.1.0 and 1.2.0 to desired 1
    releases:off) tf_apply datadog; if hybrid_enabled; then tf_apply aws; fi;;  # aws: back to desired 0
    dd-rum:*) tf_apply datadog;;
    *) die "layer_tf: unknown layer '$layer'";;
  esac
  write_env_file
  if [ "$layer" = dd-rum ] && hybrid_enabled; then
    if [ "$st" = on ]; then sync_ssm_secrets; fi
    tf_apply aws
  fi
  if [ "$layer" = releases ] && hybrid_enabled; then wait_ecs_services inventory-api-110 inventory-api-120; fi
}

layer_post() { # layer_post <layer> on|off : after layer.sh started or stopped the containers
  case "$1:$2" in
    restock:on)
      prime_procurement
      wait_subjects 300 procurement.orders stock.movements demo.config inventory.state
      undefer demand procurement restock
      apply_flink_statements "demand procurement restock" "stock.demand restock.forecast restock.requests";;
    dd-rum:*) recreate_storefront;;
    *) echo "   (nothing after the containers for $1 $2)";;
  esac
}

status() {
  echo; echo "== stack $STACK"
  echo "   layers (desired): core $( [ -f "$LAYERS_FILE" ] && tr '\n' ' ' < "$LAYERS_FILE")"
  echo "   flink deferred:   $(deferred_hcl)"
  if hybrid_enabled && tf_has_state aws; then
    local base; base="$(aws_alb_url)"
    echo "   shop:     $base/#/product/P0042"
    echo "   control:  $base/control/   (user demo, CONTROL_PASSWORD in .env.secrets)"
  elif hybrid_enabled; then
    echo "   online: AWS ALB not deployed"
  elif tf_has_state vm; then
    local base; base="$(tf_out vm ingress_base_url)"
    echo "   shop:     $base/#/product/P0042"
    echo "   control:  $base/control/   (user demo, CONTROL_PASSWORD in .env.secrets)"
  else echo "   vm: not deployed"; fi
  if [ "$TOPOLOGY" = hybrid ] && has control-center && tf_has_state vm; then
    echo "   control-center: http://$(tf_out vm public_ip):9021"
  fi
  [ -f "$DD_APPLIED" ] && echo "   dashboard: $(tf_out datadog dashboard_url)"
  if tf_has_state account; then
    echo "   cost dashboard: $(tf_out account cost_dashboard_url)"
  else
    echo "   cost dashboard: account not deployed"
  fi
  if hybrid_enabled && tf_has_state aws; then
    echo "   ALB:       $(aws_alb_url)"
    echo "   ECS:       $(aws_cluster) (services-stable required)"
  fi
  if tf_has_state cloud; then echo "   confluent environment: $(tf_out cloud environment_id), cluster $(tf_out cloud kafka_cluster_id)"; else echo "   cloud: not deployed"; fi
  if docker context inspect "$CTX" >/dev/null 2>&1 && [ -f "$ENV_CLOUD" ]; then mk layers-status || true; fi
}

ACCOUNT_DOWN_OVERRIDE="$TF/account/account-down_override.tf.json"
write_account_down_override() {
  local tmp="$ACCOUNT_DOWN_OVERRIDE.tmp"
  cat > "$tmp" <<'JSON' || die "could not write account-down override $tmp"
{
  "resource": {
    "confluent_service_account": {
      "cost_meter": { "lifecycle": { "prevent_destroy": false } }
    },
    "confluent_role_binding": {
      "cost_meter_billing_admin": { "lifecycle": { "prevent_destroy": false } },
      "cost_meter_metrics_viewer": { "lifecycle": { "prevent_destroy": false } }
    },
    "aws_s3_bucket": {
      "cur": { "force_destroy": true }
    }
  }
}
JSON
  chmod 600 "$tmp" || die "could not chmod account-down override $tmp"
  mv "$tmp" "$ACCOUNT_DOWN_OVERRIDE" || die "could not install account-down override"
}

account_down() {
  [ "${CONFIRM:-}" = yes ] || die "account-down destroys account resources: add CONFIRM=yes"
  [ "${ACCOUNT_DOWN_DESTROY:-}" = yes ] || die "account-down is additionally gated: add ACCOUNT_DOWN_DESTROY=yes"
  write_account_down_override
  trap 'rm -f "$ACCOUNT_DOWN_OVERRIDE"' EXIT
  tf_apply account destroy
  rm -f "$ACCOUNT_DOWN_OVERRIDE"
  trap - EXIT
}

down_credentials_preflight() { # down_credentials_preflight "<layers with state>" : read-only, before any destroy or prompt
  local found=" $1 " out code dd_url
  load_env
  # AWS: terraform aws and vm, and the final leftover check (Tagging API) always needs it.
  out="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Arn --output text 2>&1)" \
    || die "AWS session expired or missing ($(printf '%s' "$out" | tail -n1)): run aws login --profile $AWS_SOURCE_PROFILE, then rerun stack-down. Nothing was destroyed."
  echo "   AWS session ($AWS_SOURCE_PROFILE): ok"
  case "$found" in *" cloud "*)
    # terraform/cloud authenticates with the Cloud API key from .env, not with the CLI login. Key never on argv.
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -K - <<EOF
user = "$CONFLUENT_CLOUD_API_KEY:$CONFLUENT_CLOUD_API_SECRET"
url = "https://api.confluent.cloud/org/v2/environments?page_size=1"
EOF
)" || die "could not reach the Confluent Cloud API (network?): fix it, then rerun stack-down. Nothing was destroyed."
    [ "$code" = 200 ] || die "Confluent Cloud API key in $ENV_DIR/.env rejected (HTTP $code): put a valid CONFLUENT_CLOUD_API_KEY/SECRET there, then rerun stack-down. Nothing was destroyed."
    echo "   Confluent Cloud API key: ok";;
  esac
  # The final leftover check lists environments with the Confluent CLI (skipped there when the CLI is missing).
  if command -v confluent >/dev/null; then
    out="$(confluent environment list -o json 2>&1)" \
      || die "Confluent CLI session expired or missing ($(printf '%s' "$out" | tail -n1)): run confluent login, then rerun stack-down. Nothing was destroyed."
    echo "   Confluent CLI login: ok"
  fi
  case "$found" in *" datadog "*)
    # Datadog has no hourly cost: a rejected key is a warning; the datadog layer then fails and keeps its state.
    dd_url="${TF_VAR_datadog_api_url:-https://api.datadoghq.eu/}"
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -K - <<EOF
header = "DD-API-KEY: $DD_API_KEY"
url = "${dd_url%/}/api/v1/validate"
EOF
)" || code="unreachable"
    if [ "$code" = 200 ]; then echo "   Datadog API key: ok"
    else echo "WARNING: Datadog API key check returned $code; the datadog layer will likely fail (no hourly cost; fix DD_API_KEY/DD_APP_KEY in .env and rerun stack-down)" >&2; fi;;
  esac
}

DOWN_FAILED=""
down_cause_file() { printf '%s\n' "$STATE_DIR/$STACK-down-$1.cause"; }   # the first reason a layer's destroy failed
down_layer() { # down_layer <layer> <function> : one layer's destroy in a subshell (die ends only that layer); failures collected
  local layer="$1" s; shift
  echo; echo "== [destroy $layer]"
  s=$(date +%s)
  rm -f "$(down_cause_file "$layer")" || die "could not reset $(down_cause_file "$layer")"
  if ( CAUSE_FILE="$(down_cause_file "$layer")"; "$@" ); then
    echo "   [destroy $layer] took $(( $(date +%s) - s )) s (stack $STACK, total $(( $(date +%s) - T0 )) s)"
  else
    echo "stack.sh: destroy of layer '$layer' FAILED (see the error above); continuing with the remaining layers" >&2
    DOWN_FAILED="$DOWN_FAILED $layer"
  fi
}
down_datadog() { tf_apply datadog destroy; }
down_aws() {   # ECR images first (populated repositories), SSM parameters only after a successful destroy
  delete_ecr_repositories || return 1
  tf_apply aws destroy || return 1
  delete_stack_ssm_parameters
}
down_vm() {
  local ip
  ip="$(tf_out vm public_ip 2>/dev/null)" || ip=""
  tf_apply vm destroy || return 1
  if [ -n "$ip" ]; then
    if ssh-keygen -R "$ip" >/dev/null 2>&1; then echo "   removed $ip from known_hosts"; else echo "   note: could not remove $ip from known_hosts"; fi
  fi
}
down_vm_instance() { # after a failed aws layer: terminate the VM (the hourly cost) but keep its security group,
  # which terraform/aws still references (rules on it, and the Redis rule pointing at it); a rerun removes the rest.
  TF_DESTROY_TARGET=aws_instance.main tf_apply vm destroy
}
down_cloud() { detach_cloud_schemas_before_destroy || return 1; tf_apply cloud destroy; }
down_images() { tf_apply images destroy; }   # force_delete: the images go with their repositories
DOWN_CLOUD_PID=""
DOWN_CLOUD_LOG=""
start_down_cloud() { # down_layer cloud in the background; its output is copied into this log by wait_down_cloud
  DOWN_CLOUD_LOG="$LOG_DIR/$STACK-down-cloud-$(date +%Y%m%d-%H%M%S).log"
  mkdir -p "$LOG_DIR" || die "could not create $LOG_DIR"
  : > "$DOWN_CLOUD_LOG" || die "could not create $DOWN_CLOUD_LOG"
  chmod 600 "$DOWN_CLOUD_LOG" || die "could not protect $DOWN_CLOUD_LOG"
  # The subshell starts with DOWN_FAILED empty; down_layer sets it on failure, which becomes the exit status.
  # shellcheck disable=SC2030  # deliberate: the subshell's DOWN_FAILED only sets its exit status
  ( DOWN_FAILED=""; down_layer cloud down_cloud; [ -z "$DOWN_FAILED" ] ) > "$DOWN_CLOUD_LOG" 2>&1 &
  DOWN_CLOUD_PID=$!
  echo; echo "== [destroy cloud] started in the background (pid $DOWN_CLOUD_PID), output copied here when it ends"
}
wait_down_cloud() {
  local status=0
  wait "$DOWN_CLOUD_PID" || status=$?
  echo; echo "== [destroy cloud] background output:"
  cat "$DOWN_CLOUD_LOG" || echo "stack.sh: could not read $DOWN_CLOUD_LOG" >&2
  # shellcheck disable=SC2031  # the parent's DOWN_FAILED, not the background subshell's
  [ "$status" = 0 ] || DOWN_FAILED="$DOWN_FAILED cloud"
}

down() {
  [ "${CONFIRM:-}" = yes ] || die "stack-down destroys stack $STACK: add CONFIRM=yes"
  # A mistyped STACK must not "succeed": the workspaces must exist and at least one must hold state.
  # Read-only. Terraform state and workspaces are never deleted, so a failed run can always be rerun.
  local d found="" workspaces state
  local tf_dirs="cloud vm datadog"
  hybrid_enabled && tf_dirs="$tf_dirs aws images"
  if keep_images; then echo "   keep_images: true: the ECR repositories of stack $STACK are kept"; fi   # also validates it, before any destroy
  for d in $tf_dirs; do
    [ -d "$TF/$d/.terraform" ] || tf "$d" init -input=false >/dev/null || die "terraform init failed in terraform/$d"
    workspaces="$(tf "$d" workspace list)" || die "could not list Terraform workspaces in terraform/$d"
    printf '%s\n' "$workspaces" | tr -d '* ' | grep -qx "$STACK" || continue
    tf "$d" workspace select "$STACK" >/dev/null || die "could not select workspace $STACK in terraform/$d"
    state="$(tf "$d" state list)" || die "could not list the Terraform state of terraform/$d (workspace $STACK)"
    [ -n "$state" ] && found="$found $d"
  done
  [ -n "$found" ] || die "no Terraform state for stack '$STACK' in cloud, vm, datadog, aws or images (typo? workspaces: $(tf cloud workspace list | tr -d '* \n' | tr '\n' ' '))"
  echo "   stack $STACK has state in:$found"
  # Credentials before the question and before any destroy: an expired login must stop here with nothing touched.
  down_credentials_preflight "$found"
  confirm "Destroy EVERYTHING of stack $STACK (containers and volumes, Datadog objects, EC2, Confluent environment)?" || exit 1
  case " $found " in *" vm "*)
    if docker context inspect "$CTX" >/dev/null 2>&1 && [ -f "$ENV_CLOUD" ]; then
      # Loud but not blocking: an unreachable host must not keep the billed resources alive.
      mk _down PURGE=1 || echo "WARNING: compose down failed on the host (continuing: the host is destroyed next)" >&2
    fi;;
  esac
  [ -f "$LAYERS_FILE" ] || set_layers "${VALID_LAYERS// /,}"   # destroy needs a variable set; every valid layer is the superset
  export TF_DESTROY=1
  # Order: datadog, then cloud in the background while aws, vm and images run in turn. Each layer runs even when an
  # earlier one failed. vm after a failed aws is limited to the EC2 instance: terraform/aws owns rules on the VM security
  # group and an ElastiCache rule that references it, and AWS refuses to delete a referenced group (DependencyViolation),
  # so a full vm destroy would stall and fail. The instance is the hourly cost and nothing in aws depends on it.
  # cloud shares nothing with aws, vm or images (destroy plans use placeholders, never another layer's outputs), and
  # CONFIRM=yes means no prompt can compete for the terminal, so it runs in parallel (it bills the most).
  DOWN_FAILED=""
  case " $found " in *" datadog "*) down_layer datadog down_datadog;; esac
  case " $found " in *" cloud "*) start_down_cloud;; esac
  case " $found " in *" aws "*) down_layer aws down_aws;; esac
  case " $found " in *" vm "*)
    case " $DOWN_FAILED " in
      *" aws "*) echo; echo "== [destroy vm] EC2 instance only: terraform/aws failed and still references the VM security group, so the group, key pair and IAM role (no hourly cost) stay for the rerun"
                 down_layer vm down_vm_instance
                 case " $DOWN_FAILED " in *" vm "*) ;; *) DOWN_FAILED="$DOWN_FAILED vm(partial)";; esac;;
      *) down_layer vm down_vm;;
    esac;;
  esac
  case " $found " in *" images "*)
    if keep_images; then
      echo; echo "== [destroy images] KEPT: keep_images is true; the ECR repositories of stack $STACK stay (see the leftover check)"
    else
      down_layer images down_images
    fi;;
  esac
  case " $found " in *" cloud "*) wait_down_cloud;; esac
  if [ -n "$DOWN_FAILED" ]; then
    echo >&2
    echo "stack.sh: FAILED:$DOWN_FAILED; Terraform state, local env files and docker context kept. Fix the cause printed above (log: $LOG_DIR/$STACK-latest.log) and rerun stack-down: it resumes with the layers that still have state." >&2
    local left=0
    leftover_report || left=$?
    destroy_outcome "$left"
    exit 1
  fi
  if docker context inspect "$CTX" >/dev/null 2>&1; then docker context rm -f "$CTX" >/dev/null && echo "   removed docker context $CTX"; fi
  rm -f "$ENV_CLOUD" "$LAYERS_FILE" "$DEFER_FILE" "$DD_APPLIED" "$DD_RUM_APPLIED" "$IMAGE_TAGS_FILE" "$IMAGES_PUSHED_FILE" "$SSM_DIGEST_FILE" "$STATE_DIR/layers-$STACK.env" "$STATE_DIR/dd-layers-$STACK" "$STATE_DIR/routing-$STACK"
  local left=0
  leftover_report || left=$?
  destroy_outcome "$left"
  [ "$left" = 0 ] || { echo "stack.sh: stack $STACK: the leftover check did not pass (list above); rerun stack-down" >&2; exit 1; }
  echo "== stack $STACK destroyed in $(( $(date +%s) - T0 )) s"
}

# ---------------------------------------------------------------------------------------------- final banner
# Estimates from the ./demo create cost note (AWS about $0.50/h, Confluent Cloud about $1 to $2/h); not a bill.
billing_estimate() { # billing_estimate <aws 0|1> <confluent 0|1>
  local parts=""
  [ "$1" = 1 ] && parts="AWS about \$0.50/h"
  [ "$2" = 1 ] && parts="${parts:+$parts + }Confluent Cloud about \$1 to \$2/h"
  [ -z "$parts" ] || printf 'estimate: %s\n' "$parts"
}
left_items() { # left_items : the leftover list of $LEFT_FILE on one line, at most 8 items
  awk 'NF { n++; if (n <= 8) out = out (n > 1 ? ", " : "") $0 } END { if (n > 8) out = out sprintf(", and %d more (list above)", n - 8); print out }' "$LEFT_FILE"
}
leftover_report() { # read-only; returns 0 nothing billable left, 1 leftovers listed in $LEFT_FILE, 2 the check itself failed
  LEFT_FILE="$STATE_DIR/stack-$STACK.left"
  : > "$LEFT_FILE" || die "could not create $LEFT_FILE"
  rm -f "$LEFT_FILE.rc" "$LEFT_FILE.cause" || die "could not reset $LEFT_FILE.rc"
  local status=0
  ( CAUSE_FILE="$LEFT_FILE.cause"; leftover_check ) || status=$?
  if [ ! -f "$LEFT_FILE.rc" ]; then
    echo "stack.sh: the leftover check could not finish (exit $status): $(first_line "$LEFT_FILE.cause")" >&2
    return 2
  fi
  return "$(cat "$LEFT_FILE.rc")"
}
destroy_outcome() { # destroy_outcome <leftover_report status> : DESTROY COMPLETE only when no layer failed and nothing is left
  local left="$1" aws=0 confluent=0 l est cause lines=()
  if [ -z "$DOWN_FAILED" ] && [ "$left" = 0 ]; then
    write_outcome 0 "DESTROY COMPLETE: nothing left billing for stack $STACK"
    return 0
  fi
  case " $DOWN_FAILED " in *" aws "*|*" vm "*) aws=1;; esac
  case " $DOWN_FAILED " in *" cloud "*) confluent=1;; esac
  if [ "$left" = 1 ]; then
    grep -q '^AWS ' "$LEFT_FILE" && aws=1
    grep -q '^Confluent ' "$LEFT_FILE" && confluent=1
  fi
  if [ "$left" = 2 ] && [ -z "$DOWN_FAILED" ]; then aws=1; confluent=1; fi   # unknown: assume the worst
  est="$(billing_estimate "$aws" "$confluent")"
  if [ -n "$est" ]; then lines+=("DESTROY INCOMPLETE: stack $STACK is STILL BILLING ($est)")
  else lines+=("DESTROY INCOMPLETE: stack $STACK (what is left has no hourly cost)"); fi
  [ -z "$DOWN_FAILED" ] || lines+=("Not destroyed (Terraform state kept):$DOWN_FAILED")
  case "$left" in
    1) lines+=("Left: $(left_items)");;
    2) lines+=("Left: unknown, the leftover check failed: $(first_line "$LEFT_FILE.cause")");;
  esac
  for l in $DOWN_FAILED; do
    case "$l" in
      "vm(partial)") lines+=("Note: vm: EC2 instance terminated; its security group, key pair and IAM role (no hourly cost) wait for the aws layer");;
      *) cause="$(first_line "$(down_cause_file "$l")")"
         lines+=("Cause: $l layer failed: ${cause:-see the error above}");;
    esac
  done
  [ -n "$DOWN_FAILED" ] || [ "$left" != 1 ] || lines+=("Cause: resources still exist after terraform destroy (leftover check above)")
  lines+=("Fix: ./demo destroy --yes   (it resumes; log: $LOG_DIR/$STACK-latest.log)")
  write_outcome 1 "${lines[@]}"
}
layers_with_state() { # layers_with_state : "<dir>(<n> resources)" for every terraform dir of the stack with state; read-only
  local d workspaces state n out=""
  local dirs="cloud vm datadog"
  hybrid_enabled && dirs="$dirs aws images"
  for d in $dirs; do
    [ -d "$TF/$d/.terraform" ] || continue
    workspaces="$(tf "$d" workspace list)" || { out="$out $d(state unreadable)"; continue; }
    printf '%s\n' "$workspaces" | tr -d '* ' | grep -qx "$STACK" || continue
    tf "$d" workspace select "$STACK" >/dev/null || { out="$out $d(state unreadable)"; continue; }
    state="$(tf "$d" state list)" || { out="$out $d(state unreadable)"; continue; }
    n="$(printf '%s\n' "$state" | awk 'NF { n++ } END { print n + 0 }')"
    [ "$n" = 0 ] || out="$out $d($n resources)"
  done
  printf '%s\n' "$out"
}
create_outcome() { # after a failed up: what is half-built, what bills, why it failed
  local built left=0 aws=0 confluent=0 est cause lines=()
  echo; echo "== [create FAILED] checking what stack $STACK already has (read-only)"
  built="$(layers_with_state)"
  echo "   Terraform state:${built:- none}"
  leftover_report || left=$?
  case "$built" in *" aws("*|*" vm("*) aws=1;; esac
  case "$built" in *" cloud("*) confluent=1;; esac
  if [ "$left" = 1 ]; then
    grep -q '^AWS ' "$LEFT_FILE" && aws=1
    grep -q '^Confluent ' "$LEFT_FILE" && confluent=1
  fi
  est="$(billing_estimate "$aws" "$confluent")"
  if [ -n "$est" ]; then lines+=("CREATE FAILED: stack $STACK is half-built and STILL BILLING ($est)")
  else lines+=("CREATE FAILED: stack $STACK (nothing billed hourly was found)"); fi
  lines+=("Half-built (Terraform state):${built:- none}")
  case "$left" in
    1) lines+=("Left: $(left_items)");;
    2) lines+=("Left: unknown, the leftover check failed: $(first_line "$LEFT_FILE.cause")");;
  esac
  cause="$(first_line "$CAUSE_FILE")"
  lines+=("Cause: ${cause:-see the error above}")
  lines+=("Fix: fix the cause, then ./demo create --yes (it resumes), or ./demo destroy --yes to stop the billing")
  write_outcome 1 "${lines[@]}"
}
reported() { # reported up|down : runs it in a subshell; a failure always ends with a final banner in $OUTCOME_FILE
  local command="$1" status=0 cause
  CAUSE_FILE="$STATE_DIR/stack-$STACK.cause"
  : > "$CAUSE_FILE" || die "could not create $CAUSE_FILE"
  ( "$command" ) || status=$?
  [ "$status" != 0 ] || return 0
  if [ ! -s "$OUTCOME_FILE" ]; then
    case "$command" in
      up) create_outcome;;
      down) cause="$(first_line "$CAUSE_FILE")"
            write_outcome 1 "DESTROY INCOMPLETE: stack $STACK was not destroyed and may be STILL BILLING" \
              "Cause: ${cause:-see the error above}" "Fix: fix the cause, then ./demo destroy --yes   (it resumes)";;
    esac
  fi
  return "$status"
}

# ---------------------------------------------------------------------------------------------- leftover check
# The Resource Groups Tagging API lists deleted resources for a while (ECS services, volumes, security-group
# rules) and keeps every deregistered ECS task-definition revision (INACTIVE, free). So it is only the candidate
# list: task definitions are skipped and every other ARN is confirmed with its own service API.
aws_count() { # aws_count <not-found error regex> <aws args...> : prints the --query count, 0 when the API says not found
  local nf="$1" out rc=0
  shift
  out="$(aws "$@" --output text 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then printf '%s\n' "$out"; return 0; fi
  if [ -n "$nf" ] && printf '%s' "$out" | grep -Eq "$nf"; then echo 0; return 0; fi
  echo "stack.sh: leftover check: 'aws $*' failed (exit $rc): $out" >&2
  return 1
}

arn_exists() { # arn_exists <arn> : prints 1 (exists), 0 (gone) or "?" (type not verified here); returns 1 on API error
  local arn="$1" svc region rest kind id r
  IFS=: read -r _ _ svc region _ rest <<<"$arn"
  r=(--region "${region:-$(aws_region)}")
  case "$svc:$rest" in
    ec2:instance/*) id="${rest#instance/}"
      aws_count '' ec2 describe-instances "${r[@]}" --filters "Name=instance-id,Values=$id" \
        "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" --query 'length(Reservations[].Instances[])';;
    ec2:volume/*) aws_count '' ec2 describe-volumes "${r[@]}" --filters "Name=volume-id,Values=${rest#volume/}" --query 'length(Volumes)';;
    ec2:elastic-ip/*) aws_count '' ec2 describe-addresses "${r[@]}" --filters "Name=allocation-id,Values=${rest#elastic-ip/}" --query 'length(Addresses)';;
    ec2:natgateway/*) aws_count '' ec2 describe-nat-gateways "${r[@]}" --filter "Name=nat-gateway-id,Values=${rest#natgateway/}" \
        "Name=state,Values=pending,available,deleting" --query 'length(NatGateways)';;
    ec2:security-group/*) aws_count '' ec2 describe-security-groups "${r[@]}" --filters "Name=group-id,Values=${rest#security-group/}" --query 'length(SecurityGroups)';;
    ec2:security-group-rule/*) aws_count '' ec2 describe-security-group-rules "${r[@]}" \
        --filters "Name=security-group-rule-id,Values=${rest#security-group-rule/}" --query 'length(SecurityGroupRules)';;
    ec2:key-pair/*) aws_count '' ec2 describe-key-pairs "${r[@]}" --filters "Name=key-pair-id,Values=${rest#key-pair/}" --query 'length(KeyPairs)';;
    ecs:cluster/*) aws_count '' ecs describe-clusters "${r[@]}" --clusters "${rest#cluster/}" --query 'length(clusters[?status!=`INACTIVE`])';;
    ecs:service/*) kind="${rest#service/}"   # <cluster>/<service>
      aws_count 'ClusterNotFoundException' ecs describe-services "${r[@]}" --cluster "${kind%%/*}" --services "${kind#*/}" \
        --query 'length(services[?status!=`INACTIVE`])';;
    elasticloadbalancing:loadbalancer/*) aws_count 'LoadBalancerNotFound' elbv2 describe-load-balancers "${r[@]}" --load-balancer-arns "$arn" --query 'length(LoadBalancers)';;
    elasticloadbalancing:targetgroup/*) aws_count 'TargetGroupNotFound' elbv2 describe-target-groups "${r[@]}" --target-group-arns "$arn" --query 'length(TargetGroups)';;
    elasticloadbalancing:listener/*) aws_count 'ListenerNotFound' elbv2 describe-listeners "${r[@]}" --listener-arns "$arn" --query 'length(Listeners)';;
    elasticloadbalancing:listener-rule/*) aws_count 'RuleNotFound|ListenerNotFound' elbv2 describe-rules "${r[@]}" --rule-arns "$arn" --query 'length(Rules)';;
    elasticache:cluster:*) aws_count 'CacheClusterNotFound' elasticache describe-cache-clusters "${r[@]}" --cache-cluster-id "${rest#cluster:}" --query 'length(CacheClusters)';;
    elasticache:replicationgroup:*) aws_count 'ReplicationGroupNotFound' elasticache describe-replication-groups "${r[@]}" --replication-group-id "${rest#replicationgroup:}" --query 'length(ReplicationGroups)';;
    elasticache:subnetgroup:*) aws_count 'CacheSubnetGroupNotFound' elasticache describe-cache-subnet-groups "${r[@]}" --cache-subnet-group-name "${rest#subnetgroup:}" --query 'length(CacheSubnetGroups)';;
    ecr:repository/*) aws_count 'RepositoryNotFoundException' ecr describe-repositories "${r[@]}" --repository-names "${rest#repository/}" --query 'length(repositories)';;
    logs:log-group:*) id="${rest#log-group:}"; id="${id%:\*}"
      aws_count '' logs describe-log-groups "${r[@]}" --log-group-name-prefix "$id" --query "length(logGroups[?logGroupName=='$id'])";;
    ssm:parameter/*) aws_count 'ParameterNotFound' ssm get-parameter "${r[@]}" --name "/${rest#parameter/}" --query 'length([Parameter.Name])';;
    iam:role/*) aws_count 'NoSuchEntity' iam get-role --role-name "${rest##*/}" --query 'length([Role.RoleName])';;
    iam:instance-profile/*) aws_count 'NoSuchEntity' iam get-instance-profile --instance-profile-name "${rest##*/}" --query 'length([InstanceProfile.InstanceProfileName])';;
    s3:*) aws_count '' s3api list-buckets --query "length(Buckets[?Name=='$rest'])";;
    *) echo "?";;
  esac
}

aws_leftovers() { # confirm every tagged project=dd-demo ARN; returns 1 when this stack still has resources
  local tagged arn tag n=0 skipped=0 gone=0 exists mine=() other=() account=() unverified=() kept=() keep=0
  if keep_images; then keep=1; fi
  echo "== leftover check: AWS resources tagged project=dd-demo, each confirmed with its service API"
  tagged="$(aws resourcegroupstaggingapi get-resources --region "$(aws_region)" --tag-filters "Key=project,Values=dd-demo" \
    --query 'ResourceTagMappingList[].[ResourceARN, Tags[?Key==`stack`].Value | [0]]' --output text)" \
    || die "leftover check: Resource Groups Tagging API call failed (AWS_PROFILE=$AWS_PROFILE); check the EC2/ECS/ELB/ElastiCache consoles by hand"
  while IFS=$'\t' read -r arn tag; do
    [ -n "$arn" ] || continue
    n=$((n + 1))
    case "$arn" in *:task-definition/*) skipped=$((skipped + 1)); continue;; esac   # INACTIVE revisions: free, kept by AWS
    exists="$(arn_exists "$arn")" || die "leftover check: could not confirm $arn (API error above)"
    case "$exists" in
      0) gone=$((gone + 1)); continue;;
      "?") unverified+=("$arn	stack=$tag	(type not verified by this check: look it up by hand)"); continue;;
      [1-9]*) ;;
      *) die "leftover check: unexpected answer '$exists' for $arn";;
    esac
    case "$arn" in
      *":repository/dd-demo-$STACK/"*) if [ "$keep" = 1 ]; then kept+=("${arn##*:repository/}"); continue; fi;;
    esac
    case "$tag" in
      account) account+=("$arn");;
      "$STACK"|None|"") mine+=("$arn	stack=$tag");;
      *) other+=("$arn	stack=$tag");;
    esac
  done <<<"$tagged"
  echo "   $n tagged ARNs: $skipped ECS task-definition revisions skipped (free), $gone already deleted (Tagging API lags)"
  [ "${#account[@]}" -eq 0 ] || printf '   account-owned, kept on purpose (make account-down removes it): %s\n' "${account[@]}"
  [ "${#other[@]}" -eq 0 ] || printf '   other stack, not part of this teardown: %s\n' "${other[@]}"
  [ "${#unverified[@]}" -eq 0 ] || printf 'WARNING: %s\n' "${unverified[@]}" >&2
  if [ "${#kept[@]}" -gt 0 ]; then kept_images_report "${kept[@]}"; fi
  if [ "${#mine[@]}" -eq 0 ]; then
    echo "   no billable AWS leftovers for stack $STACK"
    return 0
  fi
  echo "WARNING: ${#mine[@]} resource(s) of stack $STACK still exist and may bill:" >&2
  printf 'WARNING:   %s\n' "${mine[@]}" >&2
  if [ -n "$LEFT_FILE" ]; then
    printf '%s\n' "${mine[@]}" | awk -F'\t' '{ split($1, a, ":"); r = a[6]; for (i = 7; i in a; i++) r = r ":" a[i]; print "AWS " a[3] " " r }' >> "$LEFT_FILE" \
      || die "could not write $LEFT_FILE"
  fi
  return 1
}

ECR_USD_PER_GB_MONTH=0.10   # private ECR storage, eu-west-1: AWS Price List API, read 2026-10-06
kept_images_report() { # kept_images_report <repository...> : count, stored size and storage price, read-only
  local repo bytes total=0
  for repo in "$@"; do
    bytes="$(aws ecr describe-images --repository-name "$repo" --region "$(aws_region)" \
      --query 'sum(imageDetails[].imageSizeInBytes)' --output text)" \
      || die "leftover check: could not read the image sizes of kept ECR repository $repo"
    [[ "$bytes" =~ ^[0-9]+$ ]] || die "leftover check: unexpected image size '$bytes' for ECR repository $repo"
    total=$((total + bytes))
  done
  # shellcheck disable=SC2016  # awk program: $ is literal dollar text in its printf
  LC_ALL=C awk -v n="$#" -v b="$total" -v p="$ECR_USD_PER_GB_MONTH" -v s="$STACK" -v r="$(aws_region)" 'BEGIN {
    gb = b / 1073741824
    printf "   kept on purpose (keep_images: true): %d ECR repositories of stack %s, %.2f GB stored, about $%.2f per month at $%.2f per GB-month (price read for eu-west-1; region %s). To delete them: keep_images: false, then ./demo destroy\n", n, s, gb, gb * p, p, r
  }' || die "leftover check: could not format the kept-images report"
}

confluent_leftovers() { # returns 1 when a dd-demo-* environment still exists
  local envs
  if ! command -v confluent >/dev/null; then
    echo "   (Confluent leftover check skipped: confluent CLI missing; check the Cloud console for dd-demo-$STACK)"
    return 0
  fi
  envs="$(confluent environment list -o json 2>&1)" \
    || die "leftover check: 'confluent environment list' failed (not logged in? run: confluent login): $envs"
  envs="$(printf '%s' "$envs" | python3 -c 'import json,sys; print("\n".join(e["name"] for e in json.load(sys.stdin) if e["name"].startswith("dd-demo-")))')" \
    || die "leftover check: could not parse 'confluent environment list -o json'"
  if [ -z "$envs" ]; then echo "   no dd-demo-* Confluent environment"; return 0; fi
  if printf '%s\n' "$envs" | grep -qx "dd-demo-$STACK"; then
    printf 'WARNING: Confluent environment still exists (billed hourly): %s\n' "$envs" >&2
    if [ -n "$LEFT_FILE" ]; then echo "Confluent environment dd-demo-$STACK" >> "$LEFT_FILE" || die "could not write $LEFT_FILE"; fi
    return 1
  fi
  printf '   other stacks, not part of this teardown: Confluent environment %s\n' $envs
}

leftover_check() { # read-only; also standalone: make MODE=cloud STACK=<s> stack-leftovers
  local rc=0
  echo
  aws_leftovers || rc=1
  confluent_leftovers || rc=1
  if [ -n "$LEFT_FILE" ]; then echo "$rc" > "$LEFT_FILE.rc" || die "could not write $LEFT_FILE.rc"; fi
  return "$rc"
}

dispatch_command() { # dispatch_command <command> [args...]
  configure_aws_refresh_profile
  # Commands that plan terraform/vm or terraform/aws with the ingress CIDR: fill an empty allowed_cidr once, first.
  case "$1" in preflight|up|layer-tf) resolve_presenter_cidr;; esac
  case "$1" in
    preflight) preflight;;
    up) reported up;;
    down) reported down;;
    leftovers) leftover_check;;
    status) load_env; status;;
    account-up)   [ "$STACK" = account ] || die "account-up: run with STACK=account (account-wide, not per stack)"; tf_apply account;;
    account-down) [ "$STACK" = account ] || die "account-down: run with STACK=account"; account_down;;
    layer-tf)   : "${3:?stack.sh layer-tf <layer> on|off}"; layer_tf "$2" "$3";;
    layer-post) : "${3:?stack.sh layer-post <layer> on|off}"; layer_post "$2" "$3";;
    *) echo "usage: stack.sh preflight | up | down | leftovers | status | account-up | account-down | layer-tf <layer> on|off | layer-post <layer> on|off" >&2; return 2;;
  esac
}

command="${1:-usage}"
case "$command" in
  preflight|up|down|leftovers|status|account-up|account-down|layer-tf|layer-post) ;;
  *) command=usage;;
esac
if [ "${STACK_SOURCE_ONLY:-}" = 1 ]; then
  return 0 2>/dev/null || exit 0
fi
run_logged "$command" dispatch_command "$@"
