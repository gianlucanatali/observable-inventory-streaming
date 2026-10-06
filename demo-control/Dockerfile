# Build context: repository root (`docker build -f demo-control/Dockerfile .`)
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" SCHEMA_DIR=/app/contracts/avro REGISTRY_FILE=/app/contracts/demo-params.json \
    DD_SERVICE=demo-control DD_LOGS_INJECTION=true
RUN useradd --system --uid 10001 --home-dir /app control
WORKDIR /app
COPY demo-control/pyproject.toml demo-control/uv.lock ./
RUN uv sync --frozen --no-dev
COPY contracts/demo-params.json ./contracts/demo-params.json
COPY contracts/avro/demo_config_key.avsc contracts/avro/demo_config.avsc ./contracts/avro/
COPY demo-control/demo_control ./demo_control
COPY scenario/scenario ./scenario
USER control
EXPOSE 8000
# One worker: startup seeds demo.config defaults. Compose sets DD_AGENT_HOST, DD_ENV, DD_VERSION, CONTROL_PASSWORD, ...
CMD ["ddtrace-run", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--worker-class", "gthread", "--threads", "4", "--access-logfile", "-", "--no-control-socket", "demo_control.wsgi:app"]
