"""The load / verify / canary-check runs shared by the CLI (`make load|verify|canary-check`) and the scenario API.

Each function writes its output lines through `emit` (the CLI passes `print`) and returns (exit_code, data):
exit 0 ok, 1 check failed. Errors raise (ConfigError, RuntimeError, ValueError, OSError), as before.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Callable

from .canary import evaluate, verdict
from .config import open_sources, redis_connect, require
from .load import run_load
from .redisview import verify

Emit = Callable[[str], None]


def write_json(path: str, data) -> None:
    """Replace `path` atomically (a reader never sees a half-written summary)."""
    tmp = f"{path}.tmp-{secrets.token_hex(6)}"  # unique across containers (each may be pid 1)
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_run(rps: float, duration_s: float, seed: int, window_s: float, output: str | None,
             emit: Emit) -> tuple[int, dict]:
    base = require(["BASE_URL"])["BASE_URL"].rstrip("/")
    summary = asyncio.run(run_load(base, rps, duration_s, seed, window_s, emit=emit))
    emit(json.dumps({"summary": summary}))
    if output:
        write_json(output, summary)
    return 0, summary


def verify_run(settle_s: float, json_out: str | None, emit: Emit) -> tuple[int, dict]:
    with open_sources() as conns:
        problems, sellable_problems = verify(conns, redis_connect(), settle_s=settle_s)
    for p in problems + sellable_problems:
        emit(p)
    result = {"ok": not problems and not sellable_problems, "mismatches": len(problems),
              "sellable_mismatches": len(sellable_problems)}
    if json_out:
        write_json(json_out, result)
    emit(json.dumps(result))
    return (0 if result["ok"] else 1), result


def canary_check_run(summary_path: str, release_a: str | None, release_b: str, min_samples: int,
                     max_error_rate: float, p95_budget_ms: float, verify_file: str | None,
                     emit: Emit) -> tuple[int, dict]:
    with open(summary_path) as f:
        summary = json.load(f)
    verify_result = None
    if verify_file:
        with open(verify_file) as f:
            verify_result = json.load(f)
    gates = evaluate(summary, release_a, release_b, min_samples, max_error_rate, p95_budget_ms, verify_result)
    for g in gates:
        emit(f"{'PASS' if g.passed else 'FAIL'} {g.name}: {g.detail}")
    ok = all(g.passed for g in gates)
    emit(verdict(gates))
    data = {"ok": ok, "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail, "reason": g.reason}
                                for g in gates],
            "releases": summary["total"]["releases"], "verify": verify_result}
    return (0 if ok else 1), data
