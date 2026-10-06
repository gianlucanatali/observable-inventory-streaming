#!/usr/bin/env bash
# Offline guard: `make control` in cloud mode selects the stack's Terraform workspace before reading the URL output
# (like links.sh), and fails loudly when the workspace or the output is missing. Terraform is a stub on PATH.
set -euo pipefail
OVERLAY="$(cd "$(dirname "$0")/../../.." && pwd)"
fail() { printf 'test-make-control: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp" "$OVERLAY/.state/layers-ctltest.env"' EXIT  # the Makefile touches the stack's layer env file
mkdir -p "$tmp/bin"
cat > "$tmp/bin/terraform" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TF_LOG"
case "$*" in
  *"workspace select"*) [ "${TF_NO_WORKSPACE:-}" = 1 ] && { echo "Workspace doesn't exist." >&2; exit 1; }; exit 0;;
  *"output -raw alb_url"*) echo "http://alb.example";;
  *"output -raw ingress_base_url"*) echo "http://vm.example:8080";;
  *) exit 3;;
esac
STUB
chmod +x "$tmp/bin/terraform"
# Cloud-mode make checks that the calibration profile is git-tracked; the standalone suite runs on a non-git copy.
real_git="$(command -v git)"
printf '#!/usr/bin/env bash\ncase " $* " in *" ls-files "*) exit 0;; esac\nexec "%s" "$@"\n' "$real_git" > "$tmp/bin/git"
chmod +x "$tmp/bin/git"
run() { # run <TOPOLOGY> [env...]; prints combined output, returns make's status
  local topo="$1"; shift
  env PATH="$tmp/bin:$PATH" TF_LOG="$tmp/log" ENV_DIR="$tmp" "$@" \
    make --no-print-directory -C "$OVERLAY" MODE=cloud STACK=ctltest TOPOLOGY="$topo" control 2>&1
}
: > "$tmp/log"
out="$(run hybrid)" || fail "hybrid control failed: $out"
case "$out" in *"URL:      http://alb.example/control/"*) ;; *) fail "hybrid URL missing: $out";; esac
first="$(sed -n 1p "$tmp/log")"; second="$(sed -n 2p "$tmp/log")"
case "$first" in *"terraform/aws workspace select ctltest") ;; *) fail "workspace not selected first: $first";; esac
case "$second" in *"terraform/aws output -raw alb_url") ;; *) fail "unexpected second call: $second";; esac
: > "$tmp/log"
out="$(run vm)" || fail "vm control failed: $out"
case "$out" in *"URL:      http://vm.example:8080/control/"*) ;; *) fail "vm URL missing: $out";; esac
grep -q "terraform/vm workspace select ctltest" "$tmp/log" || fail "vm workspace not selected: $(cat "$tmp/log")"
out="$(run hybrid TF_NO_WORKSPACE=1)" && fail "control passed without the workspace: $out"
case "$out" in *"Terraform workspace 'ctltest' is unavailable in terraform/aws"*) ;; *) fail "missing-workspace message: $out";; esac
case "$out" in *"URL:"*) fail "printed a URL without the workspace: $out";; esac
printf 'test-make-control: PASS (workspace selected before the URL output; missing workspace fails)\n'
