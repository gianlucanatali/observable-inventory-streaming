# jr (synthetic background sales)

The image is built from the public [`gianlucanatali/jr`](https://github.com/gianlucanatali/jr) fork in `vendor/jr`, branch `sql-producer`. That branch adds the SQL producer used for the deterministic background sales below.

- Five containers `jr-sales-s01`..`jr-sales-s05`, one per store source. Each has `PG_HOST=store-s0n`, a fixed store in the
  embedded template and seed `42 + n`, so the run is repeatable.
- `sales.json`: statement mode, one `sell(store, product, 1)` per generated record, but only when the tick "wins":
  jr picks a product at random every tick, and the statement swaps it for a non-existent one (`'__never__'`, `sell()`
  then changes nothing) unless

  ```sql
  random() < (SELECT value FROM demo_setting WHERE key = 'sales_per_min_per_store') / 240
             * COALESCE((SELECT weight FROM product_weight WHERE product_id = :product_id::text), 0)
  ```

  So the sale rate per store is `sales_per_min_per_store` (live, written by demo-control into `demo_setting`; default 24 =
  0.4/s per store, 2/s over five stores) and product `p` sells in proportion to its `product_weight` (mean 1; Zipf-like
  from `scenario seed|reset`, `demand_skew`). The probability is not capped at 1 in SQL: a very popular product at a high
  rate simply sells on every tick, so the realised rate is a little below the setting when skew is high. A missing weight
  (before the first `scenario seed`) means weight 0: no background sales. `sell()` changes nothing when the position is
  out of stock or soft-deleted, so random sales never break the stock rules, and it stamps `change_reason = 'sale'`.
  The statement was run against a real PostgreSQL with the source schema (`contracts/source/001..003`): valid SQL,
  `sell()` returns no row for `'__never__'`. jr's own parameter substitution (`:name`) is unchanged from before
  (`:product_id` was already used twice) but has not been run end to end here.
- The sell-out product `P0042` is excluded **in the SQL** in every store (explicit CASE branch, and weight 0), so
  background sales can never touch it; only `make sell-out` sells it.
- Frequency `JR_SALES_FREQUENCY` per container, default **250ms** (`--frequency 250ms`): 240 ticks/min, which is why the
  rate divides by 240. `make reset` restores quantities.

Run with the compose profile `jr` (`make sales-on` / `make sales-off`).
