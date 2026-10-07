#!/usr/bin/env bash
# Offline guard: stack.sh layer defaults. No LAYERS means core only; LAYERS=all means every layer except control-center;
# an explicit list is taken as written; control-center is only valid by name in hybrid; unknown names fail loudly.
set -euo pipefail
OVERLAY="$(cd "$(dirname "$0")/../../.." && pwd)"
STACK_SH="$OVERLAY/compose/scripts/stack.sh"
fail() { printf 'test-stack-layers: %s\n' "$*" >&2; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

grep -q 'set_layers "${LAYERS:-core}"' "$STACK_SH" || fail "stack-up must default LAYERS to core"
grep -q 'set_layers "${LAYERS:-all}"' "$STACK_SH" && fail "stack-up must not default LAYERS to all"

# Run the real layer functions of stack.sh in isolation (die is stubbed to a loud exit).
run() { # run <topology> <LAYERS value or -> : prints the layers file
  local topo="$1" want="$2"
  ( LAYERS_FILE="$tmp/layers"; TOPOLOGY="$topo"; : > "$tmp/layers"
    die() { echo "die: $*" >&2; exit 9; }
    eval "$(grep -E '^(ALL_LAYERS|ALL_FLINK)=' "$STACK_SH")"
    VALID_LAYERS="$ALL_LAYERS"
    eval "$(grep -E '^\[ "\$TOPOLOGY" = hybrid \] && VALID_LAYERS=' "$STACK_SH")"
    eval "$(sed -n '/^set_layers() {/,/^}/p' "$STACK_SH")"
    if [ "$want" = - ]; then set_layers "${LAYERS_UNSET:-core}"; else set_layers "$want"; fi
    tr '\n' ' ' < "$LAYERS_FILE" )
}
out="$(run hybrid core)"; [ -z "${out// /}" ] || fail "core must enable no optional layer: '$out'"
out="$(run hybrid all)"
for l in releases restock offers dd-streams dd-synthetics dd-rum; do case " $out " in *" $l "*) ;; *) fail "all lacks $l: '$out'";; esac; done
case " $out " in *" control-center "*) fail "all must not include control-center: '$out'";; esac
out="$(run hybrid core,restock,control-center)"
[ "$out" = "restock control-center " ] || fail "explicit list changed: '$out'"
run hybrid bogus >/dev/null 2>&1 && fail "unknown layer accepted"
run local control-center >/dev/null 2>&1 && fail "control-center accepted outside hybrid"
# Terraform layer toggles default to off, so a stack without a layer never gets it by omission. stack.sh passes every
# toggle explicitly (vars_of), so an explicit layer list produces the same plan as before.
python3 - "$OVERLAY/terraform" <<'PY' || fail "a Terraform layer toggle does not default to false"
import re, sys
from pathlib import Path
root = Path(sys.argv[1])
want = {"cloud": ["enable_restock", "enable_offers", "enable_dd_streams", "enable_control_center"],
        "aws": ["enable_releases", "enable_offers", "enable_dd_rum"],
        "datadog": ["enable_releases", "enable_restock", "enable_offers", "enable_dd_streams", "enable_dd_synthetics", "enable_dd_rum"],
        "vm": ["enable_dd_synthetics", "enable_control_center"]}
bad = []
for d, names in want.items():
    text = (root / d / "variables.tf").read_text()
    for n in names:
        m = re.search(r'variable "%s" \{.*?default\s*=\s*(\w+)' % n, text, re.S)
        if not m or m.group(1) != "false":
            bad.append(f"{d}/{n}")
if bad:
    print("not default false:", ", ".join(bad), file=sys.stderr); sys.exit(1)
PY
python3 - "$STACK_SH" <<'PY' || fail "vars_of must pass every layer toggle explicitly"
import re, sys
t = open(sys.argv[1]).read()
v = t[t.index("vars_of() {"):t.index("vars_of() {") + 6000]
for n in ["enable_restock", "enable_offers", "enable_control_center", "enable_dd_streams", "enable_dd_synthetics", "enable_releases", "enable_dd_rum"]:
    if "-var=%s=" % n not in v:
        print("missing -var=" + n, file=sys.stderr); sys.exit(1)
PY
echo "test-stack-layers: ok"
