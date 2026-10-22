#!/bin/sh
# Production container entrypoint for J-ONE Hotel & Lodge (API, Worker & Beat).
#
# Intentional safety rules:
#   1. NEVER auto-runs `migrate`, `flush`, `loaddata`, or seed commands on boot.
#   2. Uses `exec` so Gunicorn or Celery becomes the foreground signal target and
#      receives SIGTERM cleanly during rolling deployments on Northflank / Docker.
#   3. Supports running API (`SERVICE_ROLE=web`, default), Celery worker
#      (`SERVICE_ROLE=worker`), or single-instance Celery Beat scheduler
#      (`SERVICE_ROLE=beat`) from one immutable image.
set -eu

# Allow explicit command pass-through when operators run one-off jobs such as:
#   python manage.py migrate --noinput
#   python manage.py check --deploy
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

ROLE="${SERVICE_ROLE:-web}"

case "${ROLE}" in
    worker)
        CONCURRENCY="${CELERY_CONCURRENCY:-2}"
        LOG_LEVEL="${CELERY_LOG_LEVEL:-info}"
        exec celery -A config worker \
            --loglevel="${LOG_LEVEL}" \
            --concurrency="${CONCURRENCY}" \
            --without-gossip \
            --without-mingle
        ;;
    beat|scheduler)
        LOG_LEVEL="${CELERY_LOG_LEVEL:-info}"
        SCHEDULE_FILE="${CELERY_BEAT_SCHEDULE_FILENAME:-/tmp/celerybeat-schedule}"
        exec celery -A config beat \
            --loglevel="${LOG_LEVEL}" \
            --schedule="${SCHEDULE_FILE}"
        ;;
    web|api)
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
        ;;
    *)
        echo "Unsupported SERVICE_ROLE='${ROLE}'. Expected one of: web, worker, beat." >&2
        exit 2
        ;;
esac
