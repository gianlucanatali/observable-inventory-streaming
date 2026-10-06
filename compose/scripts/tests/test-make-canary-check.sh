#!/usr/bin/env bash
# Offline guard: `make canary-check` without CHECK_ARGS reads the live routing (route --sync read-back, both the nginx
# and the ALB wording) and passes it to scenario as --routing; an explicit CHECK_ARGS is passed through unchanged;
# `make canary-110-10` routes 90/10/0. ROUTING and SCEN are stubs; nothing touches Docker, AWS or Redis.
set -euo pipefail
OVERLAY="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-make-canary-check: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/route" <<'STUB'
#!/usr/bin/env bash
printf 'route %s\n' "$*" >> "$LOG"
[ "${1:-}" = --sync ] || exit 0
[ -n "${SYNC_FAIL:-}" ] && { echo "alb-routing.sh: could not read the weighted forward action" >&2; exit 1; }
printf '%s\n' "$SYNC_OUT"
STUB
printf '#!/usr/bin/env bash\nprintf "scen %%s\\n" "$*" >> "$LOG"\n' > "$tmp/scen"
chmod +x "$tmp/route" "$tmp/scen"
run() { # run <target> [make args...]
  local target="$1"; shift
  : > "$tmp/log"
  env LOG="$tmp/log" "${ENVS[@]}" make --no-print-directory -C "$OVERLAY" MODE=dev STACK=cctest \
    ROUTING="$tmp/route" SCEN="$tmp/scen" "$target" "$@" 2>&1
}
ENVS=(SYNC_OUT="alb-routing: live weights 1.0.0/1.1.0/1.2.0 = 90 10 0%")
out="$(run canary-check)" || fail "hybrid read-back failed: $out"
grep -qx "scen canary-check /out/last.json --routing 90 10 0 --verify-file /out/verify.json" "$tmp/log" \
  || fail "ALB read-back not passed as --routing: $(cat "$tmp/log")"
case "$out" in *"at live routing 90 10 0"*) ;; *) fail "echo line missing the live routing: $out";; esac
ENVS=(SYNC_OUT="apply-routing: demo:routing synced to 0 90 10")
run canary-check > /dev/null || fail "nginx read-back failed"
grep -qx "scen canary-check /out/last.json --routing 0 90 10 --verify-file /out/verify.json" "$tmp/log" \
  || fail "nginx read-back not passed as --routing: $(cat "$tmp/log")"
ENVS=(SYNC_OUT="x" SYNC_FAIL=1)
out="$(run canary-check)" && fail "canary-check passed without a routing read-back: $out"
grep -q "^scen" "$tmp/log" && fail "scenario ran without a routing read-back"
ENVS=(SYNC_OUT="nothing useful")
out="$(run canary-check)" && fail "canary-check passed with an unreadable read-back: $out"
case "$out" in *"no weights in the routing read-back"*) ;; *) fail "unreadable read-back message: $out";; esac
ENVS=(SYNC_OUT="unused")
run canary-check CHECK_ARGS="--release-a 1.1.0 --release-b 1.2.0 --verify-file /out/verify.json" > /dev/null || fail "explicit CHECK_ARGS failed"
grep -qx "scen canary-check /out/last.json --release-a 1.1.0 --release-b 1.2.0 --verify-file /out/verify.json" "$tmp/log" \
  || fail "explicit CHECK_ARGS not passed through: $(cat "$tmp/log")"
grep -q "^route" "$tmp/log" && fail "explicit CHECK_ARGS still read the routing"
out="$(run canary-110-10)" || fail "canary-110-10 failed: $out"
grep -qx "route 90 10 0" "$tmp/log" || fail "canary-110-10 did not route 90 10 0: $(cat "$tmp/log")"
case "$out" in *"== route 90/10/0 (canary: 10% to new release 1.1.0)"*) ;; *) fail "canary-110-10 echo: $out";; esac
for t in "canary-10:90 0 10" "canary-50:50 0 50" "canary-100:0 0 100"; do
  run "${t%%:*}" > /dev/null || fail "${t%%:*} failed"
  grep -qx "route ${t#*:}" "$tmp/log" || fail "${t%%:*} did not route ${t#*:}: $(cat "$tmp/log")"
done
printf 'test-make-canary-check: PASS (live routing -> --routing; explicit CHECK_ARGS unchanged; canary-110-10 = 90 10 0; fix canaries 90 0 10, 50 0 50, 0 0 100)\n'
