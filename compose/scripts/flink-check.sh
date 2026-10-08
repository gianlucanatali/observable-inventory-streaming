#!/usr/bin/env bash
# Read-only check of the offers Flink output after `./demo create` (layers core,releases,offers):
#   1. the offers statement (statement set) is RUNNING and its description carries no UPSERT_AND_PRIMARY_KEYS_DIFFERENT / upsert-key warning
#   2. the latest carts.at-risk-key subject has exactly the fields scenario_id, cart_id, product_id
# Run via make:  make flink-check   (cloud stack from demo.yaml, or MODE=cloud STACK=<name> make flink-check)
#
# Strictly read-only: Terraform outputs, one `confluent flink statement describe`, one Schema Registry GET.
# No create/apply/destroy, nothing is written to a topic or to a subject. Secrets are passed through the
# environment only and never printed.
# Credentials: the stack's Terraform outputs (Schema Registry key of sa-flink) and your Confluent CLI login.
#
# The offers statement is not measured for latency here: no script in this repository measures the Flink hop.
set -euo pipefail
umask 077

fail=0
: "${STACK:?flink-check: STACK must be set (run via make flink-check)}"
: "${OVERLAY:?flink-check: OVERLAY must be set (run via make flink-check)}"
[ "${MODE:-cloud}" = cloud ] || { echo "flink-check: MODE=${MODE:-} is not cloud: this check reads the Confluent Cloud stack (MODE=cloud STACK=<name>)" >&2; exit 1; }

