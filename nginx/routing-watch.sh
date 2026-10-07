#!/bin/sh
# Local mode only (compose.dev.yaml starts it next to nginx): applies release-routing requests that the control panel
# (demo-control, NginxRouting) drops into the shared nginx-routing volume. The panel has no Docker socket and nginx
# OSS has no runtime API, so the volume is the hand-over point.
#
#   requests/request  "<id> <w100> <w110> <w120>"            written by demo-control (tmp file + rename)
#   requests/result   "<id> ok <w100> <w110> <w120>"         or "<id> error <reason>"; written here atomically
#
# The same steps as nginx/apply-routing.sh: render-routing.sh (the same file), swap routing.conf, `nginx -t`, restore
# the previous file if the test fails, then reload. Only three integers cross the volume, never nginx syntax.
# Usage: routing-watch.sh            poll forever (every INTERVAL seconds)
#        routing-watch.sh --once     handle at most one pending request, then exit (tests)
# Env (tests): ROUTING_DIR (/etc/nginx/routing), RENDER (/etc/nginx/render-routing.sh), NGINX (nginx), INTERVAL (1).
set -u
set -f   # the request is split into words below; never glob it
R="${ROUTING_DIR:-/etc/nginx/routing}"
Q="$R/requests"
RENDER="${RENDER:-/etc/nginx/render-routing.sh}"
NGINX="${NGINX:-nginx}"
INTERVAL="${INTERVAL:-1}"

log() { echo "routing-watch: $*" >&2; }
result() { # result <line>: atomic, so the panel never reads half a line
  printf '%s\n' "$*" > "$Q/result.tmp" && mv "$Q/result.tmp" "$Q/result" \
    || log "could not write $Q/result ($*)"
}
oneline() { tr '\n' ' ' | sed 's/  */ /g; s/ $//'; }

apply_one() {
  # Claim the request first: a new one written meanwhile is handled on the next pass, never half-read.
  mv "$Q/request" "$Q/processing" 2>/dev/null || return 0
  line="$(cat "$Q/processing")"; rm -f "$Q/processing"
  # shellcheck disable=SC2086
  set -- $line
  if [ "$#" -ne 4 ]; then result "${1:-?} error request must be '<id> <w100> <w110> <w120>', got '$line'"; return 0; fi
  id="$1"; shift
  case "$id" in ''|*[!A-Za-z0-9-]*) result "? error invalid request id '$id'"; return 0;; esac
  if ! conf="$(sh "$RENDER" "$@" 2>&1)"; then
    log "rejected $*: $conf"; result "$id error $conf"; return 0
  fi
  printf '%s\n' "$conf" > "$R/routing.conf.new" || { result "$id error could not write $R/routing.conf.new"; return 0; }
  cp "$R/routing.conf" "$R/routing.conf.bak" || { result "$id error could not back up $R/routing.conf"; return 0; }
  mv "$R/routing.conf.new" "$R/routing.conf" || { result "$id error could not replace $R/routing.conf"; return 0; }
  if ! out="$("$NGINX" -t 2>&1)"; then
    mv "$R/routing.conf.bak" "$R/routing.conf"
    log "nginx -t failed for $*, previous routing restored"
    result "$id error nginx -t failed, previous routing restored: $(printf '%s' "$out" | oneline)"; return 0
  fi
  if ! out="$("$NGINX" -s reload 2>&1)"; then
    log "nginx -s reload failed for $*"
    result "$id error nginx -s reload failed: $(printf '%s' "$out" | oneline)"; return 0
  fi
  log "applied weights 1.0.0/1.1.0/1.2.0 = $*"
  result "$id ok $*"
}

[ -d "$Q" ] || { log "$Q is missing (the nginx command creates it); the control panel cannot change routing"; exit 1; }
[ -f "$R/routing.conf" ] || { log "$R/routing.conf is missing"; exit 1; }
if [ "${1:-}" = --once ]; then
  [ -f "$Q/request" ] && apply_one
  exit 0
fi
log "watching $Q for control-panel routing requests"
while :; do
  [ -f "$Q/request" ] && apply_one
  sleep "$INTERVAL"
done
