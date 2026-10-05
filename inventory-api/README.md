# inventory-api

Availability API, `GET /api/availability/{product_id}` ([contract §5](../contracts/README.md)). One Flask app under gunicorn (sync workers); the three demo releases are one image differing only by env.

| `DD_VERSION` | `CATALOGUE_MODE` | Behaviour |
|---|---|---|
| `1.0.0` | `none` | `product: null`, no catalogue |
| `1.1.0` | `per_request` | open + parse + index the catalogue inside every request (span `catalogue.prepare`) — the regression |
| `1.2.0` | `startup` | same work once per gunicorn worker in `post_fork`, before the worker serves; `/readyz` is 503 until done — the fix |

1.1.0 and 1.2.0 return identical bodies except `release` (tested).

## Environment

Required (startup fails naming the variable): `REDIS_URL`, `DD_VERSION`, `CATALOGUE_MODE`, `STORE_HOSTS` (`S01=store-s01,...`, parsed strictly; only the ids are used). Optional: `CATALOGUE_PATH` (default `/app/catalogue/catalogue.json`), `PORT` (8080), `WEB_CONCURRENCY` (2, max 8), `GUNICORN_THREADS` (1, max 8), `GUNICORN_TIMEOUT` (30), `DD_SERVICE=inventory-api`, `DD_ENV=dd-demo`, `DD_AGENT_HOST`, `DD_DOGSTATSD_HOST/PORT` (default agent host : 8125).
Profiler: set `DD_PROFILING_ENABLED=true` (off by default; ddtrace-run honours it).

## Behaviour notes

- Reads per request, all inside the custom `stock.read` span: pipeline 1 = `stock:active_ns`, `sellable:{product_id}`, `feed:status:{store}` for every store; pipeline 2 (only when a namespace exists; its keys cannot be named before) = `stock:{ns}:meta.ready` and every `stock:{ns}:{store}:{product}`. Two round trips because plain Redis cannot pipeline a dependent key.
- `sellable` is the answer; the per-store `stores` breakdown is detail and may briefly disagree. A store is unknown when its position is missing, the namespace is not ready, or its feed is not `ok`/older than 10 s; any unknown store sets `at_least`. `sellable == 0` with `at_least` is `unknown/stores_unknown`; a missing `sellable:` key is `unknown/not_found`. Never zero or available for unknown.
- Redis exceptions answer 200 `unknown/redis_error`, log with stack, and count `stock.lookup.result{status,unknown_reason,at_least}` (`unknown_reason:none` when not unknown).
- Ids not matching `^P\d{4}$` (e.g. the `__probe__` product) answer 404. The old `/api/stock/...` route is gone.
- A malformed position or sellable hash is an API failure (500, logged), not hidden.
- JSON logs on stdout carry `dd.trace_id`/`dd.span_id`, `dd.service/env/version`.

## Catalogue and calibration

See [catalogue/README.md](catalogue/README.md). The rehearsal profile selects seed 42, N=3000, 6.1 MB, generated in the Docker build and hash-checked. The release containers use four sync workers and a two-CPU limit so the intentional CPU-bound regression can sustain the demo's 5 rps without starving the host. After rebuilding a shared image tag, the three release containers must be recreated so they adopt the new image ID before measuring.

## Tests and image

```
uv run --python 3.12 pytest -q
docker build -f inventory-api/Dockerfile -t inventory-api:dev .   # from the repo root
```
`uv.lock` is committed; dependencies in `pyproject.toml` are exact pins.
