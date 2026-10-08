#!/usr/bin/env bash
# Offline guard for flink-check.sh: stubs terraform and confluent on PATH and serves Schema Registry from a local
# http.server. Nothing reaches a real account.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-flink-check: %s\n' "$*" >&2; exit 1; }
TMP="$(mktemp -d)"
SRV=""
trap '[ -z "$SRV" ] || kill "$SRV" 2>/dev/null; rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/ov/terraform/cloud/.terraform" "$TMP/sr/subjects/carts.at-risk-key/versions"
ln -s "$ROOT/compose" "$TMP/ov/compose"

cat >"$TMP/bin/terraform" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *"workspace list"*) printf '  default\n* hybrid\n';;
  *"workspace select"*) ;;
  *"output -raw environment_id"*) printf 'env-test';;
  *"output -raw flink_rest_endpoint"*) printf 'https://flink.eu-west-1.aws.confluent.cloud';;
  *"output -raw schema_registry_url"*) printf 'http://127.0.0.1:%s' "$SR_PORT";;
  *"output -raw schema_check_sr_api_key"*) printf 'k';;
  *"output -raw schema_check_sr_api_secret"*) printf 's';;
  *) echo "unexpected terraform call: $*" >&2; exit 9;;
esac
SH
cat >"$TMP/bin/confluent" <<'SH'
#!/usr/bin/env bash
[ "$1 $2 $3" = "flink statement describe" ] || { echo "unexpected confluent call (not read-only): $*" >&2; exit 9; }
[ "$4" = "dd-demo-hybrid-offers-set" ] || { echo "wrong statement $4" >&2; exit 9; }
[ -z "${DESCRIBE_FAILS:-}" ] || { echo "Error: not logged in" >&2; exit 1; }
echo "No Flink endpoint is specified, defaulting to public endpoint: https://x" >&2   # CLI notice on stderr
cat "$DESCRIBE_FILE"
SH
chmod +x "$TMP/bin/terraform" "$TMP/bin/confluent"

# Schema Registry stub: serves $TMP/sr/<path> as JSON
cat >"$TMP/srv.py" <<'PY'
import http.server, sys, os
root = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = os.path.join(root, self.path.lstrip("/"), "latest")
        if self.path.endswith("/latest") and os.path.isfile(os.path.join(root, self.path.lstrip("/"))):
            b = open(os.path.join(root, self.path.lstrip("/")), "rb").read(); self.send_response(200)
        else:
            b = b'{"error_code":40401}'; self.send_response(404)
        self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(s.server_port, flush=True)
s.serve_forever()
PY
python3 "$TMP/srv.py" "$TMP/sr" >"$TMP/port" & SRV=$!; disown "$SRV"
for _ in $(seq 1 50); do [ -s "$TMP/port" ] && break; sleep 0.1; done
export SR_PORT; SR_PORT="$(cat "$TMP/port")"
KEY="$TMP/sr/subjects/carts.at-risk-key/versions/latest"

run() { PATH="$TMP/bin:$PATH" STACK=hybrid MODE=cloud OVERLAY="$TMP/ov" ENV_DIR="$TMP" DESCRIBE_FILE="$TMP/describe.json" "$ROOT/compose/scripts/flink-check.sh" 2>&1; }
key() { printf '{"version":1,"schemaType":"AVRO","schema":"{\\"type\\":\\"record\\",\\"name\\":\\"k\\",\\"fields\\":[%s]}"}' "$1" >"$KEY"; }
f() { printf '{\\"name\\":\\"%s\\",\\"type\\":\\"string\\"}' "$1"; }
GOODKEY="$(f scenario_id),$(f cart_id),$(f product_id)"

# 1. all good
echo '{"name":"dd-demo-hybrid-offers-set","statement":"-- upsert key note","status":"RUNNING"}' >"$TMP/describe.json"; key "$GOODKEY"
out="$(run)" || fail "good case failed: $out"
case "$out" in *"flink-check: PASS"*) ;; *) fail "good case did not PASS: $out";; esac

# 2. warning present
echo '{"status":"RUNNING","warnings":[{"severity":"CRITICAL","reason":"UPSERT_AND_PRIMARY_KEYS_DIFFERENT","message":"x"}]}' >"$TMP/describe.json"
out="$(run)" && fail "warning case passed: $out"
case "$out" in *"UPSERT_AND_PRIMARY_KEYS_DIFFERENT"*) ;; *) fail "warning case gave no clear message: $out";; esac

# 3. statement DEGRADED
echo '{"status":"DEGRADED"}' >"$TMP/describe.json"
out="$(run)" && fail "degraded case passed: $out"
case "$out" in *"DEGRADED"*) ;; *) fail "degraded case gave no clear message: $out";; esac

# 4. old key (risk_id)
echo '{"status":"RUNNING"}' >"$TMP/describe.json"; key "$(f risk_id)"
out="$(run)" && fail "old key case passed: $out"
case "$out" in *"expected exactly [scenario_id, cart_id, product_id]"*) ;; *) fail "old key case gave no clear message: $out";; esac

# 5. wrong order is also a failure; missing subject (404)
key "$(f cart_id),$(f scenario_id),$(f product_id)"; out="$(run)" && fail "wrong order passed"
rm -f "$KEY"; out="$(run)" && fail "missing subject passed"
case "$out" in *"HTTP 404"*) ;; *) fail "404 case gave no clear message: $out";; esac

# 6. statement missing or CLI not logged in
key "$GOODKEY"; out="$(DESCRIBE_FAILS=1 run)" && fail "describe failure passed"
case "$out" in *"could not describe statement"*"confluent login"*) ;; *) fail "describe failure gave no clear message: $out";; esac

# 7. no stack: empty terraform output fails fast
sed -i.bak 's/printf .env-test./printf ""/' "$TMP/bin/terraform"
out="$(run)" && fail "absent stack passed"
case "$out" in *"missing or empty"*"./demo create"*) ;; *) fail "absent stack gave no clear message: $out";; esac
echo "test-flink-check: PASS"
