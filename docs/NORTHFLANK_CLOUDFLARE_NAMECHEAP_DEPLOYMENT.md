# J-ONE HOTEL & LODGE — Northflank + Cloudflare + Namecheap Production Deployment Guide

This guide documents the verified production deployment architecture and runbook for **J-ONE HOTEL & LODGE**:

- **Public Website, Guest Portal & Staff Dashboard (Static Hosting + PWA):** Cloudflare Workers Static Assets (`frontend/wrangler.jsonc`) or Cloudflare Pages serving `frontend/` at `https://jonehotels.com` (and optional `https://www.jonehotels.com`)
- **Backend API (`web`), Background Worker (`worker`), and Single Scheduler (`beat`):** Northflank Docker services built from `backend/Dockerfile` running at `https://api.jonehotels.com`
- **Database:** Northflank Managed **MySQL 8.0+** (`utf8mb4` / `utf8mb4_unicode_ci`, persistent volume, automated backups enabled)
- **Broker & Hot Cache:** Northflank Managed **Redis** (`rediss://` TLS normalized automatically)
- **Media Storage:** Backblaze B2 S3-compatible bucket (`BACKBLAZE_*`) or persistent volume mounted at `/app/media`
- **Payments:** Paystack (`NGN`, HMAC-SHA512 verified webhooks at `https://api.jonehotels.com/api/payments/webhook/`)
- **Transactional Email:** Synchronous HTTPS API (`EMAIL_PROVIDER=brevo` default) or SMTP (`EMAIL_PROVIDER=smtp`)
- **Timezone:** `Africa/Lagos` across Django (`TIME_ZONE`) and Celery (`CELERY_TIMEZONE`)

---

## 1. Non-Destructive Deployment Invariants

1. **Zero destructive startup behavior:** `backend/docker/docker-cmd.sh` starts `gunicorn`, `celery worker`, or `celery beat` **only**. It never executes `flush`, `sqlflush`, `reset_db`, seed scripts, or automatic schema migrations on container boot.
2. **Explicit controlled migrations:** Schema migrations run exclusively via a dedicated manual Northflank Job or one-off command (`python manage.py migrate --noinput`) **after** taking/verifying a database backup.
3. **Single Celery Beat instance:** Run **at most `1` replica** (`replicas = 1`) for the `beat` service (`SERVICE_ROLE=beat`) so periodic tasks (`expire_stale_pending_bookings`, `auto_checkout_overdue_stays`, `warn_stays_nearing_checkout`) are never double-scheduled.
4. **No secrets in frontend or git:** `frontend/js/config.js` and `frontend/js/runtime-config.js` contain public origin URLs only (`https://api.jonehotels.com`). All secret keys remain exclusively inside Northflank Secret Groups.

---

## 2. Northflank Services Configuration

### 2.1 Addons (Create First)
1. **Managed MySQL 8.0+ (`jone-mysql`)**
   - Engine: **Oracle MySQL 8.0+** (not MariaDB — `payments.Refund`, `FaceTemplate`, and `FingerprintTemplate` define conditional partial `UniqueConstraint` indexes natively supported by MySQL 8.0.13+).
   - Character set / Collation: `utf8mb4` / `utf8mb4_unicode_ci`.
   - Storage & Backups: Persistent storage enabled; automated daily backups + pre-release manual snapshot enabled.
2. **Managed Redis (`jone-redis`)**
   - TLS enabled (`rediss://...` connection URI is normalized automatically to include `ssl_cert_reqs=required` by `backend/config/settings/base.py`).

### 2.2 Secret Group (`jone-backend-env`)
Attach one shared Northflank **Secret Group** to all three backend workloads (`jone-api`, `jone-worker`, `jone-beat`), inheriting `DATABASE_URL` from `jone-mysql` and `REDIS_URL` from `jone-redis`:

