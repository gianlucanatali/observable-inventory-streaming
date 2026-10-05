# Build context: repository root (`docker build -f cost-meter/Dockerfile .`)
FROM python:3.12.15-slim-trixie@sha256:29113dcae7aad06daa8e95260fa09f27d62be33b9687ea3774f771d601a02256
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
WORKDIR /app
COPY cost-meter/pyproject.toml cost-meter/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY cost-meter/cost_meter ./cost_meter
COPY cost-meter/prices.json ./prices.json
RUN useradd --system --uid 10001 costmeter
USER costmeter
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["python", "-m", "cost_meter"]
