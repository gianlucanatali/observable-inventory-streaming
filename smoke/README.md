# smoke

Automated smoke test of the running demo, so a machine, not a person looking at the screen, catches a broken stack.
It runs as a compose service (profile `tools`) on the stack network, the same way locally (Lima VM, project `dd-demo-dev`) and in the cloud (EC2 through the SSH docker context).

```
make smoke                       # from overlay/ ; MODE=cloud STACK=<s> for the cloud stack
make smoke SMOKE_ARGS=--offers   # also posts a cart ADD and ABANDON
make smoke SMOKE_ARGS="--json-out /out/smoke.json"
```

`smoke [--offers] [--json-out PATH]` prints one line per check (`PASS|FAIL  name  detail`), a summary line, and exits 1 on any FAIL. `stack-up` runs it after "verify source vs Redis".

## Checks

1. Connect (`http://connect:8083/connectors?expand=status`): `inventory-s01..s05` and `sellable-redis` exist, every connector and every task is RUNNING (any other connector listed must be RUNNING too).
2. API (`http://nginx/api/availability/{P0042,P0001,P0100}`): HTTP 200, `feed` ok, every store feed ok, status `available` or `out_of_stock` (never `unknown`).
3. Browser (headless chromium): `/` renders product cards; `/#/product/P0042` shows "available online" or "Out of stock online", not "can't be confirmed", no "not live" in the store line, and a footer "Serving release <x>". Console errors, uncaught page errors, failed requests and HTTP >= 400 responses on either page are a FAIL. Screenshots: `/out/smoke-home.png`, `/out/smoke-product.png`.
4. Control panel: `GET /control/` and `/control/api/params` with basic auth `demo` / `CONTROL_PASSWORD` (from `.env.secrets` via compose, never printed): page loads, parameters listed, none in `error`.
5. `--offers`: `POST /api/cart` ADD (expects 201) then ABANDON (201) for P0001. Nothing is sold; the only change is the cart events.

Redis is not read directly: the API answer already covers it.

## Notes

- The screenshots share the `scenario-out` volume with the scenario tool. Read them with `docker compose ... run --rm --entrypoint cat smoke ...` or copy from the volume.
- A product that is genuinely out of stock passes (the check is "the answer is known", not "in stock"). A store paused with `make store-pause` makes its feed non-ok and the smoke fails: that is intended.
- The base image `mcr.microsoft.com/playwright/python:v1.63.0-noble` is pinned by tag and by index digest (linux/amd64 and linux/arm64 manifests verified on mcr.microsoft.com on 2026-10-03). `playwright==1.63.0` in `pyproject.toml` must stay equal to the image tag; bump both together and refresh `uv.lock` (`uv lock`).
- The container build and a real run still need to be exercised against the stack. Selectors come from `storefront/frontend/src` (`data-testid` product-card and stock-panel, `footer.foot`).

## Tests

Pure evaluation logic only (no containers, no browser): `cd smoke && uv run pytest`.
