#!/usr/bin/env bash
# Offline guard for die(): red marker only on a terminal without NO_COLOR, plain text otherwise, and the log
# copy stripped of escape codes while keeping the "stack.sh: <message>" text that grep relies on.
set -euo pipefail

STACK_SH="$(cd "$(dirname "$0")/.." && pwd)/stack.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { printf 'test-stack-die-color: %s\n' "$*" >&2; exit 1; }

# The helpers under test, cut out of stack.sh (it runs a command dispatcher at the end, so it cannot be sourced).
sed -n '/^use_color()/,/^first_line()/p' "$STACK_SH" | sed '$d' > "$WORK/helpers.sh"
printf 'note_cause() { :; }\n' > "$WORK/pre.sh"
cat > "$WORK/run.sh" <<'RUN'
source "$1/pre.sh"; source "$1/helpers.sh"
( die "AWS login session unavailable; run: aws login --profile dd-demo" ) 2>&1 | tee "$1/out.log" >/dev/null || true
RUN

check_plain() { # <label> : out.log must be exactly the plain message
  [ "$(cat "$WORK/out.log")" = "stack.sh: AWS login session unavailable; run: aws login --profile dd-demo" ] \
    || fail "$1: expected the plain message, got: $(cat -v "$WORK/out.log")"
}

# 1. stderr is a pipe: no escape codes
bash "$WORK/run.sh" "$WORK"; check_plain "pipe"
# 2. NO_COLOR set, even on a terminal
NO_COLOR=1 STACK_COLOR=1 bash "$WORK/run.sh" "$WORK"; check_plain "NO_COLOR"
# 3. colour decided by run_logged (STACK_COLOR=1): red marker, text unchanged
STACK_COLOR=1 bash "$WORK/run.sh" "$WORK"
grep -q $'\033\\[1;31m✗\033\\[0m stack.sh: AWS login session unavailable' "$WORK/out.log" || fail "STACK_COLOR=1: no red marker: $(cat -v "$WORK/out.log")"
# 4. strip_ansi turns the coloured line back into the plain one
source "$WORK/pre.sh"; source "$WORK/helpers.sh"
strip_ansi "$WORK/out.log"; check_plain "strip_ansi"
# 5. a real terminal gets colour: run through a pty
python3 - "$WORK" <<'PY'
import os, pty, sys
work = sys.argv[1]
pid, fd = pty.fork()
if pid == 0:
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "STACK_COLOR")}
    os.execvpe("bash", ["bash", "-c", f'source {work}/pre.sh; source {work}/helpers.sh; (die boom)'], env)
data = b""
while True:
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    data += chunk
os.waitpid(pid, 0)
assert b"\x1b[1;31m\xe2\x9c\x97" in data, f"no red marker on a pty: {data!r}"
PY
echo "test-stack-die-color: ok"
