# Build context: repository root (`docker build -f supplier-sim/Dockerfile .`)
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 --home-dir /app supplier
WORKDIR /app
COPY supplier-sim/pyproject.toml supplier-sim/uv.lock ./
RUN uv sync --frozen --no-dev
COPY supplier-sim/supplier_sim ./supplier_sim
USER supplier
# Compose sets DD_LOGS_INJECTION=true, DD_AGENT_HOST, DD_ENV, DD_SERVICE, DD_VERSION.
ENTRYPOINT ["ddtrace-run", "python", "-m", "supplier_sim"]
