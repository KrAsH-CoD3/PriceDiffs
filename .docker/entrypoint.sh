#!/usr/bin/env bash
# PriceDiffs container entrypoint.
# Default (CMD omitted or "web"):  run migrations, collectstatic, start Gunicorn.
# Pass "worker":                  start Celery worker.
# Pass "shell":                   drop into a bash shell.
set -euo pipefail

cd /app

case "${1:-web}" in
  web)
    echo "[entrypoint] Running migrations..."
    python manage.py migrate --noinput

    echo "[entrypoint] Collecting static files..."
    python manage.py collectstatic --noinput 2>/dev/null || true

    Xvfb :99 -screen 0 1024x768x24 -ac & sleep 0.5

    PORT="${PORT:-8000}"
    echo "[entrypoint] Starting Gunicorn on 0.0.0.0:$PORT..."
    exec gunicorn pricediff.wsgi:application \
      --bind "0.0.0.0:$PORT" \
      --workers "${GUNICORN_WORKERS:-4}" \
      --threads "${GUNICORN_THREADS:-2}" \
      --worker-class sync \
      --timeout 120 \
      --access-logfile - \
      --error-logfile - \
      --log-level "${LOG_LEVEL:-info}"
    ;;

  worker)
    echo "[entrypoint] Starting Celery worker..."
    exec celery -A pricediff worker \
      --loglevel="${LOG_LEVEL:-info}" \
      --concurrency="${CELERY_CONCURRENCY:-4}" \
      --max-tasks-per-child="${CELERY_MAX_TASKS:-100}" \
      -E
    ;;

  beat)
    echo "[entrypoint] Starting Celery beat..."
    exec celery -A pricediff beat \
      --loglevel="${LOG_LEVEL:-info}" \
      --scheduler django_celery_beat.schedulers:DatabaseScheduler 2>/dev/null \
      || exec celery -A pricediff beat --loglevel="${LOG_LEVEL:-info}"
    ;;

  all)
    echo "[entrypoint] Starting web + worker via supervisord..."
    exec supervisord -c /app/.docker/supervisord.conf
    ;;

  shell)
    exec /bin/bash
    ;;

  *)
    echo "Usage: $0 [web|worker|beat|all|shell]" >&2
    exit 1
    ;;
esac
