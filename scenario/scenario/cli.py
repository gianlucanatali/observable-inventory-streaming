from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time

from .canary import evaluate
from .demo_config import read_param
from .config import ConfigError, open_sources, redis_connect, require
from .load import run_load
from .procurement import (cancel_open_orders, clear_eta_keys, procurement_connect,
                          restock_layer_on, set_lead_time)
from .redisview import ResetTimeout, reset, verify
from .seed_data import product_weights, seed_hash, seed_rows, seed_rows_for
from .source import read_quantity, sell, upsert_positions, write_product_weights


def cmd_seed(args) -> int:
    rows = seed_rows()
    skew = read_param(redis_connect(), "demand_skew")
    weights = product_weights(skew)
    total = 0
    with open_sources() as conns:
        for store, conn in conns.items():
            total += len(upsert_positions(conn, seed_rows_for(store), "seed"))
            write_product_weights(conn, weights)
    print(f"seeded {total} positions into {len(conns)} source(s), seed hash {seed_hash(rows)}, "
          f"product weights for demand skew {skew}")
    return 0


def cmd_reset(args) -> int:
    layer = restock_layer_on()
    cancelled = None
    if layer:  # restock layer on: close open purchase orders first so nothing is delivered onto the baseline
        with contextlib.closing(procurement_connect()) as pc:
            cancelled = cancel_open_orders(pc)
    r = redis_connect()
    with open_sources() as conns:
        result = reset(conns, r, args.timeout)
    extra = ""
    if layer:
        extra = f", {cancelled} open purchase order(s) cancelled, {clear_eta_keys(r)} restock:eta key(s) cleared"
    print(f"reset ok: scenario {result.scenario_id}, {result.positions} positions at baseline, "
          f"sellable caught up, feed ok{extra}")
    return 0


def cmd_lead_time(args) -> int:
    if not restock_layer_on():
        raise RuntimeError("PROCUREMENT_HOST is not set: the restock layer is off, there is no lead time to set")
    with contextlib.closing(procurement_connect()) as pc:
        set_lead_time(pc, args.seconds)
    print(f"lead time set to {args.seconds} s (applies to every open purchase order within a second)")
    return 0


def cmd_sell_out(args) -> int:
    """Sell every store's whole quantity of one product, one store at a time, each in its own source."""
    gap_s = args.gap_s if args.gap_s is not None else read_param(redis_connect(), "sell_out_gap_s")
    with open_sources() as conns:
        stores = list(conns)
        for step, store in enumerate(stores, start=1):
            conn = conns[store]
            qty = read_quantity(conn, store, args.product)
            remaining = 0
            if qty > 0:
                row = sell(conn, store, args.product, qty)
                if row is None:
                    raise RuntimeError(f"sell({store}, {args.product}, {qty}) sold nothing "
                                       f"(stock changed concurrently?)")
                remaining = int(row[0])
            print(json.dumps({"store": store, "sold": qty, "remaining_store": remaining, "step": step}),
                  flush=True)
            if step < len(stores):
                time.sleep(gap_s)
    return 0


def cmd_verify(args) -> int:
    with open_sources() as conns:
        problems, sellable_problems = verify(conns, redis_connect(), settle_s=args.settle_s)
    for p in problems + sellable_problems:
        print(p)
    result = {"ok": not problems and not sellable_problems, "mismatches": len(problems),
              "sellable_mismatches": len(sellable_problems)}
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(result, f)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


def cmd_load(args) -> int:
    base = require(["BASE_URL"])["BASE_URL"].rstrip("/")
    summary = asyncio.run(run_load(base, args.rps, args.duration, args.seed, args.window))
    print(json.dumps({"summary": summary}))
    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f)
    return 0


def cmd_canary_check(args) -> int:
    with open(args.summary) as f:
        summary = json.load(f)
    verify_result = None
    if args.verify_file:
        with open(args.verify_file) as f:
            verify_result = json.load(f)
    gates = evaluate(summary, args.release_a, args.release_b, args.min_samples,
                     args.max_error_rate, args.p95_budget_ms, verify_result)
    for g in gates:
        print(f"{'PASS' if g.passed else 'FAIL'} {g.name}: {g.detail}")
    ok = all(g.passed for g in gates)
    print("CANARY GATES " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scenario")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    s = sub.add_parser("reset")
    s.add_argument("--timeout", type=float, default=120)
    s.set_defaults(fn=cmd_reset)
    s = sub.add_parser("lead-time", help="set the supplier lead time (restock layer)")
    s.add_argument("--seconds", type=int, required=True)
    s.set_defaults(fn=cmd_lead_time)
    s = sub.add_parser("sell-out")
    s.add_argument("--product", default="P0042")
    s.add_argument("--gap-s", type=float, default=None,
                   help="pause between stores, seconds (default: demo:config sell_out_gap_s, else 1.5)")
    s.set_defaults(fn=cmd_sell_out)
    s = sub.add_parser("verify")
    s.add_argument("--settle-s", type=float, default=30, help="retry the sellable comparison this long")
    s.add_argument("--json-out", help="write {ok, mismatches, sellable_mismatches} here, for canary-check --verify-file")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("load")
    s.add_argument("--rps", type=float, default=20)
    s.add_argument("--duration", type=float, required=True, help="seconds")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--window", type=float, default=10, help="summary window, seconds")
    s.add_argument("--output", help="write the final summary JSON here, for canary-check")
    s.set_defaults(fn=cmd_load)
    s = sub.add_parser("canary-check")
    s.add_argument("summary", help="summary JSON written by `load --output`")
    s.add_argument("--release-a", help="baseline release; omit at 100% when it gets no traffic")
    s.add_argument("--release-b", required=True)
    s.add_argument("--min-samples", type=int, default=100)
    s.add_argument("--max-error-rate", type=float, default=0.0)
    s.add_argument("--p95-budget-ms", type=float, default=200)
    s.add_argument("--verify-file")
    s.set_defaults(fn=cmd_canary_check)
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(args.fn(args))
    except (ConfigError, ResetTimeout, RuntimeError, ValueError) as exc:
        print(f"scenario {args.cmd} failed: {exc}", file=sys.stderr)
        sys.exit(2)
