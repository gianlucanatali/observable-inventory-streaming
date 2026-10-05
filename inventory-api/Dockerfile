# Build context: repository root (`docker build -f inventory-api/Dockerfile .`)
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256 AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
WORKDIR /app
COPY inventory-api/pyproject.toml inventory-api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY inventory-api/app ./app
COPY inventory-api/gunicorn.conf.py ./
COPY inventory-api/catalogue/generate.py ./catalogue/generate.py
# Deterministic fixture, hash verified (value recorded in catalogue/README.md).
ARG CATALOGUE_PRODUCTS=3500
RUN case "${CATALOGUE_PRODUCTS}" in \
      2000) CATALOGUE_SHA256=e16118e2593a8d33c2021cc0870b946fffd782a92e6e1887335cec2a0e95e8df ;; \
      2500) CATALOGUE_SHA256=1ff388fae2df0f93c693d9a2a68d07ec7d6c857c79d78bdce229162cb4e0fc3c ;; \
      3000) CATALOGUE_SHA256=8ff7374ed39c4c888ecd0a742dc2eba02899995b9c450b05f4fd248d3e7f7357 ;; \
      3500) CATALOGUE_SHA256=418b973b8670ddee40db412266bde90e824097decf784b0beed442a86cd18ca1 ;; \
      4000) CATALOGUE_SHA256=d2e3e6c869a3d04fc8ff4e198c82b3dd8e31a35de70596fc46af3feba09c4dce ;; \
      8000) CATALOGUE_SHA256=3320f08aaf5f8f1bb4c05ce2e21e4d93fc2d6ab32f7a029bb8984554e699a19f ;; \
      *) echo "unsupported CATALOGUE_PRODUCTS=${CATALOGUE_PRODUCTS}; choose 2000, 2500, 3000, 3500, 4000 or 8000" >&2; exit 2 ;; \
    esac \
 && .venv/bin/python catalogue/generate.py --seed 42 --products "${CATALOGUE_PRODUCTS}" --out catalogue/catalogue.json \
 && echo "${CATALOGUE_SHA256}  catalogue/catalogue.json" | sha256sum -c -

FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
USER 10001
EXPOSE 8080
# Env: REDIS_URL, DD_VERSION, CATALOGUE_MODE (none|per_request|startup), DD_SERVICE=inventory-api, DD_ENV
CMD ["ddtrace-run", "gunicorn", "-c", "gunicorn.conf.py", "app.wsgi:app"]
