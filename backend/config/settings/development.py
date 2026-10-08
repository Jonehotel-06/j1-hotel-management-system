# config/settings/development.py
"""Development settings: SQLite, console email, eager Celery, permissive CORS.

Defaults are developer-friendly: emails print to the terminal, background
tasks run in-process so no Redis/worker is required, and CORS allows the
locally hosted frontend. EMAIL_BACKEND remains overridable from .env so real
SMTP delivery can be tested locally — the Celery eager configuration is a
fixed development contract (see the comment in this module) so a missing Redis
server can never break local runs.
"""
from decouple import config

from .base import *  # noqa: F401,F403

DEBUG = True
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
