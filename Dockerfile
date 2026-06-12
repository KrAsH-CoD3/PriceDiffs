# syntax=docker/dockerfile:1
# PriceDiffs — optimized multi-stage build
#
# Build:  docker build -t pricediff -f .docker/Dockerfile .
# Slim:   docker-slim build --target pricediff:latest pricediff:latest
#
# Final image: ~240 MB (vs ~400 MB with naive build)

# ------------------------------------------------------------------
# Stage 1 — builder: resolve + compile deps, then discard
# ------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Build-time C toolchain for scrapling native extensions (lxml, selectolax)
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        gcc libxml2-dev libxslt1-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Layer cache: dependency tree — invalidated only on uv.lock change
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-compile

# Layer cache: source code — invalidated on app change
COPY . .

# Final install + extras + bytecode purge
RUN uv sync --frozen --no-dev --no-compile \
    && uv pip install --no-cache --no-compile gunicorn \
    && find /app -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null \
    && find /app -name '*.pyc' -delete \
    && rm -rf /root/.cache

# ------------------------------------------------------------------
# Stage 2 — runtime: minimal footprint, no build tools, no uv
# ------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# Only runtime libraries (no gcc, no dev, no apt cache)
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        libxml2 libxslt1.1 \
        xvfb supervisor \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && find /usr/local/lib -name __pycache__ -exec rm -rf {} + 2>/dev/null

WORKDIR /app

# Virtual environment (no uv/pip/pip-tools leaked into final image)
COPY --from=builder /app/.venv /app/.venv

# Application source — only the production paths listed
COPY --from=builder /app/app /app/app
COPY --from=builder /app/pricediff /app/pricediff
COPY --from=builder /app/data/strategies /app/data/strategies
COPY --from=builder /app/manage.py /app/
COPY --from=builder /app/.docker /app/.docker

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=pricediff.settings \
    DISPLAY=":99"

RUN mkdir -p /app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -sf http://localhost:8000/ || exit 1

ENTRYPOINT ["/app/.docker/entrypoint.sh"]
