# config/settings/development.py
"""Development settings: SQLite, console email, eager Celery, permissive CORS.

Defaults are developer-friendly: emails print to the terminal, background
tasks run in-process so no Redis/worker is required, and CORS allows the
locally hosted frontend. EMAIL_BACKEND remains overridable from .env so real
SMTP delivery can be tested locally — the Celery eager configuration is a
fixed development contract (see the comment in this module) so a missing Redis
server can never break local runs.
"""
import os

from django.core.exceptions import ImproperlyConfigured
from decouple import config

# ---------------------------------------------------------------------------
# Guard: this module must NEVER serve a hosted deployment.
#
# Development settings enable DEBUG, ALLOWED_HOSTS=["*"] and wildcard CORS.
# A committed/deployed ".env" carrying DJANGO_SETTINGS_MODULE=
# config.settings.development (the value shipped in .env.example) therefore
# silently turns a public API into a wildcard-CORS, debug-mode service. That is
# exactly how the deployed API came to answer CORS preflight with
# "Access-Control-Allow-Origin: *" (audit SEC-02).
#
# Rather than trusting the environment to be configured correctly, refuse to
# boot in a managed hosting environment. Local development, CI and the test
# suite are unaffected.
# ---------------------------------------------------------------------------
_HOSTING_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RENDER",
    "RENDER_SERVICE_ID",
    "FLY_APP_NAME",
    "DYNO",
    "K_SERVICE",
    "WEBSITE_SITE_NAME",
    "HEROKU_APP_NAME",
)
_detected = sorted(name for name in _HOSTING_MARKERS if os.environ.get(name))
if _detected and os.environ.get("JONE_ALLOW_DEVELOPMENT_SETTINGS") != "1":
    raise ImproperlyConfigured(
        "config.settings.development was selected on a hosted platform "
        f"({', '.join(_detected)}). Development settings enable DEBUG, "
        "ALLOWED_HOSTS='*' and wildcard CORS, which must never be exposed "
        "publicly. Set DJANGO_SETTINGS_MODULE=config.settings.production and "
        "restart. Override only for a deliberate, isolated staging run by "
        "setting JONE_ALLOW_DEVELOPMENT_SETTINGS=1."
    )

from .base import *  # noqa: E402,F401,F403

DEBUG = True
# Local development only: the dev server is reached through localhost, the
# loopback address, a LAN hostname or a forwarded sandbox host. Everything
# here is intentionally permissive and is guarded above.
ALLOWED_HOSTS = ["*"]

# The separately hosted frontend may run on any local port during development.
# This is intentionally dev-only; production uses an explicit origin allowlist.
CORS_ALLOW_ALL_ORIGINS = True

# ---------------------------------------------------------------------------#
# Celery — DEVELOPMENT CONTRACT (do not make these overridable from .env).
#
# Local development runs every task EAGERLY, IN-PROCESS:
#   * no Redis broker is ever contacted (broker = in-process memory transport);
#   * no Redis RESULT backend exists (eager results are in-memory EagerResult
#     objects), so no code path can start a "Connection to Redis lost …
#     retrying" loop when Redis is not running;
#   * transactional email is delivered SYNCHRONOUSLY by apps.core.emails and
#     never touches Celery/Redis at all (development default: console backend,
#     so nothing leaves the machine).
#
# To exercise the real Redis broker + Celery worker architecture locally, run
# with production-style settings (config.settings.production) or a dedicated
# local settings module — never by flipping these values via .env.
# ---------------------------------------------------------------------------#
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TASK_STORE_EAGER_RESULT = False
CELERY_RESULT_BACKEND = None          # eager tasks have no result store at all
CELERY_BROKER_URL = "memory://"       # even accidental dispatch stays local
CELERY_CACHE_BACKEND = "memory://"

# Emails print to the terminal by default (nothing leaves the machine).
# Setting EMAIL_BACKEND in .env to the SMTP backend — or setting
# EMAIL_PROVIDER=brevo with a BREVO_API_KEY — switches to REAL delivery.
# Delivery is synchronous either way; no Redis/worker is ever needed.
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
# Deterministic transport in development: the Django backend (console unless
# overridden), never a surprise Brevo call just because a key is present in
# the developer's environment. Set EMAIL_PROVIDER=brevo explicitly to test
# the real HTTPS API locally.
EMAIL_PROVIDER = config("EMAIL_PROVIDER", default="django").strip().lower()

# In-process cache is fine on a single dev machine.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "jone-cache",
    }
}

# SQLite concurrency for local development: the threaded dev server plus the
# background email-delivery threads write concurrently. WAL mode lets readers
# proceed while a writer commits, and a busy timeout makes writers WAIT for
# each other instead of failing instantly with "database is locked".
#
# transaction_mode="IMMEDIATE" is the critical part. Django's atomic() opens
# DEFERRED transactions by default: no lock is taken until the first write, so
# a request that READS (e.g. select_for_update on the booking) and then writes
# must UPGRADE its lock mid-transaction. If the background email thread
# commits a write in that window, SQLite detects the potential deadlock and
# fails the upgrade INSTANTLY with "database is locked" — the busy timeout is
# deliberately ignored (observed: create booking → initialize payment within
# milliseconds of each other). BEGIN IMMEDIATE takes the write lock upfront,
# so concurrent writers simply queue behind the busy timeout instead of ever
# hitting an upgrade failure.
# (Production uses MySQL, where none of these options apply.)
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):  # noqa: F405
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].update(
        {
            "transaction_mode": "IMMEDIATE",        # atomic() opens BEGIN IMMEDIATE
            "timeout": 30,                            # sqlite busy timeout (s)
            "init_command": "PRAGMA journal_mode=WAL",  # readers never block writers
        }
    )
