"""Deterministic open-loop HTTP load generator with per-release counting."""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections import defaultdict
from dataclasses import dataclass

from .seed_data import PRODUCTS

UNKNOWN_RELEASE = "unknown"


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an ascending list; None when empty."""
    if not sorted_values:
        return None
    rank = max(1, -(-len(sorted_values) * pct // 100))  # ceil
    return sorted_values[int(rank) - 1]


def request_plan(seed: int, n: int) -> list[str]:
    """Product ids P0001..P0200, Zipf-like (weight 1/rank) so a few are hot. Same seed, same plan."""
    rng = random.Random(seed)
    weights = [1 / (i + 1) for i in range(len(PRODUCTS))]
    return rng.choices(PRODUCTS, weights=weights, k=n)


@dataclass
class Sample:
    release: str
    status: str  # HTTP code as text, or "error" for transport failures
    latency_ms: float

    @property
    def is_error(self) -> bool:
        return self.status == "error" or int(self.status) >= 500


def summarize(samples: list[Sample]) -> dict:
    by_rel: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_rel[s.release].append(s)
    releases = {}
    for rel in sorted(by_rel):
        group = by_rel[rel]
        lat = sorted(s.latency_ms for s in group if s.status != "error")
        codes: dict[str, int] = defaultdict(int)
        for s in group:
            codes[s.status] += 1
        errors = sum(1 for s in group if s.is_error)
        releases[rel] = {"count": len(group), "errors": errors,
                         "error_rate": errors / len(group), "status": dict(sorted(codes.items())),
                         "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95),
                         "p99_ms": percentile(lat, 99)}
    return {"count": len(samples), "releases": releases}


async def run_load(base_url: str, rps: float, duration_s: float, seed: int, window_s: float,
                   timeout_s: float = 10.0, emit=lambda line: print(line, flush=True)) -> dict:
    import httpx
    n = int(rps * duration_s)
    plan = request_plan(seed, n)
    windows: dict[int, list[Sample]] = defaultdict(list)
    scheduled: dict[int, int] = defaultdict(int)
    closed: set[int] = set()
    emitted: set[int] = set()
    all_samples: list[Sample] = []

    def maybe_emit(w: int) -> None:
        if w in closed and w not in emitted and len(windows[w]) == scheduled[w]:
            emitted.add(w)
            emit(json.dumps({"window": w, "window_s": window_s, **summarize(windows[w])}))

    async def one(client, i: int, product: str, w: int) -> None:
        t0 = time.perf_counter()
        try:
            resp = await client.get(f"{base_url}/api/availability/{product}")
            sample = Sample(resp.headers.get("X-Release", UNKNOWN_RELEASE), str(resp.status_code),
                            (time.perf_counter() - t0) * 1000)
        except httpx.HTTPError as exc:
            sample = Sample(UNKNOWN_RELEASE, "error", (time.perf_counter() - t0) * 1000)
            emit(json.dumps({"event": "transport_error", "request": i, "error": repr(exc)}))
        windows[w].append(sample)
        all_samples.append(sample)
        maybe_emit(w)

    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    start = time.perf_counter()
    tasks = []
    async with httpx.AsyncClient(timeout=timeout_s, limits=limits) as client:
        for i, product in enumerate(plan):
            delay = start + i / rps - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            w = int((i / rps) // window_s)
            for done in range(w):
                closed.add(done)
                maybe_emit(done)
            scheduled[w] += 1
            tasks.append(asyncio.create_task(one(client, i, product, w)))
        for w in list(scheduled):
            closed.add(w)
        await asyncio.gather(*tasks)
        for w in sorted(scheduled):
            maybe_emit(w)
    return {"rps": rps, "duration_s": duration_s, "seed": seed, "base_url": base_url,
            "total": summarize(all_samples)}
