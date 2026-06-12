# syntax=docker/dockerfile:1
# PriceDiffs — single-stage build for Railway free tier
#
# Build:  docker build -t pricediff -f Dockerfile .
FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        gcc libxml2-dev libxslt1-dev pkg-config \
        libxml2 libxslt1.1 \
        xvfb supervisor curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && find /usr/local/lib -name __pycache__ -exec rm -rf {} + 2>/dev/null

COPY . .

RUN pip install --no-cache-dir --no-compile . gunicorn \
    && find /app -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null \
    && find /app -name '*.pyc' -delete \
    && rm -rf /root/.cache

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=pricediff.settings \
    DISPLAY=":99"

EXPOSE 8000
ENTRYPOINT ["/app/.docker/entrypoint.sh"]
