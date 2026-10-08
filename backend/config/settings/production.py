# config/settings/production.py
"""Production settings: MySQL, strict security, B2 media, Redis cache.

Fails fast on missing critical configuration instead of silently falling back
to insecure defaults.
"""
import dj_database_url
from decouple import Csv, config

from .base import *  # noqa: F401,F403

DEBUG = False
SECRET_KEY = config("DJANGO_SECRET_KEY")  # required — no insecure default
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", cast=Csv())

# --- Database: MySQL 8+ via DATABASE_URL or components ----------------------
_database_url = config("DATABASE_URL", default="")
if _database_url:
    DATABASES = {
        "default": dj_database_url.parse(
            _database_url,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": config("DB_NAME"),
            "USER": config("DB_USER"),
            "PASSWORD": config("DB_PASSWORD"),
            "HOST": config("DB_HOST", default="127.0.0.1"),
            "PORT": config("DB_PORT", default="3306"),
            "CONN_MAX_AGE": 600,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
# Guard: refuse to boot production on SQLite.
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    raise RuntimeError("Production requires MySQL (set DATABASE_URL or DB_* variables).")

# --- CORS / CSRF: explicit allowlists only ----------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", cast=Csv())
CORS_ALLOW_ALL_ORIGINS = False

# --- HTTPS / browser security headers ---------------------------------------
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

# --- Static files via WhiteNoise --------------------------------------------
# Preserve the base-selected media backend (B2 when configured, local
# otherwise); only production static files are replaced by WhiteNoise.
STORAGES["staticfiles"] = {
    "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
}

# A deployed Paystack callback must be public HTTPS, never a development host.
from django.core.exceptions import ImproperlyConfigured
from urllib.parse import urlparse
_callback = urlparse(PAYMENT_CALLBACK_URL)
if _callback.scheme != "https" or _callback.hostname in {"localhost", "127.0.0.1", None}:
    raise ImproperlyConfigured(
        "PAYMENT_CALLBACK_URL (or FRONTEND_URL fallback) must be a public HTTPS URL in production."
    )
_portal_frontend = urlparse(PORTAL_FRONTEND_URL)
if _portal_frontend.scheme != "https" or _portal_frontend.hostname in {"localhost", "127.0.0.1", None}:
    raise ImproperlyConfigured(
        "PORTAL_FRONTEND_URL (or FRONTEND_URL fallback) must be a public HTTPS URL in production."
    )


# --- Redis-backed cache (throttles, settings cache, hot public content) ------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,  # noqa: F405
    }
}

# --- Transactional email ------------------------------------------------------
# Delivery is synchronous — no Celery, no Redis, no worker in the email path.
#
# The provider is pinned explicitly (default: brevo) so the mere presence of
# Gmail/SMTP EMAIL_* variables can never silently reroute production email
# through an SMTP port. HTTPS API delivery is portable across hosts with
# restricted SMTP egress; SMTP remains available on hosts that permit it.
EMAIL_PROVIDER = config("EMAIL_PROVIDER", default="brevo").strip().lower()

from apps.notifications.providers import API_PROVIDERS as _API_PROVIDERS  # noqa: E402

if EMAIL_PROVIDER in _API_PROVIDERS and not (
    (EMAIL_API_KEY or "").strip() or (BREVO_API_KEY or "").strip()  # noqa: F405
):
    raise ImproperlyConfigured(
        f"EMAIL_API_KEY is required in production: EMAIL_PROVIDER is "
        f"'{EMAIL_PROVIDER}', which delivers over an HTTPS API. Create the key "
        "in that provider's dashboard and set EMAIL_API_KEY. "
        "DEFAULT_FROM_EMAIL must use a sender address verified with the provider."
    )
if EMAIL_PROVIDER == "mailgun" and not (EMAIL_API_DOMAIN or "").strip():  # noqa: F405
    raise ImproperlyConfigured(
        "EMAIL_API_DOMAIN is required when EMAIL_PROVIDER is 'mailgun' — set it "
        "to the sending domain configured in Mailgun."
    )

# DEFAULT_FROM_EMAIL must parse to a real address — providers reject the send
# otherwise. Fail at boot, not at the first guest receipt.
from email.utils import parseaddr as _parseaddr  # noqa: E402
_sender_name, _sender_email = _parseaddr(DEFAULT_FROM_EMAIL)  # noqa: F405
if not _sender_email or "@" not in _sender_email:
    raise ImproperlyConfigured(
        "DEFAULT_FROM_EMAIL is not a valid sender address. Use the form "
        "'J-one hotel & lodge <sender@example.com>' with an address verified "
        "as a sender with the configured email provider."
    )

# Celery still powers the NON-EMAIL scheduled hotel tasks (pending-booking
# expiry, automatic checkout, checkout warnings) via the beat + worker
# services. Email no longer touches the queue, so a missing broker can no
# longer lose mail — but the scheduled tasks do still need Redis when a real
# worker architecture is used (CELERY_TASK_ALWAYS_EAGER=False).
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=False, cast=bool)
if not CELERY_TASK_ALWAYS_EAGER and not (REDIS_URL or "").strip():  # noqa: F405
    raise ImproperlyConfigured(
        "REDIS_URL (Celery broker) is required when CELERY_TASK_ALWAYS_EAGER is "
        "False so the worker can run the scheduled booking tasks (expiry, "
        "auto-checkout, checkout warnings). Email does NOT use this queue."
    )

# A Paystack PUBLIC key must never be used as the server-side secret: secret
# keys start with sk_ (live) / sk_test_ (test); public keys start with pk_.
if PAYSTACK_SECRET_KEY.strip().startswith(("pk_", "pk_test_")):  # noqa: F405
    raise ImproperlyConfigured(
        "PAYSTACK_SECRET_KEY looks like a Paystack PUBLIC key (pk_…). Public keys "
        "belong in frontend configuration only — set the server-side secret key "
        "(sk_… / sk_test_…) instead."
    )
