#!/bin/sh
# Production container entrypoint for J-ONE Hotel & Lodge (API, Worker & Beat).
#
# Startup sequence (web role only):
#   1. Wait for the database to accept connections (bounded, no credentials logged).
#   2. Apply pending Django migrations via `migrate --noinput` (fail fast on error).
#   3. Collect static files when WhiteNoise serves them from the container.
#   4. Exec Gunicorn so it receives SIGTERM cleanly during rolling deployments.
#
# Safety rules:
#   * NEVER runs `makemigrations`, `flush`, `loaddata`, or seed commands on boot.
#   * NEVER starts serving requests when migrations fail (set -e + explicit check).
#   * Only the web role migrates. Worker/beat never migrate, so multiple
#     replicas cannot race competing migration processes; they wait for the
#     web service healthcheck (compose depends_on service_healthy) instead.
#   * Set RUN_MIGRATIONS=false to disable auto-migration (e.g. when migrations
#     are applied by a separate release job). Default is true for web.
set -eu

# Allow explicit command pass-through when operators run one-off jobs such as:
#   python manage.py migrate --noinput
#   python manage.py check --deploy
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

ROLE="${SERVICE_ROLE:-web}"

# Wait for the database to become available. Uses Django's own connection
# check so DATABASE_URL / DB_* settings are honoured without printing secrets.
wait_for_database() {
    MAX_ATTEMPTS="${DB_WAIT_ATTEMPTS:-30}"
    SLEEP_SECONDS="${DB_WAIT_SLEEP:-2}"
    attempt=1
    while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
        if python manage.py dbshell --command "SELECT 1" >/dev/null 2>&1 || \
           python -c "import django; django.setup(); from django.db import connection; connection.ensure_connection()" >/dev/null 2>&1; then
            echo "Database is available (attempt $attempt)."
            return 0
        fi
        echo "Database not ready yet (attempt $attempt/$MAX_ATTEMPTS); waiting ${SLEEP_SECONDS}s..."
        sleep "$SLEEP_SECONDS"
        attempt=$((attempt + 1))
    done
    echo "ERROR: Database did not become available after $MAX_ATTEMPTS attempts. Refusing to start." >&2
    return 1
}

run_migrations() {
    if [ "${RUN_MIGRATIONS:-true}" != "true" ]; then
        echo "RUN_MIGRATIONS=false: skipping automatic migrations."
        return 0
    fi
    wait_for_database
    echo "Applying pending Django migrations..."
    # --noinput never prompts; a failed migration exits non-zero and, with
    # set -e, prevents Gunicorn from starting (no unsafe serving).
    if ! python manage.py migrate --noinput; then
        echo "ERROR: Database migration failed. Refusing to start the application server." >&2
        exit 1
    fi
    echo "Migrations applied successfully."
    # Static files are pre-built at image build time; re-collecting here is
    # a no-op safety net for deployments that mount fresh code. It never
    # touches the database.
    python manage.py collectstatic --noinput >/dev/null 2>&1 || echo "collectstatic skipped (static already built)."
}

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
        run_migrations
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
