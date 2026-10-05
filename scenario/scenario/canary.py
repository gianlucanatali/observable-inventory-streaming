"""Canary gates over a load summary (plan section 4). Pure."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


def evaluate(summary: dict, release_a: str | None, release_b: str, min_samples: int,
             max_error_rate: float, p95_budget_ms: float, verify_result: dict | None) -> list[Gate]:
    releases = summary["total"]["releases"]
    gates: list[Gate] = []
    for label, rel in (("a", release_a), ("b", release_b)):
        if rel is None:
            continue
        count = releases.get(rel, {}).get("count", 0)
        gates.append(Gate(f"min_samples_{label}", count >= min_samples,
                          f"release {rel}: {count} samples, need >= {min_samples}"))
    for label, rel in (("a", release_a), ("b", release_b)):
        if rel is None:
            continue
        data = releases.get(rel)
        if not data:
            gates.append(Gate(f"error_rate_{label}", False, f"release {rel}: no samples"))
            continue
        gates.append(Gate(f"error_rate_{label}", data["error_rate"] <= max_error_rate,
                          f"release {rel}: {data['errors']}/{data['count']} errors "
                          f"({data['error_rate']:.4f}), max {max_error_rate}"))
    b = releases.get(release_b)
    p95 = None if not b else b["p95_ms"]
    gates.append(Gate("p95_b", p95 is not None and p95 <= p95_budget_ms,
                      f"release {release_b}: p95 {p95} ms, budget {p95_budget_ms} ms"))
    if verify_result is None:
        gates.append(Gate("correctness", False, "no verify result supplied (not evaluated = fail)"))
    else:
        gates.append(Gate("correctness", bool(verify_result.get("ok")),
                          f"verify: {verify_result.get('mismatches', '?')} mismatch(es)"))
    return gates
