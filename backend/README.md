# J-ONE HOTEL & LODGE — Backend API

Production-grade Django + Django REST Framework backend powering:

1. the **public hotel website** (independently hosted HTML/CSS/Vanilla-JS frontend),
2. the **staff hotel management dashboard**, and
3. the **verified-email guest portal**.

All three use this same authoritative Django API and database. Django is used
strictly as a **JSON API backend** — the frontend never touches
Django templates, template context, or the database.

- Hotel: **J-ONE HOTEL & LODGE**, Plot 566 Mgbowo Street, off Ezike Street
- Phone: **+234803 211 2874** · Email: **jonathanonu76@gmail.com**
- Currency: **NGN** · Timezone: **Africa/Lagos**

---

## 1. Technology stack

| Area | Choice |
|---|---|
| Language | Python 3.11+ |
| Framework | Django 5.2.x, Django REST Framework |
| Database | MySQL 8.0+ (production) · SQLite (local dev) |
| Auth | JWT (djangorestframework-simplejwt, rotation + blacklist) |
| Payments | Paystack (server-side init/verify + signed webhooks) |
| Media | Local disk (dev) · Backblaze B2 / S3-compatible (production) |
| Cache / throttle / broker | Redis in production (Django's built-in Redis backend) |
| Background tasks | Celery + Celery Beat (booking lifecycle + nightly finance recognition; email remains synchronous) |
| Docs | OpenAPI 3 via drf-spectacular (`/api/docs/`) |
| Serving | Gunicorn + WhiteNoise |

All configuration is environment-driven (python-decouple + python-dotenv).
No secret ever lives in code — see [`.env.example`](.env.example).

## 2. Project layout

```
backend/
├── manage.py
├── requirements.txt
├── .env.example
├── config/
│   ├── settings/{base,development,production}.py
│   ├── urls.py  wsgi.py  asgi.py  celery.py
├── apps/
│   ├── core/          # shared envelope, pagination, exceptions, permissions
│   ├── accounts/      # custom User (email auth), roles, JWT auth endpoints
│   ├── hotel/         # hotel settings (singleton), policies, facilities
│   ├── rooms/         # room types, images, amenities, physical rooms
│   ├── offers/        # offers/discounts + eligibility service
│   ├── gallery/       # photo gallery
│   ├── bookings/      # guests, bookings, booking↔room assignments + engines
│   ├── stays/         # operational stays, room-occupancy history
│   ├── payments/      # payments, Paystack integration, webhooks
│   ├── finance/       # immutable ledger, folios, approvals, cash sessions
│   ├── pos/           # menu, kitchen tickets, POS/room-service orders
│   ├── guest_services/ # guest requests, events, portal-safe comments
│   ├── housekeeping/  # room-status and housekeeping operations
│   ├── maintenance/   # maintenance work orders and evidence
│   ├── inventory/     # items, suppliers, stock movements, procurement
│   ├── staff_operations/ # profiles, shifts, attendance, leave
│   ├── portal/        # verified-email guest portal sessions
│   ├── enquiries/     # contact form + staff handling
│   ├── notifications/ # in-app notifications + email task
│   ├── reports/       # staff dashboard aggregation + revenue/occupancy reports
│   └── audit/         # immutable audit trail (read-only)
├── tests/             # automated API/integration/regression tests
└── docs/              # API, architecture, booking flow, frontend contract
```

Business logic lives in **service modules** (e.g. `apps/bookings/services/`),
not in views or serializers — views stay thin and everything is testable.

## 3. Quick start (local development)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # mysqlclient may be skipped locally, see §11
cp .env.example .env                   # defaults are dev-ready

python manage.py migrate
python manage.py seed_demo             # hotel identity + DEMO room content
python manage.py createsuperuser       # your admin login
python manage.py runserver
```

API index:      http://localhost:8000/api/
Swagger docs:   http://localhost:8000/api/docs/
Legacy readiness: http://localhost:8000/api/health/
Liveness:        http://localhost:8000/api/health/live/  (process only; no DB check)
Readiness:       http://localhost:8000/api/health/ready/ (database check)
Django admin:    http://localhost:8000/django-admin/  (internal fallback only)

> **Demo data note:** `seed_demo` loads the real hotel identity plus clearly
> labeled DEMO room types/rooms/offers (spec §120–§121). Replace demo prices,
> images and counts with real hotel content via the staff API before launch.

## 4. Authentication (used by the frontend)

```http
POST /api/auth/register/          → { user, tokens }   (creates GUEST account)
POST /api/auth/login/             → { user, tokens }   (email + password)
POST /api/auth/token/refresh/     → rotated access (+refresh) token
POST /api/auth/logout/            → blacklists the refresh token
GET|PATCH  /api/auth/profile/     → profile read/update
POST /api/auth/password/change/
POST /api/auth/password/reset/    → email with reset link (never leaks existence)
POST /api/auth/password/reset/confirm/
```

Send `Authorization: Bearer <access_token>` on protected endpoints. Tokens are
JWT; access ≈30 min, refresh ≈7 days, refresh tokens rotate and are
blacklisted on use/logout.

**Roles:** `GUEST` (public users only), `RECEPTIONIST`, `MANAGER`, `ADMIN`.
Roles are **always resolved server-side** from the database — the frontend can
never grant itself privileges. Registration always creates `GUEST` accounts;
staff roles are assigned by an ADMIN via `/api/admin/users/`. New operational
areas add capability and team-scope checks on top of these legacy role labels;
the server remains authoritative.

**CSRF:** JWT flows authenticate with `Authorization: Bearer` headers rather
than cookies. CORS is still restricted to exact frontend origins in production.

### Verified-email portal authentication

The portal does **not** accept a staff JWT or booking
`X-Guest-Access-Token`. Its deliberately separate flow is:

```http
POST /api/portal/auth/request/  # generic/non-enumerating email-link response
POST /api/portal/auth/consume/  # one-use challenge -> opaque short-lived session
GET  /api/portal/me/            # X-Portal-Session: <opaque-session>
POST /api/portal/auth/logout/   # X-Portal-Session: <opaque-session>
```

The server stores only hashes of challenge/session credentials. The client
holds the opaque session only in active-page memory and sends it solely in the
explicit `X-Portal-Session` header with no staff JWT. It is neither a cookie
nor durable browser storage. Portal views scope records by the verified email,
keep list reads bounded/paginated, and require an owned active in-house stay
for guest-service request creation. Logout revokes the server-side session.

## 5. Environments & settings modules

| Module | Use |
|---|---|
| `config.settings.development` | SQLite, console email, synchronous Celery, permissive CORS |
| `config.settings.production`  | MySQL (required), B2 media, Redis cache, strict security headers, SMTP |

`manage.py` defaults to development; `config/wsgi.py` (Gunicorn) defaults to
production. Override anywhere with `DJANGO_SETTINGS_MODULE`.

### Browser-origin deployment contract

The public site, staff console, and verified-email portal may share one origin
or be independently hosted, but always use this one Django API and database.
Choose real HTTPS origins per environment rather than hard-coding hosts:

| Concern | Environment/configuration |
|---|---|
| Public checkout/callback origin | `FRONTEND_URL` and, when needed, `PAYMENT_CALLBACK_URL` |
| Portal magic-link origin | `PORTAL_FRONTEND_URL` (falls back to `FRONTEND_URL` only when intentionally blank) |
| Browser origins permitted to call the API | `CORS_ALLOWED_ORIGINS` — every real public, management, and portal origin; exact scheme + host, comma-separated |
| Django form/admin/cookie trust | `CSRF_TRUSTED_ORIGINS` — matching exact HTTPS origins where Django cookies/forms are used |
| API hosts accepted by Django | `DJANGO_ALLOWED_HOSTS` — API hostnames only, no scheme |
| Frontend API target | the public `frontend/js/runtime-config.js` `API_BASE_URL` (or empty for same-origin `/api/`) |

For example:

```dotenv
FRONTEND_URL=https://hotel.example.com
PORTAL_FRONTEND_URL=https://guest.example.com
DJANGO_ALLOWED_HOSTS=api.example.com
CORS_ALLOWED_ORIGINS=https://hotel.example.com,https://manage.example.com,https://guest.example.com
CSRF_TRUSTED_ORIGINS=https://hotel.example.com,https://manage.example.com,https://guest.example.com
```

`CORS_ALLOWED_ORIGINS` is an exact allowlist: no wildcard, URL path, trailing
slash, or unneeded origin. The API explicitly permits `x-portal-session` in
CORS preflight headers. Public/staff JWT flows and the current header-based
portal session need no cross-origin cookies, so `CORS_ALLOW_CREDENTIALS` remains
`False`. Do not weaken that setting to fix a bad origin configuration.

## 6. MySQL (production)

Provide either `DATABASE_URL=mysql://user:pass@host:3306/jone_hotel`
or the `DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT` components.

```sql
CREATE DATABASE jone_hotel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Production refuses to boot on SQLite (guard in `production.py`).

## 7. Paystack

1. Get test/live keys: <https://dashboard.paystack.com/#/settings/developers>
2. Set `PAYSTACK_SECRET_KEY` on the **backend only**. `PAYSTACK_PUBLIC_KEY` is optional for this hosted-redirect checkout.
3. Webhook URL (Paystack dashboard): `https://<your-api-host>/api/payments/webhook/`
   — the endpoint validates the `x-paystack-signature` HMAC before processing.
4. Enable these webhook events in Paystack: `charge.success`, `refund.pending`,
   `refund.processing`, `refund.processed`, `refund.failed` and, where available,
   `refund.needs-attention`; dispute webhooks may also be enabled for alerts.
5. `PAYMENT_CALLBACK_URL` should point at your frontend's verify page
   (default: `{FRONTEND_URL}/payment-verify.html`).
6. Paystack Dashboard transaction receipts may be enabled there for card/bank
   payment receipts only. Booking, cancellation, refund-status and staff emails
   are sent by the app's SMTP/Brevo-style transactional email configuration.
7. To make **the hotel/merchant bear Paystack charges**, open Paystack Dashboard
   → **Settings** → **Preferences** → **Transaction fees** and leave **Pass fees
   to customers** unchecked. Verification requires Paystack's original `requested_amount` (or the compatible `amount - fees` fallback) to equal the backend-created Payment amount in kobo. Paystack `fees` are stored only as merchant-settlement metadata and never affect guest-facing amounts.

If keys are missing the API returns `503 PAYMENT_NOT_CONFIGURED` explicitly —
there is **no fake "demo payment" success path**. Browser redirects never confirm
a booking unless the backend verifies the transaction with Paystack.

## 8. Media / Backblaze B2 (production)

When `BACKBLAZE_KEY_ID/APPLICATION_KEY/BUCKET_NAME/ENDPOINT` are set
(and DEBUG=False), uploads (room images, gallery, etc.) go to B2 via
django-storages. Make the bucket **public** (or provide `MEDIA_CUSTOM_DOMAIN`)
so `--` URLs render in browsers. Uploads are validated for extension, size
(`MAX_UPLOAD_MB`) and real image content.

## 9. Background tasks & email

### Scheduled tasks (Celery)

`apps.bookings.tasks` and `apps.finance.tasks` run the time-based hotel jobs —
pending-booking expiry (every 5 min), automatic checkout/check-out warnings,
and prior-night accommodation recognition (00:10 Africa/Lagos; cursor-continued in bounded pages):

```bash
celery -A config worker -l info
celery -A config beat -l info
```

State changes are also reconciled lazily on reads, so an expired hold never
blocks inventory even before the beat task runs.

### Transactional email

**Email delivery is synchronous and never touches Celery or Redis.** Messages
are sent inside the request that triggers them, so a missing broker can never
lose mail. The business rules for when mail is sent are independent of the
transport described here.

The transport sits behind a small adapter in
`apps/notifications/providers.py`. One variable selects it:

| `EMAIL_PROVIDER` | Transport |
| --- | --- |
| `django`, `smtp`, `console` | Django's own `EMAIL_BACKEND` |
| `brevo`, `sendgrid`, `mailgun`, `postmark`, `resend` | That vendor's HTTPS API |
| *(empty)* | An API provider if a key is set, otherwise Django's backend |

SMTP needs `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_USE_TLS`/`EMAIL_HOST_USER`/
`EMAIL_HOST_PASSWORD`. The API providers need `EMAIL_API_KEY` (plus
`EMAIL_API_DOMAIN` for Mailgun, and optionally `EMAIL_API_BASE_URL` for
regional endpoints). Both need `DEFAULT_FROM_EMAIL` to be an address verified
with the active provider. Adding a vendor means adding one adapter — callers
never change. See `.env.example` for every variable.

Development defaults to the console backend, so nothing leaves the machine.
API providers are the right choice on hosts that block outbound SMTP ports.

Verify the whole chain for whichever provider is active:

```bash
python manage.py email_check                     # config + auth + sender
python manage.py email_check --send you@example.com   # + one real delivery
```

### Email logs

`EmailLog` records `PENDING → SENDING → SENT | FAILED`. `SENT` is written only
after the provider accepts the message; a rejection stores `FAILED` with the
provider's reason. Credentials are never logged.

### Logo rendering

Email templates use the hotel logo at its real aspect ratio. Where the
transport supports `multipart/related` (SMTP, SendGrid, Mailgun, Postmark) the
logo is embedded inline as a `cid:` part. Brevo's and Resend's APIs cannot
deliver inline images, so those providers fall back to the absolute HTTPS URL
in `EMAIL_LOGO_URL`. Set it whenever one of them is active, or the header logo
will not render.

## 10. Testing

```bash
python manage.py test          # full backend suite: auth, availability, pricing,
                               # bookings, payments/refunds (mocked Paystack), staff, security
python manage.py spectacular --file schema.yml --validate   # OpenAPI sanity
```

## 11. Troubleshooting

**`mysqlclient` fails to build locally** — it's only needed for production.
Install system headers (`sudo apt install pkg-config default-libmysqlclient-dev
build-essential python3-dev`) or skip it locally (SQLite is the dev default).

**Images 404 in dev** — media is served by runserver only when DEBUG=True.

**429 responses while testing** — sensitive endpoints are rate-limited; wait a
minute or reset the dev server (locmem cache).

**Emails don't "arrive" in dev** — they print to the server console by design.

## 12. Deployment (provider-independent)

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#deployment) for the full
layout. The web process is standard Gunicorn behind any HTTPS-capable proxy;
MySQL 8+ is authoritative. Background worker/beat processes are optional
operational components where scheduled booking and nightly-finance jobs are enabled, not a
requirement of a specific hosting provider.

- Build command: `./build.sh` (installs deps, collects static, migrates)
- Start command: `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`
- Liveness probe: `/api/health/live/`; readiness probe: `/api/health/ready/`
  (`/api/health/` remains a compatibility readiness alias)
- Set `DJANGO_ALLOWED_HOSTS`, database, frontend/CORS, `PORTAL_FRONTEND_URL`,
  media, email, and Paystack values from `.env.example` with environment-specific
  production values. Never deploy example origins or credentials.
- On the static frontend host, serve `/portal/**` with `Cache-Control: no-store`
  and `X-Robots-Tag: noindex, nofollow`; the service worker has the same
  network-only rule but must not be the sole protection.

The same commands work on Render, Railway, Fly.io, or an Ubuntu VPS; each
platform supplies its own `$PORT`, process supervisor, and environment-variable
configuration.

## 13. Documentation

- [`docs/API.md`](docs/API.md) — endpoint inventory with auth/params/responses
- [`docs/BOOKING_FLOW.md`](docs/BOOKING_FLOW.md) — guest booking + payment state machine
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design, engines, data model
- [`docs/FRONTEND_CONTRACT.md`](docs/FRONTEND_CONTRACT.md) — **the binding frontend↔backend contract**
- Swagger UI `/api/docs/` · ReDoc `/api/redoc/` · raw schema `/api/schema/`
