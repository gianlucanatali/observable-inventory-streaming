# Build context: repository root (`docker build -f offer-worker/Dockerfile .`)
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" SCHEMA_DIR=/app/contracts/avro CATALOGUE_FILE=/app/catalogue/products.json
RUN useradd --system --uid 10001 --home-dir /app offers
WORKDIR /app
COPY offer-worker/pyproject.toml offer-worker/uv.lock ./
RUN uv sync --frozen --no-dev
COPY contracts/avro/cart_at_risk_key.avsc contracts/avro/cart_at_risk.avsc contracts/avro/offer_key.avsc contracts/avro/offer.avsc ./contracts/avro/
# Same catalogue file the storefront serves (read-only copy at build time).
COPY storefront/backend/app/products.json ./catalogue/products.json
COPY offer-worker/offer_worker ./offer_worker
USER offers
# Compose sets DD_DATA_STREAMS_ENABLED=true, DD_LOGS_INJECTION=true, DD_AGENT_HOST, DD_ENV, DD_SERVICE, DD_VERSION.
ENTRYPOINT ["ddtrace-run", "python", "-m", "offer_worker"]
