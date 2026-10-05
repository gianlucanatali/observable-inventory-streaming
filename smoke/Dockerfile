# Build context: repository root (`docker build -f smoke/Dockerfile .`)
# Tool image: runs `smoke` on the compose network (profile `tools`). Playwright 1.63.0 must match pyproject.toml.
# Index digest of v1.63.0-noble (linux/amd64 + linux/arm64) read from mcr.microsoft.com on 2026-10-03.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv
WORKDIR /app
# The browsers live in the base image (PLAYWRIGHT_BROWSERS_PATH); the venv only needs the matching Python package.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY smoke/pyproject.toml smoke/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY smoke/smoke ./smoke
# /out is the scenario-out volume, owned by the scenario tool's uid 10001: run as that uid so both tools can write it.
# pwuser comes with the base image; only its uid changes.
RUN uv sync --frozen --no-dev && usermod -u 10001 pwuser && chown -R pwuser /home/pwuser && mkdir -p /out && chown pwuser /out
USER pwuser
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ENTRYPOINT ["smoke"]