# Reuse stack.sh helpers (tf_out, TF path, CTX) without running anything.
# shellcheck disable=SC1091
STACK_SOURCE_ONLY=1 . "$OVERLAY/compose/scripts/stack.sh"
set +e   # stack.sh sets -e; this script reports every check before it exits
say() { printf '%s\n' "$*"; }
err() { printf 'flink-check: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }   # replaces stack.sh's die, which names stack.sh

command -v terraform >/dev/null || die "terraform is not installed: it reads the stack outputs"
command -v python3 >/dev/null || die "python3 is not installed: it parses the statement and schema JSON"
command -v confluent >/dev/null || die "the Confluent CLI is not installed: it describes the Flink statement"

[ -d "$TF/cloud/.terraform" ] || die "terraform/cloud is not initialised: no stack has been created from this checkout (run ./demo create first)"
tf cloud workspace list 2>/dev/null | sed 's/^[* ]*//' | grep -qx "$STACK" \
  || die "no Terraform workspace '$STACK' in terraform/cloud: the stack does not exist or is destroyed (run ./demo create first)"

# tf_out selects the stack's workspace (it exists, checked above) and reads one output; a failure names the output.
out() { # an empty or destroyed state prints nothing and exits 0, so an empty value is a failure too
  local v
  v="$(tf_out "$1" "$2")" && [ -n "$v" ] \
    || die "terraform/$1 output '$2' is missing or empty for stack $STACK: the cloud layer is not applied (stack not created, or destroyed): run ./demo create first"
  printf '%s' "$v"
}
env_id="$(out cloud environment_id)" || exit 1
flink_endpoint="$(out cloud flink_rest_endpoint)" || exit 1
sr_url="$(out cloud schema_registry_url)" || exit 1
sr_key="$(out cloud schema_check_sr_api_key)" || exit 1
sr_secret="$(out cloud schema_check_sr_api_secret)" || exit 1

# Flink regional endpoint: https://flink.<region>.<cloud>.confluent.cloud
host="${flink_endpoint#https://}"; host="${host%%/*}"
IFS=. read -r _ region cloud _ <<<"$host"
[ -n "${region:-}" ] && [ -n "${cloud:-}" ] || die "cannot read region and cloud from the Flink endpoint '$flink_endpoint' (expected https://flink.<region>.<cloud>.confluent.cloud)"

stmt="${CTX}-offers-set"   # the EXECUTE STATEMENT SET with the INSERTs of sellable.sql and cart_at_risk.sql (offers-0 is the CREATE TABLE)

say "== 1. Flink statement $stmt (environment $env_id, $cloud $region)"
errf="$(mktemp)"; trap 'rm -f "$errf"' EXIT   # the CLI prints notices on stderr: keep them out of the JSON
if ! desc="$(confluent flink statement describe "$stmt" --environment "$env_id" --cloud "$cloud" --region "$region" -o json 2>"$errf")"; then
  err "FAIL: could not describe statement $stmt: $(tail -n1 "$errf")"
  err "why: the statement is missing (the offers layer is not on, or its INSERT was not applied) or the Confluent CLI is not logged in (run: confluent login)"
  fail=1
else
  STMT_JSON="$desc" python3 - "$stmt" <<'PY' || fail=1
import json, os, re, sys
name = sys.argv[1]
raw = os.environ["STMT_JSON"]
try:
    d = json.loads(raw)
except ValueError as e:
    print(f"flink-check: FAIL: describe output for {name} is not JSON ({e}): {raw[:200]}", file=sys.stderr); sys.exit(1)
# Verified live shape: top-level "status" (string), optional "status_detail" (string) and optional
# "warnings" (list of {severity, reason, message, created_at}). The SQL text ("statement") is not scanned.
phase = d.get("status") if isinstance(d.get("status"), str) else ""
phase = phase or "<unknown>"
detail = d.get("status_detail") or ""
warnings = d.get("warnings") or []
print(f"   status: {phase}" + (f" ({detail[:160]})" if detail else ""))
for w in warnings:
    print(f"   warning: {w.get('severity')} {w.get('reason')}: {str(w.get('message'))[:160]}")
pat = re.compile(r"upsert_and_primary_keys_different|upsert key|primary key", re.I)
bad = any(pat.search(json.dumps(w)) for w in warnings) or bool(pat.search(detail))
ok = phase.upper() == "RUNNING" and not bad
if phase.upper() != "RUNNING":
    print(f"flink-check: FAIL: statement {name} is {phase}, expected RUNNING (DEGRADED is the documented effect of the key mismatch)", file=sys.stderr)
if bad:
    print(f"flink-check: FAIL: statement {name} still carries an UPSERT_AND_PRIMARY_KEYS_DIFFERENT / upsert-key warning: the sink PRIMARY KEY does not match the query's upsert key", file=sys.stderr)
print("   upsert-key warning: " + ("PRESENT" if bad else "none in the describe output"))
print("   " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
PY
fi

say "== 2. Schema Registry subject carts.at-risk-key (latest version)"
SR_URL="$sr_url" SR_KEY="$sr_key" SR_SECRET="$sr_secret" python3 - <<'PY' || fail=1
import base64, json, os, sys, urllib.error, urllib.request
want = ["scenario_id", "cart_id", "product_id"]
url = os.environ["SR_URL"].rstrip("/") + "/subjects/carts.at-risk-key/versions/latest"
auth = base64.b64encode(f"{os.environ['SR_KEY']}:{os.environ['SR_SECRET']}".encode()).decode()
try:
    body = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Basic " + auth}), timeout=15))
except urllib.error.HTTPError as e:
    print(f"flink-check: FAIL: Schema Registry answered HTTP {e.code} for carts.at-risk-key "
          "(404 = the CREATE TABLE has not run: the offers layer is off or not applied yet; 401/403 = the sa-flink key is not valid)", file=sys.stderr)
    sys.exit(1)
except (urllib.error.URLError, OSError, ValueError) as e:
    print(f"flink-check: FAIL: could not read carts.at-risk-key from {os.environ['SR_URL']}: {e}", file=sys.stderr); sys.exit(1)
if body.get("schemaType", "AVRO") != "AVRO":
    print(f"flink-check: FAIL: carts.at-risk-key is {body.get('schemaType')}, expected AVRO", file=sys.stderr); sys.exit(1)
try:
    fields = [f["name"] for f in json.loads(body["schema"])["fields"]]
except (KeyError, TypeError, ValueError) as e:
    print(f"flink-check: FAIL: carts.at-risk-key schema has no readable Avro fields ({e})", file=sys.stderr); sys.exit(1)
print(f"   version {body.get('version')}, fields: {', '.join(fields)}")
if fields == want:
    print("   PASS")
else:
    print(f"flink-check: FAIL: key fields are [{', '.join(fields)}], expected exactly [{', '.join(want)}] "
          "(a stale carts.at-risk table from before the change: the key is only registered by CREATE TABLE on a fresh stack)", file=sys.stderr)
    print("   FAIL"); sys.exit(1)
PY

say "== 3. Flink hop latency: not measured here (no script in this repository measures it)"
if [ "$fail" = 0 ]; then say "flink-check: PASS (statement and key schema)"; else err "FAIL (see the messages above)"; fi
exit "$fail"
