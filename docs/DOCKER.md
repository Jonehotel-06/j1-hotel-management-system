# Docker & Portable Container Operations Guide

Production container packaging, local full-stack development, external MySQL connectivity, safe migration workflows, media storage, reverse-proxy networking, backup/restore, and rollback runbooks for **J-ONE Hotel & Lodge**.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Local Development & Integration Setup (Docker Compose)](#2-local-development--integration-setup-docker-compose)
3. [Building & Running the Standalone Production Image](#3-building--running-the-standalone-production-image)
4. [Connecting to an External Managed MySQL Server](#4-connecting-to-an-external-managed-mysql-server)
5. [Safe Database Migration Workflow (Zero Automatic Destructive Ops)](#5-safe-database-migration-workflow-zero-automatic-destructive-ops)
6. [Static Assets (WhiteNoise) & Media Storage (Backblaze B2 vs Persistent Volume)](#6-static-assets-whitenoise--media-storage-backblaze-b2-vs-persistent-volume)
7. [HTTPS, CORS, CSRF, Reverse Proxies & Receptionist Workstation IP Policy](#7-https-cors-csrf-reverse-proxies--receptionist-workstation-ip-policy)
8. [Health Checks, Liveness vs Readiness & Graceful Shutdown](#8-health-checks-liveness-vs-readiness--graceful-shutdown)
9. [Deploying Across Cloud Providers & VPS](#9-deploying-across-cloud-providers--vps)
10. [Database Backup & Recovery Runbook](#10-database-backup--recovery-runbook)
11. [Rollback Plan](#11-rollback-plan)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Architecture Overview

The backend packages a single immutable, non-root (`appuser`, UID/GID `10001:10001`), two-stage `python:3.12-slim-bookworm` image (`backend/Dockerfile`) that powers three distinct runtime roles from identical code and dependencies:

| Process Role | Command | Scale Rule | Responsibility |
|---|---|---|---|
| **Web API (`api`)** | `/usr/local/bin/docker-cmd.sh` (`exec gunicorn config.wsgi:application ...`) | Horizontal (`1..N`) | Serves DRF JSON API (`/api/*`), Django Admin (`/django-admin/`), pre-built compressed WhiteNoise static files (`/static/*`), Paystack webhooks, and synchronous transactional emails. |
| **Celery Worker (`worker`)** | `celery -A config worker -l info --concurrency 2` | Horizontal (`1..N`) | Executes scheduled operational jobs (stale pending booking expiry, overnight no-show resolution, 12:00 WAT auto-checkout, accounting reconciliation checks). |
| **Celery Beat (`beat`)** | `celery -A config beat -l info --schedule /var/tmp/jone/celerybeat-schedule` | **Singleton (`1` ONLY)** | Publishes periodic schedules (`Africa/Lagos` business-day boundary) to Redis. **Never run more than `1` replica** and never fold `--beat` into `worker` or `api`. |

### Key Design Guarantees

- **Zero automatic schema mutation on boot:** Web workers start `exec gunicorn` directly as PID `1`. `migrate`, `flush`, `seed_demo`, and `collectstatic` **never** execute automatically on container boot.
- **Pre-built immutable static assets:** `collectstatic --noinput` runs once during `docker build` using transient build-only placeholders (never stored in image `ENV` layers), baking `staticfiles.json` and pre-compressed assets into `/app/staticfiles`.
- **Synchronous transactional emails preserved:** Booking confirmations, payment receipts, cancellation notices, and portal magic links continue to send synchronously during the request lifecycle via configured HTTPS email API providers (`brevo` / `resend` / `sendgrid` / `mailgun` / `postmark`) or SMTP — never hidden behind a broker queue.
- **Oracle MySQL 8.0+ & MariaDB parity:** Uses `django.db.backends.mysql` with strict `utf8mb4` character set enforcement and full compatibility with both Oracle MySQL 8.0+ (RDS, Cloud SQL, DigitalOcean, Aiven, `mysql:8.0`) and MariaDB.

---

## 2. Local Development & Integration Setup (Docker Compose)

From the repository root:

```bash
# 1. Create a local environment file from the safe template (ignored by git & Docker)
cp .env.example .env

# 2. Build the backend image and start MySQL 8.0, Redis 7, API, Worker, and Beat
docker compose up -d --build --wait

# 3. Inspect service health
docker compose ps

# 4. Run database migrations explicitly once
docker compose exec api python manage.py migrate --noinput

# 5. Verify UTF-8 charset health and media configuration
docker compose exec api python manage.py check_database_charset
docker compose exec api python manage.py check_media_storage

# 6. (Optional, local dev only) Seed starter hotel catalog + Nigerian statutory payroll rules
docker compose exec api python manage.py seed_demo
docker compose exec api python manage.py seed_nigeria_payroll_rules
docker compose exec api python manage.py createsuperuser
```

Start the static frontend locally in a second terminal if desired:

```bash
cd frontend
python3 dev_server.py 5500 http://127.0.0.1:8000
```

- **Backend API root:** `http://127.0.0.1:8000/api/`
- **Swagger / ReDoc:** `http://127.0.0.1:8000/api/docs/` · `http://127.0.0.1:8000/api/redoc/`
- **Frontend UI (proxied same-origin):** `http://127.0.0.1:5500`

### Stopping & Rebuilding Safely Without Data Loss

```bash
# Stop and remove containers while keeping `mysql_data`, `redis_data`, and `media_data` volumes intact:
docker compose down

# Rebuild after code changes without losing local MySQL records or uploaded media:
docker compose up -d --build --wait
```

> **Warning:** `docker compose down -v` permanently deletes the local `mysql_data`, `redis_data`, and `media_data` volumes. Only run `-v` when intentionally resetting local development data.

---

## 3. Building & Running the Standalone Production Image

Build from the repository root (works identically whether the platform root directory is repo root with Dockerfile `backend/Dockerfile` or `backend/` with context `.`):

```bash
docker build -t jone-hotel-backend:latest ./backend
```

Run a standalone web container (for example on a VPS, Render, Railway, Fly.io, ECS, or Cloud Run):

```bash
docker run -d \
  --name jone-api \
  -p 127.0.0.1:8000:8000 \
  -e PORT=8000 \
  -e DJANGO_SETTINGS_MODULE=config.settings.production \
  -e DJANGO_DEBUG=False \
  -e DJANGO_SECRET_KEY="<64-char-random-production-secret>" \
  -e DJANGO_ALLOWED_HOSTS="api.hotel.example.com" \
  -e SECURE_SSL_REDIRECT=True \
  -e DATABASE_URL="mysql://app_user:strong_password@mysql.internal:3306/jone_hotel" \
  -e REDIS_URL="rediss://default:strong_password@redis.internal:6379/0" \
  -e FRONTEND_URL="https://hotel.example.com" \
  -e CORS_ALLOWED_ORIGINS="https://hotel.example.com" \
  -e CSRF_TRUSTED_ORIGINS="https://hotel.example.com,https://api.hotel.example.com" \
  -e EMAIL_PROVIDER="brevo" \
  -e EMAIL_API_KEY="<provider-api-key>" \
  -e DEFAULT_FROM_EMAIL="J-ONE Hotel & Lodge <reservations@hotel.example.com>" \
  -e PAYSTACK_SECRET_KEY="<sk_live_...>" \
  -e BACKBLAZE_KEY_ID="<key-id>" \
  -e BACKBLAZE_APPLICATION_KEY="<application-key>" \
  -e BACKBLAZE_BUCKET_NAME="<bucket-name>" \
  -e BACKBLAZE_ENDPOINT="https://s3.us-west-004.backblazeb2.com" \
  jone-hotel-backend:latest
```

Gunicorn binds to `0.0.0.0:${PORT:-8000}` automatically. Override concurrency via:
- `GUNICORN_WORKERS` (default `2` — safe for `512MB–1GB` containers)
- `GUNICORN_THREADS` (default `2` with `gthread` worker class)
- `GUNICORN_TIMEOUT` (default `60`)
- `GUNICORN_GRACEFUL_TIMEOUT` (default `30`)

---

## 4. Connecting to an External Managed MySQL Server

`config.settings.production` supports both `DATABASE_URL` and discrete `DB_*` variables against external managed MySQL 8.0+ or MariaDB (Railway MySQL, AWS RDS, Google Cloud SQL, DigitalOcean Managed MySQL, Aiven, or a dedicated DB host):

### Option A — Single `DATABASE_URL` (Recommended on PaaS)

```dotenv
DATABASE_URL=mysql://jone_prod:STRONG_PASSWORD@mysql-prod.example.com:3306/jone_hotel
```

For managed MySQL providers enforcing explicit server TLS verification, pass standard MySQL client options on the URL query string if required by the provider:

```dotenv
DATABASE_URL=mysql://jone_prod:STRONG_PASSWORD@mysql-prod.example.com:25060/jone_hotel?ssl-mode=REQUIRED
```

### Option B — Discrete `DB_*` Variables

Leave `DATABASE_URL` unset/blank and set:

```dotenv
DB_HOST=mysql-prod.example.com
DB_PORT=3306
DB_NAME=jone_hotel
DB_USER=jone_prod
DB_PASSWORD=STRONG_PASSWORD
```

### Database Creation Checklist (Managed MySQL)

Always create the database with `utf8mb4` so Nigerian currency symbols (`₦`), accented guest names, and notes never fail charset checks:

```sql
CREATE DATABASE IF NOT EXISTS jone_hotel
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

---

## 5. Safe Database Migration Workflow (Zero Automatic Destructive Ops)

Migrations **must** be executed as an explicit release/one-off step — **never** inside `CMD`/`ENTRYPOINT` of web workers (where concurrent multi-worker deploys race on DDL locks).

### Standard Production Release Sequence

```bash
# 1. Back up the production database BEFORE applying schema changes (see Section 10).

# 2. Inspect pending migrations cleanly against the target image:
docker run --rm --env-file /etc/jone/backend.env jone-hotel-backend:<release-sha> \
  python manage.py showmigrations --plan | grep '\[ \]' || true

# 3. Apply forward migrations explicitly once:
docker run --rm --env-file /etc/jone/backend.env jone-hotel-backend:<release-sha> \
  python manage.py migrate --noinput

# 4. Run post-migration operational sanity checks:
docker run --rm --env-file /etc/jone/backend.env jone-hotel-backend:<release-sha> \
  python manage.py check_database_charset

# 5. Roll out the new image to `api`, `worker`, and `beat`.
```

Platform equivalents:
- **Railway:** Run `python manage.py migrate --noinput` as a **Pre-deploy Command** on the API service (runs once before swapping traffic, not on replicas/restarts).
- **Render:** Set `Pre-Deploy Command` to `python manage.py migrate --noinput` (or keep `build.sh` on native Python runtime).
- **Fly.io:** Set `[deploy] release_command = "python manage.py migrate --noinput"` in `fly.toml`.

---

## 6. Static Assets (WhiteNoise) & Media Storage (Backblaze B2 vs Persistent Volume)

### Static Files (`/static/*`)

- Admin CSS/JS and OpenAPI Swagger/ReDoc assets are collected during `docker build` into `/app/staticfiles` using `whitenoise.storage.CompressedManifestStaticFilesStorage`.
- Served directly by Gunicorn + WhiteNoise with long-lived immutable cache headers and gzip/brotli compression. No Nginx static volume sharing is required.

### User-Uploaded Media (Room Photos, Gallery, Staff Profiles, Biometrics, Receipts)

Choose **one** of two production modes:

1. **Backblaze B2 S3-Compatible Object Storage (Recommended for all cloud/multi-replica deployments):**
   Set `BACKBLAZE_KEY_ID`, `BACKBLAZE_APPLICATION_KEY`, `BACKBLAZE_BUCKET_NAME`, and `BACKBLAZE_ENDPOINT`. `configure_b2_media_storage()` automatically activates `S3Boto3Storage` with B2-compatible checksum/ACL settings. Containers remain completely stateless.
   Verify anytime with:
   ```bash
   docker exec jone-api python manage.py check_media_storage
   ```
2. **Mounted Persistent Volume Fallback (Single-host VPS or single-instance PaaS volume):**
   Leave `BACKBLAZE_*` blank and mount a persistent host/platform volume at `/app/media` (`MEDIA_ROOT=/app/media`), owned by UID/GID `10001:10001`, and set `MEDIA_PUBLIC_BASE_URL=https://api.hotel.example.com`.
   > **Warning:** Never write production uploads to an unmounted container filesystem — container filesystems reset on every redeploy!

---

## 7. HTTPS, CORS, CSRF, Reverse Proxies & Receptionist Workstation IP Policy

### Reverse Proxy Headers (Nginx / Caddy / Cloud Load Balancers)

Gunicorn trusts `X-Forwarded-Proto: https` (`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`). Ensure your reverse proxy forwards the real client IP and scheme:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 15M;
}
```

### Critical Note on `TRUSTED_PROXY_CIDRS` & Receptionist Desktop IP Enforcement

J-ONE Hotel & Lodge enforces workstation IP rules for receptionist and staff operations (`apps/core/client_ip.py`).
- **Default behavior (recommended):** Leave `TRUSTED_PROXY_CIDRS` and `TRUSTED_CLIENT_IP_HEADERS` **unset / commented out**. Built-in defaults already trust loopback, RFC1918 private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and CGNAT (`100.64.0.0/10` used by Railway/Render/Fly internal meshes) while refusing spoofed headers from untrusted public peers.
- **Do NOT set `TRUSTED_PROXY_CIDRS=` as an empty string** unless your container faces the public internet directly with zero reverse proxies — an explicit empty string disables forwarded-header unwinding.

### Split-Origin Frontend + Backend Settings Example

When the static frontend runs on `https://hotel.example.com` (e.g. Vercel / Cloudflare Pages / Netlify) and the Docker backend runs on `https://api.hotel.example.com`:

```dotenv
DJANGO_ALLOWED_HOSTS=api.hotel.example.com
FRONTEND_URL=https://hotel.example.com
CORS_ALLOWED_ORIGINS=https://hotel.example.com
CSRF_TRUSTED_ORIGINS=https://hotel.example.com,https://api.hotel.example.com
SECURE_SSL_REDIRECT=True
```

In `frontend/vercel.json` (or `frontend/js/runtime-config.js`), point `/api/*` rewrites (or `API_BASE_URL`) at `https://api.hotel.example.com` whenever moving the backend to a new host.

---

## 8. Health Checks, Liveness vs Readiness & Graceful Shutdown

| Endpoint | Methods | Purpose | Response (`200`) | Failure (`503`) |
|---|---|---|---|---|
| `/api/health/live/` | `GET`, `HEAD` | Container process liveness (zero DB queries; prevents restart storms during brief DB maintenance) | `{"success":true,"status":"ok"}` | Process unreachable |
| `/api/health/ready/` | `GET`, `HEAD` | Traffic readiness (executes `SELECT 1` on MySQL; zero secret/error detail leakage) | `{"success":true,"status":"ok","database":"up"}` | `{"success":false,"status":"degraded","database":"down"}` |
| `/api/health/` | `GET`, `HEAD` | Legacy readiness alias preserved for existing monitors and frontend `vercel.json` checks | `{"success":true,"status":"ok","database":"up"}` | `503` degraded |

- **Plain HTTP orchestrator compatibility:** Even when `SECURE_SSL_REDIRECT=True` and `ALLOWED_HOSTS` contains only public domains, internal orchestrator plain-HTTP probes from trusted private/loopback proxy addresses to `/api/health/live/`, `/api/health/ready/`, and `/api/health/` return `200`/`503` directly instead of `301` or `400 DisallowedHost`. External/untrusted hosts and all non-health paths strictly enforce `ALLOWED_HOSTS` and `SECURE_SSL_REDIRECT`.
- **Built-in `HEALTHCHECK`:** `backend/docker/container-healthcheck.py` auto-detects whether the container runs Gunicorn (probing `http://127.0.0.1:${PORT}/...`), Celery `worker` (checking live worker PIDs), or Celery `beat` (checking PID + fresh schedule file).
- **Graceful shutdown:** `STOPSIGNAL SIGTERM` forwards directly to PID `1` (`exec gunicorn`), allowing in-flight checkout/webhook requests up to `GUNICORN_GRACEFUL_TIMEOUT` (`30s`) to finish cleanly before container exit.

---

## 9. Deploying Across Cloud Providers & VPS

| Platform | How to Deploy This Image |
|---|---|
| **Railway** | Point service **Root Directory** at `backend` (auto-detects `backend/Dockerfile`). Add a Pre-deploy command `python manage.py migrate --noinput`. To run scheduled tasks, add two additional services pointing to the same repo/`backend` with custom start commands `celery -A config worker -l info --concurrency 2` and `celery -A config beat -l info --schedule /var/tmp/jone/celerybeat-schedule` (`1` replica). |
| **Render** | Existing native `backend/render.yaml` + `backend/build.sh` continue to work unchanged, OR switch Web Service runtime to `Docker` with `Dockerfile Path: ./backend/Dockerfile`, `Docker Build Context Directory: ./backend`, `Health Check Path: /api/health/ready/`, and `Pre-Deploy Command: python manage.py migrate --noinput`. |
| **Fly.io** | Build from `./backend/Dockerfile`, set `internal_port = 8080` + `PORT = "8080"`, `[http_service.checks] path = "/api/health/ready/"`, `[deploy] release_command = "python manage.py migrate --noinput"`, and define process groups `app`, `worker`, `beat`. |
| **VPS (Ubuntu / Debian + Nginx / Caddy)** | Place secrets in `/etc/jone/backend.env` (`chmod 600`), run `compose.yaml` (binding `127.0.0.1:8000:8000` only) with `SECURE_SSL_REDIRECT=True`, terminate TLS in Caddy or Nginx + Let's Encrypt, and point either to external Managed MySQL via `DATABASE_URL` or to the persistent `mysql_data` volume with automated off-host backups. |

---

## 10. Database Backup & Recovery Runbook

### Taking a Consistent Point-in-Time Backup (`mysqldump --single-transaction`)

Against Compose MySQL:

```bash
mkdir -p backups
chmod 700 backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T db sh -c \
  'mysqldump --single-transaction --routines --triggers --events --default-character-set=utf8mb4 -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' \
  | gzip -9 > "backups/jone_hotel_${STAMP}.sql.gz"
```

Against an external Managed MySQL server:

```bash
mysqldump \
  --single-transaction \
  --routines \
  --triggers \
  --set-gtid-purged=OFF \
  --default-character-set=utf8mb4 \
  -h "$DB_HOST" -P "${DB_PORT:-3306}" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" \
  | gzip -9 > "backups/jone_hotel_$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
```

> **Security rule:** Never store `.sql` / `.sql.gz` backups inside the Git repository or public buckets. Always store encrypted backups off-host and test restores regularly.

### Restoring a Backup Safely

```bash
# 1. Stop write traffic (`api`, `worker`, `beat`) before restoring:
docker compose stop api worker beat

# 2. Restore into MySQL:
gunzip -c backups/jone_hotel_<timestamp>.sql.gz | docker compose exec -T db sh -c \
  'mysql --default-character-set=utf8mb4 -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'

# 3. Bring services back up and verify readiness:
docker compose up -d --wait
curl -fsS http://127.0.0.1:8000/api/health/ready/
```

---

## 11. Rollback Plan

Always tag production images immutably by Git commit SHA (`jone-hotel-backend:<git-sha>`):

1. **Application-only rollback (no irreversible schema drop occurred):**
   Redeploy the previous image tag (`jone-hotel-backend:<previous-sha>`) across `api`, `worker`, and `beat`, then verify `GET /api/health/ready/` returns `200`.
2. **Release included reversible Django migrations:**
   Using the **current** release image, migrate back to the exact migration target of `<previous-sha>` (`python manage.py migrate <app_label> <target_migration>`), then redeploy `jone-hotel-backend:<previous-sha>`.
3. **Emergency rollback to non-Docker runtime (Railway Nixpacks / Render native Python):**
   `backend/Procfile` / `backend/render.yaml` / `backend/build.sh` and `backend/requirements.txt` remain completely intact. Re-selecting native Python runtime in Railway/Render immediately restores pre-Docker execution against the exact same external MySQL, Redis, Paystack, Email, and Backblaze B2 environment variables.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Container exits immediately with `ImproperlyConfigured` | Required production variable (`DJANGO_SECRET_KEY`, `DATABASE_URL`/`DB_*`, `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, or email provider setting) is missing or uses an insecure placeholder | Inspect `docker logs <container>` — the exact missing variable name is printed cleanly without exposing secret values. |
| `400 Bad Request` (`DisallowedHost`) on browser/API calls | API hostname missing from `DJANGO_ALLOWED_HOSTS` | Add exact hostname (no `https://`, no trailing slash) to `DJANGO_ALLOWED_HOSTS`. |
| Browser console reports CORS error on `/api/*` | Frontend origin missing from `CORS_ALLOWED_ORIGINS` (or trailing slash included) | Set exact `https://frontend.domain` (no trailing slash) in `CORS_ALLOWED_ORIGINS`. |
| Receptionist sign-in returns `403 STAFF_SIGN_IN_RESTRICTED` showing a `10.x` / `172.x` proxy IP | `TRUSTED_PROXY_CIDRS` or `TRUSTED_CLIENT_IP_HEADERS` was set to an empty string (`""`) in env, or reverse proxy omits `X-Forwarded-For` / `X-Real-IP` | Remove empty `TRUSTED_PROXY_CIDRS` / `TRUSTED_CLIENT_IP_HEADERS` env overrides and ensure your proxy sets `X-Real-IP` and `X-Forwarded-For`. |
| `/api/health/ready/` returns `503 {"database":"down"}` while `/api/health/live/` returns `200` | MySQL host unreachable, wrong credentials, or TLS mismatch | Check `DATABASE_URL` / firewall / VPC peering; container stays alive so orchestrators do not thrash restarts while MySQL recovers. |
| Online card checkout returns `503 PAYMENT_NOT_CONFIGURED` | `PAYSTACK_SECRET_KEY` is blank | Expected safe behavior when unconfigured; set server-side `PAYSTACK_SECRET_KEY` in backend environment variables. |
