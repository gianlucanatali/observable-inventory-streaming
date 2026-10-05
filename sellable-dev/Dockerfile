# Local-mode implementation of overlay/flink/sellable.sql; used when cloud Flink is unavailable.
# Build context: repository root (`docker build -f sellable-dev/Dockerfile .`)
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" SCHEMA_DIR=/app/contracts/avro
RUN useradd --system --uid 10001 --home-dir /app sellable
WORKDIR /app
COPY sellable-dev/pyproject.toml sellable-dev/uv.lock ./
RUN uv sync --frozen --no-dev
COPY contracts/avro/sellable_key.avsc contracts/avro/sellable.avsc contracts/avro/restock_request_key.avsc contracts/avro/restock_request.avsc \
     contracts/avro/stock_demand_key.avsc contracts/avro/stock_demand.avsc \
     contracts/avro/restock_forecast_key.avsc contracts/avro/restock_forecast.avsc \
     contracts/avro/cart_at_risk_key.avsc contracts/avro/cart_at_risk.avsc ./contracts/avro/
COPY sellable-dev/sellable_dev ./sellable_dev
USER sellable
ENTRYPOINT ["ddtrace-run", "python", "-m", "sellable_dev"]
