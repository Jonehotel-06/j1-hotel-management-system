# syntax=docker/dockerfile:1
# ===========================================================================
# J-ONE HOTEL & LODGE — Portable Production Backend Dockerfile
#
# Build from repository root:
#   docker build -t jone-hotel-backend:latest ./backend
# Or from backend/:
#   docker build -t jone-hotel-backend:latest .
#
# Two-stage build: compiles C extensions (mysqlclient) in an isolated builder
# stage so compilers and header files are excluded from the runtime image.
# ===========================================================================

# ---------------------------------------------------------------------------
# Stage 1: Dependency builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System build dependencies required solely to compile mysqlclient wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Minimal non-root production runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="J-ONE Hotel & Lodge Backend API" \
      org.opencontainers.image.description="Django REST API & Celery worker image for J-ONE Hotel & Lodge"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/var/tmp/jone \
    TMPDIR=/var/tmp/jone \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PORT=8000

# Runtime OS packages only:
#   - libmariadb3: shared C client library linked by mysqlclient
#   - ca-certificates: TLS root bundle for HTTPS APIs (Paystack, Brevo, B2/S3)
#   - tzdata: IANA timezone database required for Africa/Lagos business rules
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmariadb3 \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 appgroup \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Helper scripts for foreground Gunicorn execution and stdlib health probing.
COPY docker/docker-cmd.sh /usr/local/bin/docker-cmd.sh
COPY docker/container-healthcheck.py /usr/local/bin/container-healthcheck.py

# Application code owned cleanly; runtime directories belong to non-root user.
COPY --chown=appuser:appgroup . /app

RUN chmod 0755 /usr/local/bin/docker-cmd.sh /usr/local/bin/container-healthcheck.py /app/build.sh \
    && mkdir -p /app/staticfiles /app/media /var/tmp/jone \
    && chown appuser:appgroup /app \
    && chown -R appuser:appgroup /app/staticfiles /app/media /var/tmp/jone

USER appuser

# Pre-build compressed WhiteNoise static manifest at build time using transient,
# non-persisted placeholders (never written to image ENV layers) so workers do
# not need to run collectstatic on startup.
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    DJANGO_SECRET_KEY=build-only-ephemeral-collectstatic-placeholder \
    DJANGO_ALLOWED_HOSTS=build.invalid \
    DATABASE_URL=mysql://build:build@127.0.0.1:3306/build \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    FRONTEND_URL=https://build.invalid \
    CORS_ALLOWED_ORIGINS=https://build.invalid \
    CSRF_TRUSTED_ORIGINS=https://build.invalid \
    EMAIL_PROVIDER=console \
    python manage.py collectstatic --noinput

EXPOSE 8000

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/usr/local/bin/container-healthcheck.py"]

CMD ["/usr/local/bin/docker-cmd.sh"]
