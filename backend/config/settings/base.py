# config/settings/base.py
"""
Base settings for the J-ONE HOTEL & LODGE backend.

Everything environment-specific (secrets, hosts, databases, vendors) comes
from environment variables via python-decouple. See .env.example.
"""
import logging
import os
import re
from datetime import timedelta
from pathlib import Path

from botocore.config import Config as BotocoreConfig
from corsheaders.defaults import default_headers
from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = config("DJANGO_SECRET_KEY", default="django-insecure-dev-only-change-me")
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

DJANGO_APPS = [
    # Restricted admin site (management roles only); see apps/accounts/admin_site.py.
    "apps.accounts.apps.JOneAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.portal",
    "apps.hotel",
    "apps.rooms",
    "apps.offers",
    "apps.gallery",
    "apps.bookings",
    "apps.stays",
    "apps.payments",
    "apps.finance",
    "apps.pos",
    "apps.guest_services",
    "apps.housekeeping",
    "apps.maintenance",
    "apps.inventory",
    "apps.staff_operations",
    "apps.enquiries",
    "apps.notifications",
    "apps.reports",
    "apps.audit",
    "apps.reviews",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "apps.core.request_middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

# The backend is a pure JSON API; Django templates are only used by the
# Django admin fallback UI and drf-spectacular's Swagger UI page.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database (default SQLite; overridden per-environment)
# ---------------------------------------------------------------------------
_DEFAULT_DB = {
    "ENGINE": config("DB_ENGINE", default="django.db.backends.sqlite3"),
    "NAME": config("DB_NAME", default=BASE_DIR / "db.sqlite3"),
    "USER": config("DB_USER", default=""),
    "PASSWORD": config("DB_PASSWORD", default=""),
    "HOST": config("DB_HOST", default=""),
    "PORT": config("DB_PORT", default=""),
    "CONN_MAX_AGE": 60,
    "CONN_HEALTH_CHECKS": True,
}

# STRICT_TRANS_TABLES is MySQL-only; SQLite rejects the SET statement.
if "mysql" in _DEFAULT_DB["ENGINE"]:
    _DEFAULT_DB["OPTIONS"] = {"init_command": "SET sql_mode='STRICT_TRANS_TABLES'"}

DATABASES = {"default": _DEFAULT_DB}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=config("JWT_ACCESS_TOKEN_MINUTES", default=30, cast=int)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=config("JWT_REFRESH_TOKEN_DAYS", default=7, cast=int)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "SIGNING_KEY": config("JWT_SIGNING_KEY", default=None) or SECRET_KEY,
    "ALGORITHM": "HS256",
    "AUTH_HEADER_TYPES": ("Bearer",),
    "TOKEN_OBTAIN_SERIALIZER": "apps.accounts.serializers.JOneTokenObtainPairSerializer",
}

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ("apps.core.renderers.JOneJSONRenderer",),
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.accounts.authentication.WorkstationJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.AllowAny",),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "EXCEPTION_HANDLER": "apps.core.exception_handler.jone_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "COERCE_DECIMAL_TO_STRING": True,
    "DEFAULT_THROTTLE_RATES": {
        # Sensitive-endpoint throttle scopes. Views opt in via throttle_scope.
        "login": "5/min",
        "register": "20/hour",
        "password_reset": "10/hour",
        "enquiry": "10/hour",
        "booking_create": "30/hour",
        "availability": "240/hour",
        "payment_init": "20/hour",
        "payment_verify": "60/hour",
        "paystack_webhook": "300/min",
        "review_verify": "20/hour",
        "review_submit": "5/hour",
        # Guest portal magic links are deliberately tighter than ordinary
        # public content: endpoint response stays non-enumerating while the
        # per-email service limit adds a second protection layer.
        "portal_access_request": "10/hour",
        "portal_access_consume": "20/hour",
        # Public bearer links are scoped to one room/table, but still need
        # conservative per-client request limits against QR-form abuse.
        "service_qr_context": "60/min",
        "service_qr_submit": "8/min",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "J-ONE HOTEL & LODGE API",
    "DESCRIPTION": (
        "REST API powering the J-ONE HOTEL & LODGE public website and staff "
        "hotel management dashboard. The frontend is an independently hosted "
        "HTML/CSS/Vanilla-JS application communicating with this API over JSON."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/",
}

# ---------------------------------------------------------------------------
# Internationalisation / time
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
# The hotel operates in Nigeria; hotel-local "today" drives booking rules.
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True
# Beat schedules use the hotel's business-day boundary, not a worker host's
# timezone or Celery's default UTC boundary.
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

# ---------------------------------------------------------------------------
# Static & media files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]


# ============================================
# BACKBLAZE B2 / S3 COMPATIBLE STORAGE
# ============================================

B2_S3_BACKEND = "storages.backends.s3boto3.S3Boto3Storage"
DEFAULT_B2_ENDPOINT = "https://s3.us-east-005.backblazeb2.com"
DEFAULT_B2_REGION = "us-east-005"


def b2_region_from_endpoint(endpoint):
    """``https://s3.us-west-004.backblazeb2.com`` -> ``us-west-004``.

    B2 signs every request against the region in the endpoint. Falling back to
    botocore's default (us-east-1) makes B2 answer ``403 SignatureDoesNotMatch``
    even when the keys are correct.
    """
    match = re.search(r"s3\.([^./]+)\.backblazeb2\.com", endpoint or "")
    return match.group(1) if match else ""


def configure_b2_media_storage(storages_map):
    """Point ``storages_map["default"]`` at Backblaze B2 when it is configured.

    Returns ``{"enabled": bool, "settings": {...}}`` — ``settings`` holds the
    ``AWS_*`` values django-storages reads, including the ``botocore`` client
    config that keeps B2 happy:

      * checksums are only calculated ``when_required`` — botocore >= 1.36
        otherwise sends ``x-amz-sdk-checksum-algorithm`` on every PutObject and
        B2 answers ``400 InvalidArgument``, which is exactly the failure that
        made uploads silently stop reaching the bucket;
      * ``s3v4`` signatures with virtual-host addressing, as B2 requires.

    When the credentials are absent the map is left untouched so local/dev
    environments keep whatever backend they were given.
    """
    key_id = os.environ.get("BACKBLAZE_KEY_ID", "").strip()
    app_key = os.environ.get("BACKBLAZE_APPLICATION_KEY", "").strip()
    bucket = os.environ.get("BACKBLAZE_BUCKET_NAME", "").strip()
    if not (key_id and app_key and bucket):
        return {"enabled": False, "settings": {}}

    endpoint = (os.environ.get("BACKBLAZE_ENDPOINT", "") or "").strip() or DEFAULT_B2_ENDPOINT
    # boto3 builds a malformed URL when the scheme is missing, and an http://
    # endpoint would send the application key in the clear — always https.
    if not re.match(r"^https?://", endpoint):
        endpoint = "https://" + endpoint
    endpoint = re.sub(r"^http://", "https://", endpoint)
    region = ((os.environ.get("BACKBLAZE_REGION", "") or "").strip()
              or b2_region_from_endpoint(endpoint) or DEFAULT_B2_REGION)

    settings_out = {
        "AWS_ACCESS_KEY_ID": key_id,
        "AWS_SECRET_ACCESS_KEY": app_key,
        "AWS_STORAGE_BUCKET_NAME": bucket,
        "AWS_S3_REGION_NAME": region,
        "AWS_S3_ENDPOINT_URL": endpoint,
        "AWS_S3_ADDRESSING_STYLE": "virtual",
        "AWS_S3_SIGNATURE_VERSION": "s3v4",
        "AWS_QUERYSTRING_AUTH": False,          # public media bucket — never sign URLs
        "AWS_DEFAULT_ACL": "public-read",
        "AWS_S3_FILE_OVERWRITE": True,
        "AWS_S3_CLIENT_CONFIG": BotocoreConfig(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    }
    storages_map.setdefault("default", {})["BACKEND"] = B2_S3_BACKEND
    return {"enabled": True, "settings": settings_out}


# Use S3Boto3Storage directly — same as your Gadgets Store
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

_B2_MEDIA = configure_b2_media_storage(STORAGES)
_B2_SETTINGS = _B2_MEDIA["settings"]

# B2 credentials (from environment variables)
AWS_ACCESS_KEY_ID = config("BACKBLAZE_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = config("BACKBLAZE_APPLICATION_KEY", default="")
AWS_STORAGE_BUCKET_NAME = config("BACKBLAZE_BUCKET_NAME", default="")
AWS_S3_REGION_NAME = _B2_SETTINGS.get("AWS_S3_REGION_NAME") or config(
    "BACKBLAZE_REGION", default=DEFAULT_B2_REGION
)
AWS_S3_ENDPOINT_URL = _B2_SETTINGS.get("AWS_S3_ENDPOINT_URL") or config(
    "BACKBLAZE_ENDPOINT", default=DEFAULT_B2_ENDPOINT
)

# CRITICAL B2 Settings
AWS_S3_ADDRESSING_STYLE = "virtual"
AWS_S3_SIGNATURE_VERSION = "s3v4"
AWS_QUERYSTRING_AUTH = False
AWS_DEFAULT_ACL = "public-read"
AWS_S3_FILE_OVERWRITE = True
# The botocore client config computed above (falls back to the same safe
# defaults when B2 is not configured in this environment).
AWS_S3_CLIENT_CONFIG = _B2_SETTINGS.get("AWS_S3_CLIENT_CONFIG") or BotocoreConfig(
    signature_version="s3v4",
    s3={"addressing_style": "virtual"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)

# Media URL
MEDIA_URL = (
    f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.backblazeb2.com/"
    if _B2_MEDIA["enabled"] else "/media/"
)
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# CORS (development allows everything; production must list exact origins)
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())
CORS_ALLOW_CREDENTIALS = False  # JWT travels in Authorization headers, not cookies
CORS_EXPOSE_HEADERS = ["X-Request-ID"]
# Custom request headers the independently hosted frontend sends. Keeping this
# list explicit (instead of CORS_ALLOW_ALL_HEADERS) matters: a browser rejects
# the ENTIRE request when a sent header is not allowed by the preflight —
# losing any of these silently breaks the matching feature cross-origin:
#   x-guest-access-token          legacy booking-scoped self-service access
#   x-cancellation-access-token   cancellation status lookup (Contact flow)
#   x-portal-session              short-lived verified-email portal session
#   idempotency-key               safe booking-creation retries
CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-guest-access-token",
    "x-cancellation-access-token",
    "x-portal-session",
    "x-service-qr-token",
    "idempotency-key",
    "x-jone-terminal",
    "x-jone-desktop-key",
    "x-request-id",
)

# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:8080").rstrip("/")
# An independently hosted guest portal may override this public origin. It
# falls back to FRONTEND_URL for same-origin/public-site deployments.
PORTAL_FRONTEND_URL = (config("PORTAL_FRONTEND_URL", default="") or FRONTEND_URL).rstrip("/")
# Printed service QR links are public frontend URLs; never point them at the API
# origin unless the frontend is actually served there.
SERVICE_QR_FRONTEND_URL = (config("SERVICE_QR_FRONTEND_URL", default="") or FRONTEND_URL).rstrip("/")
# Validity of a printed service QR token. Expired or revoked tokens fail closed
# with a distinct, guest-safe response; staff reissue with rotate.
SERVICE_QR_REQUIRE_HTTPS = config("SERVICE_QR_REQUIRE_HTTPS", default=False, cast=bool)
SERVICE_QR_LINK_TTL_HOURS = min(max(config("SERVICE_QR_LINK_TTL_HOURS", default=720, cast=int), 1), 8760)
PAYMENT_CALLBACK_URL = (
    config("PAYMENT_CALLBACK_URL", default="") or f"{FRONTEND_URL}/payment-verify.html"
)
# Portal access begins with a short-lived one-use email link and an opaque
# bearer session held only in browser memory. Cookie refresh is intentionally
# deferred until a deployment has an explicit compatible cookie/CSRF policy.
PORTAL_CHALLENGE_MINUTES = config("PORTAL_CHALLENGE_MINUTES", default=15, cast=int)
PORTAL_SESSION_HOURS = config("PORTAL_SESSION_HOURS", default=8, cast=int)
# Minimum gap between staff resends of the automatic portal invitation.
# Service categories a guest may request in the portal (comma-separated values from
# ServiceRequest.Category). Empty means all categories.
PORTAL_ENABLED_SERVICE_CATEGORIES = [v.strip().upper() for v in config("PORTAL_ENABLED_SERVICE_CATEGORIES", default="").split(",") if v.strip()]
PORTAL_INVITATION_RESEND_COOLDOWN_MINUTES = config("PORTAL_INVITATION_RESEND_COOLDOWN_MINUTES", default=5, cast=int)
# Facial verification for staff attendance. Enforcement is opt-in until the desk
# capture UI is deployed; when a probe is sent it is always verified.
STAFF_FACE_VERIFICATION_REQUIRED = config("STAFF_FACE_VERIFICATION_REQUIRED", default=False, cast=bool)
FACE_MATCH_MAX_DISTANCE = config("FACE_MATCH_MAX_DISTANCE", default=0.5, cast=float)
FACE_TEMPLATE_RETENTION_DAYS = config("FACE_TEMPLATE_RETENTION_DAYS", default=365, cast=int)
FACE_ATTEMPT_RETENTION_DAYS = config("FACE_ATTEMPT_RETENTION_DAYS", default=365, cast=int)
PORTAL_SESSION_TOUCH_MINUTES = config("PORTAL_SESSION_TOUCH_MINUTES", default=15, cast=int)
PORTAL_CHALLENGES_PER_EMAIL_PER_HOUR = config("PORTAL_CHALLENGES_PER_EMAIL_PER_HOUR", default=5, cast=int)

PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_PUBLIC_KEY = config("PAYSTACK_PUBLIC_KEY", default="")
# Webhook signature validation uses the secret key per Paystack documentation.

MAX_UPLOAD_MB = config("MAX_UPLOAD_MB", default=5, cast=int)

# --- Email -----------------------------------------------------------------
EMAIL_HOST = config("EMAIL_HOST", default="")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
# TLS (STARTTLS, port 587) and SSL (implicit TLS, port 465) are mutually
# exclusive — enabling both makes Django raise at send time. Default to TLS and
# force SSL off whenever TLS is on so a misconfigured env can never enable both.
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    # Contradictory: prefer the port's convention, else fall back to TLS.
    if EMAIL_PORT == 465:
        EMAIL_USE_TLS = False
    else:
        EMAIL_USE_SSL = False
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
# Connection timeout so a stuck provider request fails fast. Email delivery is
# SYNCHRONOUS (it runs inside the originating HTTP request), so this MUST stay
# comfortably below the gunicorn request timeout (60s in production): a 60s
# email timeout would let one slow provider call get the whole web worker
# killed mid-request, losing the API response and stranding the EmailLog in
# SENDING. 20s is ample for any provider's HTTPS API.
EMAIL_TIMEOUT = config("EMAIL_TIMEOUT", default=20, cast=int)
# --- Transactional email provider ------------------------------------------

# smtp/django/console | brevo | sendgrid | mailgun | postmark | resend.
# Empty selects an API provider when a key is present, else Django's backend.
EMAIL_PROVIDER = config("EMAIL_PROVIDER", default="").strip().lower()
EMAIL_API_KEY = config("EMAIL_API_KEY", default="")
# Legacy name, still honoured so existing deployments keep working.
BREVO_API_KEY = config("BREVO_API_KEY", default="")
# Mailgun needs the sending domain; regions override the base URL.
EMAIL_API_DOMAIN = config("EMAIL_API_DOMAIN", default="")
EMAIL_API_BASE_URL = config("EMAIL_API_BASE_URL", default="")
EMAIL_API_STREAM = config("EMAIL_API_STREAM", default="outbound")
# Absolute HTTPS logo for providers that cannot deliver inline cid: images.
EMAIL_LOGO_URL = config("EMAIL_LOGO_URL", default="")
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="J-one hotel & lodge <agbo33010@gmail.com>")

# address and every provider rejects the sender.
if (len(DEFAULT_FROM_EMAIL) >= 2 and DEFAULT_FROM_EMAIL[0] == DEFAULT_FROM_EMAIL[-1]
        and DEFAULT_FROM_EMAIL[0] in ("'", '"')):
    DEFAULT_FROM_EMAIL = DEFAULT_FROM_EMAIL[1:-1].strip()
SERVER_EMAIL = DEFAULT_FROM_EMAIL
# Comma-separated operational inbox(es) for cancellation/enquiry alerts.
HOTEL_NOTIFICATION_EMAILS = config("HOTEL_NOTIFICATION_EMAILS", default="", cast=Csv())
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)

# --- Celery / cache --------------------------------------------------------

REDIS_URL = config("REDIS_URL", default="redis://127.0.0.1:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = config("CELERY_TASK_ALWAYS_EAGER", default=False, cast=bool)

CELERY_TASK_EAGER_PROPAGATES = config("CELERY_TASK_EAGER_PROPAGATES", default=False, cast=bool)

CELERY_TASK_STORE_EAGER_RESULT = False
# A task is only acknowledged after it returns — a worker crash mid-delivery
# re-queues the job rather than losing it.
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_TIME_LIMIT = 120
CELERY_TASK_SOFT_TIME_LIMIT = 90

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "jone-cache",
    }
}

# ---------------------------------------------------------------------------
# Logging — structured, leveled, and never contains credentials
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "paystack": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}