```ini
DJANGO_SETTINGS_MODULE=config.settings.production
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<generate-unique-64-char-secret>
DJANGO_ALLOWED_HOSTS=api.jonehotels.com
SECURE_SSL_REDIRECT=True

DATABASE_URL=<linked-from-northflank-mysql-addon>
DB_SSL_MODE=REQUIRED
REDIS_URL=<linked-from-northflank-redis-addon>
CELERY_TASK_ALWAYS_EAGER=False

FRONTEND_URL=https://jonehotels.com
PORTAL_FRONTEND_URL=https://jonehotels.com
SERVICE_QR_FRONTEND_URL=https://jonehotels.com
PAYMENT_CALLBACK_URL=https://jonehotels.com/payment-verify.html
CORS_ALLOWED_ORIGINS=https://jonehotels.com,https://www.jonehotels.com
CSRF_TRUSTED_ORIGINS=https://jonehotels.com,https://www.jonehotels.com,https://api.jonehotels.com

PAYSTACK_SECRET_KEY=<sk_live_or_sk_test_from_paystack>
PAYSTACK_PUBLIC_KEY=<pk_live_or_pk_test_from_paystack>

EMAIL_PROVIDER=brevo
EMAIL_API_KEY=<provider-api-key>
DEFAULT_FROM_EMAIL=J-ONE Hotel & Lodge <noreply@jonehotels.com>
EMAIL_LOGO_URL=https://jonehotels.com/assets/images/logo-light.png
HOTEL_NOTIFICATION_EMAILS=frontdesk@jonehotels.com

BACKBLAZE_KEY_ID=<b2-key-id>
BACKBLAZE_APPLICATION_KEY=<b2-application-key>
BACKBLAZE_BUCKET_NAME=<b2-bucket-name>
BACKBLAZE_ENDPOINT=https://s3.us-west-004.backblazeb2.com
MEDIA_CUSTOM_DOMAIN=<optional-cdn-host>
```

### 2.3 Workload Definitions (Shared Build: Context `/backend`, Dockerfile `/backend/Dockerfile`)

| Workload | Northflank Type | Replicas | Override Env | Port / Health Check |
|---|---|---|---|---|
| `jone-migrate` | Manual **Job** | Manual run | — (Command override: `python manage.py migrate --noinput && python manage.py check_database_charset`) | Exits `0` |
| `jone-api` | **Combined / Deployment Service** | `1–2+` | `SERVICE_ROLE=web`, `PORT=8000`, `GUNICORN_WORKERS=2`, `GUNICORN_THREADS=2` | HTTP port `8000` public -> Liveness `GET /api/health/live/`, Readiness `GET /api/health/ready/` |
| `jone-worker` | **Deployment Service** (no public port) | `1+` | `SERVICE_ROLE=worker`, `CELERY_CONCURRENCY=2` | Exec probe: `python /app/docker/container-healthcheck.py` |
| `jone-beat` | **Deployment Service** (no public port) | **`1` ONLY** | `SERVICE_ROLE=beat`, `CELERY_BEAT_SCHEDULE_FILENAME=/tmp/celerybeat-schedule` | Exec probe: `python /app/docker/container-healthcheck.py` |

### 2.4 Custom Domain on Northflank
- Add domain `api.jonehotels.com` under **Project / Account Domains**, copy the exact TXT ownership verification record + CNAME target shown by Northflank into Namecheap/Cloudflare DNS, wait for automatic TLS certificate issuance, and link `api.jonehotels.com` to `jone-api` port `8000`.

---

## 3. Cloudflare Frontend Configuration

### Option A (Repository Native — `frontend/wrangler.jsonc` Workers Static Assets)
- **Root directory:** `frontend`
- **Build command:** `python3 build.py && python3 validate.py`
- **Deploy command:** `npx wrangler deploy`
- **Assets directory:** `.` (`not_found_handling: "404-page"`, `html_handling: "auto-trailing-slash"`, zero Worker script execution cost because there is no `main` script — 100% free static edge asset serving)
- ** Security & Cache Headers:** Automatically applied from `frontend/_headers` and internal files excluded via `frontend/.assetsignore`.
- **Custom Domains:** Attach `jonehotels.com` and `www.jonehotels.com` under **Workers & Pages -> Custom Domains** (or forward `www.jonehotels.com` -> `https://jonehotels.com/$1` with `301` redirect rule).

### Option B (Cloudflare Pages Direct Git Integration)
- **Root directory:** `frontend`
- **Build command:** `python3 build.py && python3 validate.py`
- **Build output directory:** `.`

---

## 4. Post-Deploy Verification Commands

Run inside `jone-api` shell or a one-off Northflank Job:

```bash
python manage.py check --deploy --fail-level ERROR
python manage.py check_database_charset
python manage.py check_media_storage
python manage.py check_payment_config --verify
python manage.py email_pipeline_check --publish
python manage.py email_check
```

From any workstation with `curl`:

```bash
curl -fsS https://api.jonehotels.com/api/health/live/
curl -fsS https://api.jonehotels.com/api/health/ready/
curl -fsSI -H "Origin: https://jonehotels.com" https://api.jonehotels.com/api/rooms/
curl -fsS https://jonehotels.com/version.json
```
