# Build context is overlay/ :  docker build -f storefront/Dockerfile overlay/
# (so the Avro contracts can be copied). Base images are pinned by digest; see the image references below.
FROM node:22.23.3-trixie-slim@sha256:b26b04c123d9ff8ab646ceb18b9d75a1173acf64b9a401094b906d27b29338d4 AS frontend
WORKDIR /build
COPY storefront/frontend/package.json storefront/frontend/package-lock.json ./
RUN npm ci
COPY storefront/frontend/ ./
RUN npm run build

FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256 AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY storefront/backend/pyproject.toml storefront/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY storefront/backend/app ./app
COPY contracts/avro ./contracts/avro
COPY --from=frontend /build/dist ./static
RUN useradd --system --uid 10001 --no-create-home storefront
USER storefront
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DD_SERVICE=storefront \
    DD_LOGS_INJECTION=true \
    DD_DATA_STREAMS_ENABLED=true \
    AVRO_DIR=/app/contracts/avro \
    STATIC_DIR=/app/static
EXPOSE 8000
# One worker: the offers map and consumer thread live in-process. Threads give concurrency.
CMD ["ddtrace-run", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--worker-class", "gthread", "--threads", "8", "--access-logfile", "-", "--no-control-socket", "app.wsgi:app"]
