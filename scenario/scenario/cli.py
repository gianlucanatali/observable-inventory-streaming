from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time

from .demo_config import read_param
from .config import ConfigError, open_sources, redis_connect
from .procurement import (DEFAULT_LEAD_TIME_S, cancel_open_orders, clear_eta_keys, procurement_connect,
                          restock_layer_on, set_lead_time)
from .redisview import ResetTimeout, reset
from .canary import LOW_SHARE_MIN_SAMPLES, gate_for_routing, parse_routing
from .runs import canary_check_run, load_run, verify_run
from .seed_data import product_weights, seed_hash, seed_rows, seed_rows_for
from .source import read_quantity, sell, upsert_positions, write_product_weights

DEFAULT_MIN_SAMPLES = 100  # same as the API's canary-check default (scenario/api.py BOUNDS)


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
    if layer:  # restock layer on: restore the lead time, then close open purchase orders so nothing is delivered onto the baseline
        # `make lead-time` writes procurement_config only (supplier-sim's source), so that is all this restores.
        with contextlib.closing(procurement_connect()) as pc:
            set_lead_time(pc, DEFAULT_LEAD_TIME_S)
            cancelled = cancel_open_orders(pc)
    r = redis_connect()
    with open_sources() as conns:
        result = reset(conns, r, args.timeout)
    extra = ""
    if layer:
        extra = f", lead time restored to {DEFAULT_LEAD_TIME_S} s, {cancelled} open purchase order(s) cancelled, {clear_eta_keys(r)} restock:eta key(s) cleared"
    print(f"reset ok: scenario {result.scenario_id}, {result.positions} positions at baseline, "
          f"sellable caught up, feed ok{extra}")
    return 0


def cmd_lead_time(args) -> int:
    if not restock_layer_on():
        raise RuntimeError("PROCUREMENT_HOST is not set: the restock layer is off, there is no lead time to set")
    with contextlib.closing(procurement_connect()) as pc:
        set_lead_time(pc, args.seconds)
    print(f"lead time set to {args.seconds} s (applies to every open purchase order on supplier-sim's next cycle, every second)")
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


def _out(line: str) -> None:
    print(line, flush=True)


def cmd_verify(args) -> int:
    code, _ = verify_run(args.settle_s, args.json_out, _out)
    return code


def cmd_load(args) -> int:
    code, _ = load_run(args.rps, args.duration, args.seed, args.window, args.output, _out)
    return code


def canary_releases(args) -> tuple[str | None, str, int]:
    """--release-b [--release-a] as given, or both inferred from --routing (the live 1.0.0/1.1.0/1.2.0 weights)."""
    if (args.routing is None) == (args.release_b is None):
        raise ValueError("give exactly one of --release-b or --routing")
    if args.routing is None:
        return args.release_a, args.release_b, (DEFAULT_MIN_SAMPLES if args.min_samples is None else args.min_samples)
    if args.release_a is not None:
        raise ValueError("--release-a is inferred from --routing; do not give both")
    gate = gate_for_routing(parse_routing(args.routing))  # ValueError -> "scenario canary-check failed: ..." exit 2
    min_samples = args.min_samples if args.min_samples is not None else gate["min_samples"] or DEFAULT_MIN_SAMPLES
    base = f"baseline {gate['release_a']}" if gate["release_a"] else "no baseline"
    _out(f"routing {args.routing.replace(' ', '/')} (1.0.0/1.1.0/1.2.0): canary {gate['release_b']} at {gate['label']}, "
         f"{base}, min samples {min_samples}")
    return gate["release_a"], gate["release_b"], min_samples


def cmd_canary_check(args) -> int:
    release_a, release_b, min_samples = canary_releases(args)
    code, _ = canary_check_run(args.summary, release_a, release_b, min_samples,
                               args.max_error_rate, args.p95_budget_ms, args.verify_file, _out)
    return code


def cmd_api(args) -> int:
    from .api import serve
    serve(args.port)
    return 0


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
    s.add_argument("--release-b", help="canary release (or give --routing)")
    s.add_argument("--routing", help="live weights '<w100> <w110> <w120>': canary = newer release with traffic, "
                                     "baseline = older one (90 10 0 gates 1.1.0 vs 1.0.0)")
    s.add_argument("--min-samples", type=int, default=None,
                   help=f"default {DEFAULT_MIN_SAMPLES}; {LOW_SHARE_MIN_SAMPLES} with --routing when the canary is below 50%%")
    s.add_argument("--max-error-rate", type=float, default=0.0)
    s.add_argument("--p95-budget-ms", type=float, default=200)
    s.add_argument("--verify-file")
    s.set_defaults(fn=cmd_canary_check)
    s = sub.add_parser("api", help="serve the token-protected scenario REST API (long-running; see scenario/api.py)")
    s.add_argument("--port", type=int, default=8090)
    s.set_defaults(fn=cmd_api)
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(args.fn(args))
    except (ConfigError, ResetTimeout, RuntimeError, ValueError) as exc:
        print(f"scenario {args.cmd} failed: {exc}", file=sys.stderr)
        sys.exit(2)
