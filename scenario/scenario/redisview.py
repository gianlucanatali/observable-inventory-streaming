"""Reading the serving view and the reset/verify logic that compares it with the source."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

Key = tuple[str, str]
FEED_MAX_AGE_S = 10  # same as the lookup API's "feed:status older than 10 s is unknown"


def active_ns(r) -> str:
    ns = r.get("stock:active_ns")
    if ns is None:
        raise RuntimeError("Redis key stock:active_ns is not set: the projector has not started")
    return ns


def fetch_positions(r, ns: str, keys) -> dict[Key, dict | None]:
    keys = list(keys)
    pipe = r.pipeline(transaction=False)
    for s, p in keys:
        pipe.hmget(f"stock:{ns}:{s}:{p}", "quantity", "revision", "deleted")
    out: dict[Key, dict | None] = {}
    for key, (q, rev, d) in zip(keys, pipe.execute()):
        out[key] = None if rev is None else {"quantity": int(q), "revision": int(rev),
                                             "deleted": d == "1"}
    return out


def diff_source_vs_redis(source: dict[Key, tuple[int, int, bool]],
                         redis_rows: dict[Key, dict | None]) -> list[str]:
    problems = []
    for key in sorted(source):
        q, rev, deleted = source[key]
        got = redis_rows.get(key)
        if got is None:
            problems.append(f"{key[0]}/{key[1]}: missing in Redis (source q={q} rev={rev})")
            continue
        if (got["quantity"], got["revision"], got["deleted"]) != (q, rev, deleted):
            problems.append(f"{key[0]}/{key[1]}: source q={q} rev={rev} deleted={deleted}, "
                            f"redis q={got['quantity']} rev={got['revision']} deleted={got['deleted']}")
    return problems


def fetch_sellable(r, products) -> dict[str, int | None]:
    products = list(products)
    pipe = r.pipeline(transaction=False)
    for p in products:
        pipe.hget(f"sellable:{p}", "sellable")
    return {p: None if v is None else int(v) for p, v in zip(products, pipe.execute())}


def diff_sellable(expected: dict[str, int], got: dict[str, int | None]) -> list[str]:
    problems = []
    for product in sorted(expected):
        if got.get(product) is None:
            problems.append(f"sellable {product}: missing in Redis (sources sum {expected[product]})")
        elif got[product] != expected[product]:
            problems.append(f"sellable {product}: sources sum {expected[product]}, redis {got[product]}")
    return problems


def sum_sources(sources: dict[Key, tuple[int, int, bool]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for (_, product), (q, _, deleted) in sources.items():
        out[product] = out.get(product, 0) + (0 if deleted else q)
    return out


def verify(conns: dict, r, settle_s: float = 30, interval_s: float = 1.0,
           clock: Callable[[], float] = time.time,
           sleep: Callable[[float], None] = time.sleep) -> tuple[list[str], list[str]]:
    """Returns (position_mismatches, sellable_mismatches). conns = {store_id: connection to its source}.

    Positions: each source vs the projector's per-store positions. Sellable: per-product sum over the
    sources vs `sellable:{product}` (Flink path, may lag: retried until settle_s).
    """
    from .source import read_all
    ns = active_ns(r)
    problems: list[str] = []
    all_rows: dict[Key, tuple[int, int, bool]] = {}
    for store, conn in conns.items():
        rows = read_all(conn)
        foreign = sorted({k[0] for k in rows} - {store})
        if foreign:
            problems.append(f"source of {store} holds rows of other store(s) {foreign}")
        all_rows.update(rows)
        problems += diff_source_vs_redis(rows, fetch_positions(r, ns, rows))
    expected = sum_sources(all_rows)
    deadline = clock() + settle_s
    while True:
        sellable_problems = diff_sellable(expected, fetch_sellable(r, expected))
        if not sellable_problems or clock() >= deadline:
            return problems, sellable_problems
        sleep(interval_s)


def find_missing(expected: dict[Key, tuple[int, int]], actual: dict[Key, dict | None]) -> list[str]:
    """Keys whose Redis quantity differs from the baseline or whose revision is older than written."""
    missing = []
    for key in sorted(expected):
        qty, rev = expected[key]
        got = actual.get(key)
        if got is None or got["quantity"] != qty or got["revision"] < rev or got["deleted"]:
            missing.append(f"{key[0]}/{key[1]}")
    return missing


def feed_ok(feed: dict, now_s: float) -> bool:
    try:
        return feed.get("state") == "ok" and now_s * 1000 - int(feed["checked_at_ms"]) <= FEED_MAX_AGE_S * 1000
    except (KeyError, ValueError):
        return False


class ResetTimeout(Exception):
    pass


def wait_for_baseline(r, ns: str, expected: dict[Key, tuple[int, int]], timeout_s: float,
                      interval_s: float = 0.5, clock: Callable[[], float] = time.time,
                      sleep: Callable[[float], None] = time.sleep,
                      expected_sellable: dict[str, int] | None = None) -> None:
    deadline = clock() + timeout_s
    while True:
        missing = find_missing(expected, fetch_positions(r, ns, expected))
        feed = r.hgetall("feed:status")
        sellable_problems = (diff_sellable(expected_sellable, fetch_sellable(r, expected_sellable))
                             if expected_sellable else [])
        if not missing and not sellable_problems and feed_ok(feed, clock()):
            return
        if clock() >= deadline:
            raise ResetTimeout(
                f"reset did not converge in {timeout_s:.0f}s: {len(missing)} position(s) not at the "
                f"baseline in Redis namespace {ns}: {', '.join(missing[:50])}"
                f"{' ...' if len(missing) > 50 else ''}; {len(sellable_problems)} sellable mismatch(es)"
                f"{': ' + '; '.join(sellable_problems[:5]) if sellable_problems else ''}; "
                f"feed:status={feed or 'absent'}")
        sleep(interval_s)


@dataclass
class ResetResult:
    scenario_id: str
    positions: int


def reset(conns: dict, r, timeout_s: float, now: Callable[[], float] = time.time,
          sleep: Callable[[float], None] = time.sleep) -> ResetResult:
    """Restore every source to the seed, then wait for per-store positions, sellable totals and the feed."""
    from .demo_config import read_param
    from .seed_data import expected_sellable, product_weights, seed_rows_for
    from .source import upsert_positions, write_product_weights
    ns = active_ns(r)  # fail before writing anything if the view is not there
    weights = product_weights(read_param(r, "demand_skew"))
    expected: dict[Key, tuple[int, int]] = {}
    rows_all = []
    for store, conn in conns.items():
        rows = seed_rows_for(store)
        if not rows:
            raise RuntimeError(f"no seed rows for store {store}: is it one of S01..S05?")
        try:
            expected.update(upsert_positions(conn, rows, "reset"))
            write_product_weights(conn, weights)
        except Exception as exc:
            raise RuntimeError(f"reset: writing the baseline to the source of {store} failed: {exc!r}") from exc
        rows_all += rows
    scenario_id = "sc-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now()))
    r.set("scenario:current", scenario_id)
    wait_for_baseline(r, ns, expected, timeout_s, clock=now, sleep=sleep,
                      expected_sellable=expected_sellable(rows_all))
    return ResetResult(scenario_id, len(expected))
