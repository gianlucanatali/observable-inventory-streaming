# scenario

CLI used by the Makefile and runbook. Its contracts are in [`../contracts/README.md`](../contracts/README.md), sections 1, 4, 5 and 9. It operates against the five configured store sources and fails loudly when a verification or canary gate does not pass.

```
uv run --python 3.12 scenario <command>
uv run --python 3.12 pytest -q
```

Environment (untracked `.env`): `STORE_HOSTS` (`S01=store-s01,...`, strict; order = sell-out order), `PG_DATABASE`, `PG_WRITER_USER`, `PG_WRITER_PASSWORD`, optional `PG_PORT` (5432), `REDIS_URL`, and `BASE_URL` for `load`. A missing variable exits 2 naming it. Exit codes: 0 ok, 1 check failed (verify mismatch, gate failed), 2 error.

| Command | What it does |
|---|---|
| `seed` | Idempotent absolute upsert into each source of **its own store's** 200 products, quantities 0..40 from `random.Random(42)`; P0042 per store S01..S05 = 2,1,3,1,2 (sellable 9). Hash over all `store,product,qty` lines: `71e7275f...6f46d` (full: `71e7275f78d34a653b7a7870ebdae596f30abbb6540055fa2a7d716446307f46`). Triggers give new revisions on every run. The transaction sets `app.change_reason = 'seed'`, so stock-projector publishes these as ADJUST. Also (re)writes `product_weight` in every source: Zipf-like weights (seeded shuffle of the products, `1/rank^skew`, mean 1, `P0042` = 0) with `skew` read from Redis `demo:config` field `demand_skew` (default 1.0 when absent; needs `REDIS_URL`). |
| `reset [--timeout 120]` | Writes the baseline to all five sources (higher revisions, `change_reason = 'reset'` so they become ADJUST movements, not sales/restocks) and rewrites `product_weight` with the current `demand_skew`, sets `scenario:current`, then waits until every seeded position in Redis has the baseline quantity and a revision at least as new as written, every `sellable:{product}` equals the per-product sum, and `feed:status` is `ok` and checked within 10 s. Timeout lists missing keys, sellable mismatches and the feed state. Never truncates Redis or touches offsets. |
| `sell-out [--product P0042] [--gap-s S]` | `--gap-s` defaults to Redis `demo:config` `sell_out_gap_s` (registry default 1.5 when absent). For each store in `STORE_HOSTS` order, sells the store's whole current quantity via `sell(store, product, qty)` in that store's own source and prints `{"store","sold","remaining_store","step"}`; sleeps the gap between stores. A store at 0 prints `sold: 0`. Replaces `sell-last-pair`. |
| `verify [--settle-s 30] [--json-out f]` | Per store: its source rows (probe excluded) vs Redis positions (quantity, revision, deleted). Per product: sum of non-deleted source quantities vs `sellable:{product}.sellable` (Flink path, retried up to `--settle-s`). Prints mismatches, exit 1 on any. Output `{ok, mismatches, sellable_mismatches}`; `--json-out` writes it. |
| `load --duration S [--rps 20] [--seed 42] [--window 10] [--output f]` | Open-loop generator against `BASE_URL/api/availability/{product_id}` (P0001..P0200, Zipf-like weight 1/rank, seeded) at a fixed arrival rate. Counts by `X-Release` and status; transport failures count as errors under release `unknown`. Prints one JSON line per window, then a final summary; `--output` saves it. |
| `canary-check SUMMARY --release-b 1.2.0 [--release-a 1.1.0] [--min-samples 100] [--max-error-rate 0] [--p95-budget-ms 200] [--verify-file f]` | Gates: min samples per release, error rate per release, p95 of b vs budget, correctness from the `verify --json-out` file (no file = fail, never green from missing data). Omit `--release-a` at 100% when no baseline traffic exists. Exit 1 if any gate fails. |

Typical canary step: `load --duration 60 --output s.json`, `verify --json-out v.json`, `canary-check s.json --release-a 1.1.0 --release-b 1.2.0 --verify-file v.json`.

## Restock layer commands

Active only when `PROCUREMENT_HOST` is set (restock layer on); also needs `PROCUREMENT_DATABASE`, `PROCUREMENT_USER`, `PROCUREMENT_PASSWORD` (and `PG_PORT`, default 5432).

- `scenario lead-time --seconds N`: sets `procurement_config.lead_time_s`; supplier-sim applies it to every open order within a second. Fails if `PROCUREMENT_HOST` is unset.
- `scenario reset`: with the layer on it first marks all open purchase orders cancelled (`cancelled_at`, supplier-sim ignores them) and, after the baseline is back, deletes `restock:eta:*`. With the layer off nothing procurement-related is touched. A restock request still travelling through Flink and the JDBC sink at reset time can open a new order afterwards; this race is not measured by the reset command.
