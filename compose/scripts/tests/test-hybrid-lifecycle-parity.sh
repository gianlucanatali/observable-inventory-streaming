#!/usr/bin/env bash
# Offline guards for hybrid routing/lifecycle parity. Never calls cloud, Docker, or Terraform.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
MAKEFILE="$ROOT/Makefile"
STACK="$ROOT/compose/scripts/stack.sh"
LAYER="$ROOT/compose/scripts/layer.sh"
ECS="$ROOT/terraform/aws/ecs.tf"


fail() { printf 'test-hybrid-lifecycle-parity: %s\n' "$*" >&2; exit 1; }

python3 - "$MAKEFILE" <<'PY' || fail "route-show must live-sync the ALB before displaying routing"
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
body = text[text.index("route-show:\n"):text.index("\nstatus:\n", text.index("route-show:\n"))]
sync, show = body.find("$(ROUTE) --sync"), body.find("$(ROUTE) --show")
if sync < 0 or show < 0 or sync > show: raise SystemExit(1)
PY

python3 - "$MAKEFILE" <<'PY' || fail "route-check must be a hybrid read-only ALB credential gate"
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
if "route-check" not in text[text.index(".PHONY:"):text.index("help:")]: raise SystemExit(1)
body = text[text.index("route-check:\n"):text.index("\nroute-show:\n", text.index("route-check:\n"))]
if '$(MODE)' not in body or '$(TOPOLOGY)' not in body or '$(ROUTE) --sync' not in body: raise SystemExit(1)
PY

python3 - "$MAKEFILE" "$STACK" <<'PY' || fail "hybrid control and status must use ALB URLs"
import sys
from pathlib import Path
makefile, stack = (Path(path).read_text() for path in sys.argv[1:])
control = makefile[makefile.index("control:\n"):makefile.index("\n# Act 2", makefile.index("control:\n"))]
if ('$(TOPOLOGY)' not in control or 'dir=aws out=alb_url' not in control
        or 'terraform -chdir=$(CURDIR)/terraform/$$dir workspace select $(STACK)' not in control
        or 'terraform -chdir=$(CURDIR)/terraform/$$dir output -raw $$out' not in control): raise SystemExit(1)
status = stack[stack.index("status() {"):stack.index("\ndown() {", stack.index("status() {"))]
if 'hybrid_enabled && tf_has_state aws' not in status or 'shop:' not in status or 'control:' not in status: raise SystemExit(1)
if status.index('hybrid_enabled && tf_has_state aws') > status.index('shop:'): raise SystemExit(1)
PY

python3 - "$MAKEFILE" "$LAYER" "$STACK" "$ECS" <<'PY' || fail "hybrid lifecycle paths must follow TOPOLOGY and desired ECS layers"
import sys
from pathlib import Path
makefile, layer, stack, ecs = (Path(path).read_text() for path in sys.argv[1:])
for target in ("offers-on", "offers-off"):
    start = makefile.index(f"{target}:\n")
    body = makefile[start:makefile.find("\n\n", start)]
    if '$(TOPOLOGY)' not in body or 'make layer-' not in body: raise SystemExit(1)
if '[ "$TOPOLOGY" = hybrid ]' not in layer or '[ "$STACK" = hybrid ]' in layer: raise SystemExit(1)
status = layer[layer.index('  status)'):layer.index('  *) echo "usage:', layer.index('  status)'))]
if 'stack-$STACK.layers' not in status: raise SystemExit(1)
if 'inventory-api-110' not in ecs or 'inventory-api-120' not in ecs or '!var.enable_releases' not in ecs: raise SystemExit(1)
offers = stack[stack.index('    offers:on)'):stack.index('    dd-streams:on)', stack.index('    offers:on)'))]
if 'sync_ssm_secrets' not in offers or offers.index('sync_ssm_secrets') > offers.index('tf_apply aws'): raise SystemExit(1)
PY

printf 'test-hybrid-lifecycle-parity: PASS (offline guards)\n'
