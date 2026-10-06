"""Canary gates over a load summary (plan section 4). Pure."""
from __future__ import annotations

from dataclasses import dataclass

RELEASES = ("1.0.0", "1.1.0", "1.2.0")  # oldest first; weights are always given in this order
LOW_SHARE_MIN_SAMPLES = 30  # a canary below 50% gets ~60 samples in 120 s at 5 rps; 100 would never pass


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str
    reason: str = ""  # short failure reason for the verdict line; empty when the gate passed


def gate_for_routing(weights) -> dict:
    """Live routing 1.0.0/1.1.0/1.2.0 -> {release_a, release_b, min_samples, label}.

    Two releases with traffic: the newer one is the canary (release_b), the older one the baseline (release_a), whatever
    the split: 90/10/0 gates 1.1.0 against 1.0.0, 90/0/10 and 50/0/50 gate 1.2.0 against 1.0.0, 0/90/10 and 0/50/50 gate
    1.2.0 against 1.1.0. One release alone (other
    than the 1.0.0 baseline) is gated without a baseline: 0/0/100 gates 1.2.0, 0/100/0 gates 1.1.0. A canary below 50% uses
    the lower sample minimum (min_samples 30); otherwise min_samples is None (the CLI/API default, 100).
    """
    weights = tuple(weights)
    if len(weights) != 3 or any(not isinstance(w, int) or isinstance(w, bool) or w < 0 for w in weights) \
            or sum(weights) != 100:
        raise ValueError(f"routing {weights!r} is not three non-negative integer weights summing to 100")
    live = [(r, w) for r, w in zip(RELEASES, weights) if w > 0]
    shown = "/".join(map(str, weights))
    if len(live) == 2:
        (base, _), (canary, share) = live
    elif len(live) == 1 and live[0][0] != RELEASES[0]:
        base, (canary, share) = None, live[0]
    else:
        raise ValueError(f"live routing is {shown} (1.0.0/1.1.0/1.2.0); a canary check needs a newer release next to "
                         "an older one (e.g. 90/10/0, 90/0/10, 50/0/50) or a newer release alone (0/0/100)")
    return {"release_a": base, "release_b": canary, "label": f"{share}%",
            "min_samples": LOW_SHARE_MIN_SAMPLES if share < 50 else None}


def parse_routing(text: str) -> tuple[int, int, int]:
    """"90 10 0" or "90/10/0" -> (90, 10, 0)."""
    parts = text.replace("/", " ").split()
    try:
        weights = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"routing {text!r} is not '<w100> <w110> <w120>'") from exc
    if len(weights) != 3:
        raise ValueError(f"routing {text!r} is not '<w100> <w110> <w120>'")
    return weights  # type: ignore[return-value]


def evaluate(summary: dict, release_a: str | None, release_b: str, min_samples: int,
             max_error_rate: float, p95_budget_ms: float, verify_result: dict | None) -> list[Gate]:
    releases = summary["total"]["releases"]
    gates: list[Gate] = []
    for label, rel in (("a", release_a), ("b", release_b)):
        if rel is None:
            continue
        count = releases.get(rel, {}).get("count", 0)
        ok = count >= min_samples
        gates.append(Gate(f"min_samples_{label}", ok, f"release {rel}: {count} samples, need >= {min_samples}",
                          "" if ok else f"{rel} {count} samples < {min_samples}"))
    for label, rel in (("a", release_a), ("b", release_b)):
        if rel is None:
            continue
        data = releases.get(rel)
        if not data:
            gates.append(Gate(f"error_rate_{label}", False, f"release {rel}: no samples", f"{rel} no samples"))
            continue
        ok = data["error_rate"] <= max_error_rate
        gates.append(Gate(f"error_rate_{label}", ok,
                          f"release {rel}: {data['errors']}/{data['count']} errors "
                          f"({data['error_rate']:.4f}), max {max_error_rate}",
                          "" if ok else f"{rel} errors {data['errors']}/{data['count']} > max rate {max_error_rate:g}"))
    b = releases.get(release_b)
    p95 = None if not b else b["p95_ms"]
    ok = p95 is not None and p95 <= p95_budget_ms
    gates.append(Gate("p95_b", ok,
                      f"release {release_b}: p95 {'n/a' if p95 is None else f'{p95:.0f}'} ms, budget {p95_budget_ms} ms",
                      "" if ok else (f"p95 {release_b} n/a (no samples)" if p95 is None
                                     else f"p95 {release_b} {p95:.0f} ms > budget {p95_budget_ms:g} ms")))
    if verify_result is None:
        gates.append(Gate("correctness", False, "no verify result supplied (not evaluated = fail)", "no verify result"))
    else:
        ok = bool(verify_result.get("ok"))
        n = verify_result.get('mismatches', '?')
        gates.append(Gate("correctness", ok, f"verify: {n} mismatch(es)", "" if ok else f"verify {n} mismatch(es)"))
    return gates


def verdict(gates: list[Gate]) -> str:
    """The last line of a canary check: PASSED, or FAILED with each failed gate's reason and the action to take."""
    failed = [g.reason or g.name for g in gates if not g.passed]
    if not failed:
        return "CANARY GATES PASSED"
    return f"CANARY GATES FAILED: {'; '.join(failed)}; roll back"
