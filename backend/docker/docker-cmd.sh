#!/bin/sh
# Production container default command for J-ONE Hotel & Lodge API.
#
# Intentional safety rules:
#   1. Does NOT auto-run `migrate` or `collectstatic` on worker startup.
#   2. Uses `exec` so Gunicorn becomes PID 1 and receives SIGTERM directly for
#      graceful in-flight request completion on container stop/redeploy.
#   3. Honours platform-injected $PORT (Railway, Render, Fly.io, Cloud Run, VPS)
#      without hardcoding any provider hostname.
set -eu

PORT="${PORT:-8000}"
WORKERS="${GUNICORN_WORKERS:-${WEB_CONCURRENCY:-2}}"
THREADS="${GUNICORN_THREADS:-2}"
WORKER_CLASS="${GUNICORN_WORKER_CLASS:-gthread}"
TIMEOUT="${GUNICORN_TIMEOUT:-60}"
GRACEFUL_TIMEOUT="${GUNICORN_GRACEFUL_TIMEOUT:-30}"
KEEPALIVE="${GUNICORN_KEEPALIVE:-5}"
MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-1000}"
MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-100}"
LOG_LEVEL="${GUNICORN_LOG_LEVEL:-info}"

WORKER_TMP_ARGS=""
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    WORKER_TMP_ARGS="--worker-tmp-dir /dev/shm"
fi

# shellcheck disable=SC2086
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT}" \
    --worker-class "${WORKER_CLASS}" \
    --workers "${WORKERS}" \
    --threads "${THREADS}" \
    ${WORKER_TMP_ARGS} \
    --timeout "${TIMEOUT}" \
    --graceful-timeout "${GRACEFUL_TIMEOUT}" \
    --keep-alive "${KEEPALIVE}" \
    --max-requests "${MAX_REQUESTS}" \
    --max-requests-jitter "${MAX_REQUESTS_JITTER}" \
    --log-level "${LOG_LEVEL}" \
    --access-logfile - \
    --error-logfile -
