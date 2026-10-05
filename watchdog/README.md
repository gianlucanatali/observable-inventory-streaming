# watchdog

One small long-running process that decides whether the stock feed can be trusted (contract: [`../contracts/README.md`](../contracts/README.md) sections 1, 4, 7; terms: Probe, Freshness and Unknown stock are defined in the workshop guide).

- One probe per source. Every `PROBE_INTERVAL_S` it upserts (`S0n`, `__probe__`) in each store's PostgreSQL (hosts from `STORE_HOSTS`) as `stock_writer` (quantity = epoch seconds mod 1,000,000). The trigger assigns the revision; the returned revision and `changed_at` are tracked as a pending write per store. One database failing is counted and logged and marks only that store `unknown`; the others keep being probed.
- Every `CHECK_INTERVAL_S` it reads `stock:{active_ns}:S0n:__probe__` per store. Probe age = now minus `changed_at` of the oldest pending write whose revision is not yet visible (0 when caught up).
- State per store: `ok` (age <= `STALE_AFTER_S`), `stale` (older), `unknown` (last probe write failed, Redis unreadable or `stock:active_ns` missing, or no successful write yet). Missing data is never green.
- Writes `feed:status:{store}` and the aggregate `feed:status` (worst state, max `probe_age_ms`, -1 when not computable; plus `checked_at_ms`). Metrics via DogStatsD: `stock.probe.age` (s), `stock.feed.state` (1/0/-1), `stock.probe.write_errors`, each tagged `store:S0n`; `stock.sellable.age` (s the Flink + sink path is behind the newest probe write, from `sellable:__probe__.last_changed_at_ms`; 0 when caught up; not emitted before the first write).
- Every `CONNECT_INTERVAL_S` (10) it GETs `CONNECT_URL/connectors/<name>/status` for `inventory-s0n` (one per store) and `sellable-redis` (override `SELLABLE_CONNECTOR`) and emits `stock.connect.task_running{connector}` (1 only if the connector and all tasks, at least one, are RUNNING; any error is 0 and logged).
- It shares the demo host: also configure Datadog no-data/host-down monitors on these metrics. Probe age uses the PostgreSQL `changed_at` against this host's clock (same host in the demo; document NTP otherwise).
- Logs are JSON on stdout. `ddtrace` is optional (not a dependency): if installed, psycopg/redis/requests are patched.

## Configuration

Required: `STORE_HOSTS` (`S01=store-s01,...`, strict), `PG_DATABASE`, `PG_WRITER_USER`, `PG_WRITER_PASSWORD`, `REDIS_URL`, `CONNECT_URL`. Optional: `PG_PORT` (5432), `PROBE_INTERVAL_S` (5), `CHECK_INTERVAL_S` (1), `STALE_AFTER_S` (15), `CONNECT_INTERVAL_S` (10), `DD_AGENT_HOST` (localhost), `DD_DOGSTATSD_PORT` (8125). Bad or missing values exit with code 2 naming each one. Values live in the untracked `.env`.

## Run and test

```
uv run --python 3.12 python -m stock_watchdog
uv run --python 3.12 pytest -q
docker build -f watchdog/Dockerfile -t dd-watchdog .   # from the repo root
```
