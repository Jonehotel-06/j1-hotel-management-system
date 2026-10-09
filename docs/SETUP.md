# Setup and deployment

How to install, configure, validate, and deploy J-ONE Hotel & Lodge. The
application is one Django API and one MySQL database serving the public site,
staff console, and verified-email guest portal. For operational use, see
[USAGE.md](USAGE.md).

## 1. Requirements

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.11+ | Django API, management commands, frontend build/dev server |
| Node.js | 18+ | frontend `node --test` checks |
| MySQL | 8.0+ | production database; local development defaults to SQLite |
| Redis | 5+ | production cache/throttling and scheduled-job broker only |

The static frontend has no package-manager install step. There is no separate
portal backend or database.

## 2. Local development

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python manage.py migrate
python manage.py seed_demo          # hotel identity + clearly labelled DEMO rooms
python manage.py createsuperuser    # initial administrator
python manage.py runserver
```

- API index: <http://localhost:8000/api/>
- Swagger: <http://localhost:8000/api/docs/> · ReDoc: `/api/redoc/`
- Liveness: `/api/health/live/`
- Readiness: `/api/health/ready/`

`seed_demo` is only starter content. Replace demo prices, imagery, room counts,
and staff accounts before launch.

### Frontend

```bash
cd frontend
python3 dev_server.py 5500 http://127.0.0.1:8000
```

Open <http://127.0.0.1:5500>. The same-origin proxy forwards `/api/` and
`/media/` to Django, which mirrors a reverse-proxy deployment without placing a
backend URL in browser code.

- Staff sign-in: `/login.html`
- Guest portal sign-in: `/portal/login.html`
- Booking-specific self-service: `/my-bookings.html` (a separate access model)

After editing shared public chrome in `frontend/components/`, run:

```bash
cd frontend
python3 build.py
python3 validate.py
```

## 3. Settings and browser origins

All runtime settings are environment variables; use
[`backend/.env.example`](../backend/.env.example) as the annotated source of
truth. Never commit a populated `.env` file or place a secret in frontend
configuration.

### Core production settings

| Variable | Required behavior |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` in production. |
| `DJANGO_SECRET_KEY` | Unique secret per environment. |
| `DJANGO_DEBUG` | `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | API hostnames only, comma-separated, no scheme or path. |
| `DATABASE_URL` or `DB_*` | MySQL 8+ connection. Production refuses SQLite. |
| `FRONTEND_URL` | Exact public booking-site origin; used by public email/callback defaults. |
| `PORTAL_FRONTEND_URL` | Exact portal origin used in magic links. Blank intentionally falls back to `FRONTEND_URL`. |
| `SERVICE_QR_FRONTEND_URL` | Exact static frontend origin for printed room/table service QR URLs. Blank falls back to `FRONTEND_URL`; the frontend must serve `/qr-service.html`. |
| `PAYMENT_CALLBACK_URL` | Exact public Paystack callback URL; defaults to `{FRONTEND_URL}/payment-verify.html`. |

A split-origin deployment might use these literal values:

```dotenv
FRONTEND_URL=https://hotel.example.com
PORTAL_FRONTEND_URL=https://guest.example.com
PAYMENT_CALLBACK_URL=https://hotel.example.com/payment-verify.html
DJANGO_ALLOWED_HOSTS=api.example.com
CORS_ALLOWED_ORIGINS=https://hotel.example.com,https://manage.example.com,https://guest.example.com
CSRF_TRUSTED_ORIGINS=https://hotel.example.com,https://manage.example.com,https://guest.example.com
```

`CORS_ALLOWED_ORIGINS` is an exact browser-origin allowlist: use scheme + host
only, with no path, trailing slash, wildcard, or unneeded origin. Include the
management origin only if it is actually hosted separately. `CSRF_TRUSTED_ORIGINS`
uses matching full HTTPS origins for Django-admin/forms or any explicitly added
cookie flow. Keep `CORS_ALLOW_CREDENTIALS` disabled; the existing portal design
does not use cross-origin cookies.

Each static deployment owns a public `frontend/js/runtime-config.js`:

```js
window.__APP_CONFIG__ = { API_BASE_URL: "https://api.example.com" };
```

Use an origin with no trailing slash. Serve this file with `Cache-Control:
no-store`; it is deployment configuration, not a secret. Leave `API_BASE_URL`
empty when a reverse proxy serves `/api/` from the same public origin. Do not
put an environment-specific API host in `js/config.js`.

### Verified-email guest portal

The portal is deliberately different from staff JWT authentication and from
`my-bookings.html` booking tokens.

| Variable | Default / purpose |
|---|---|
| `PORTAL_CHALLENGE_MINUTES` | `15`; maximum life of a one-use email link. |
| `PORTAL_SESSION_HOURS` | `8`; maximum life of a short-lived opaque portal session. |
| `PORTAL_SESSION_TOUCH_MINUTES` | `15`; minimum write interval for last-used telemetry. |
| `PORTAL_CHALLENGES_PER_EMAIL_PER_HOUR` | `5`; per-email challenge ceiling in addition to request throttling. |

A guest requests a non-enumerating magic link, consumes it once, then the
portal sends the opaque value only in `X-Portal-Session`. The browser holds it
only in active-page JavaScript memory: it is not a cookie, staff JWT,
booking `X-Guest-Access-Token`, `localStorage`, or `sessionStorage` value.
Refreshing the portal intentionally requires reopening a secure link. The
backend hashes portal credentials, scopes reads to the verified email, and
revokes the server-side session on logout.

For a split API/frontend deployment, the backend already allows the required
`x-portal-session` header explicitly. A CORS preflight failure is therefore an
origin-allowlist/configuration error—not a reason to enable wildcard origins or
credentials.

Portal pages carry `noindex, nofollow` metadata. Configure **every** static
host or reverse proxy to return these headers for `/portal/**`:

```text
Cache-Control: no-store
X-Robots-Tag: noindex, nofollow
```

The frontend service worker independently treats `/portal/` as network-only;
these HTTP headers protect the page even before a service worker is installed.

### Guest-service QR links

Set `SERVICE_QR_FRONTEND_URL` to the origin that actually serves
`qr-service.html` (it can equal `FRONTEND_URL`). The staff-only
`/dashboard/service-qr.html` page issues room or table links and downloadable
self-contained SVG codes. The browser-visible bearer is placed after `#` in the
URL so it is not included in the page request or ordinary referrer; the guest
page removes it from the address bar and sends it only in
`X-Service-QR-Token`. Configure your edge/APM/proxy not to record this header or
issuance response bodies.

Return the following headers for `/qr-service.html` on **every** static host:

```text
Cache-Control: no-store
X-Robots-Tag: noindex, nofollow
Referrer-Policy: no-referrer
```

Django marks every `/api/` response—including QR context, request creation,
issuance, rotation, and revocation—`private, no-store`; the guest page also uses
`fetch(..., {cache: "no-store"})`, and the service worker treats its navigation
as network-only. Allow the frontend origin in the exact
`CORS_ALLOWED_ORIGINS` list and retain the configured `x-service-qr-token`
request header. QR tokens are stored as digests, returned only when a code is
issued/rotated, and rotation invalidates every old print. Room requests are
accepted only when exactly one current in-house stay occupies the room; table
requests are anonymous and go only to Food & Beverage. The QR feature creates
service requests, **not** orders or charges; actual priced orders continue to
use the established POS workflow.

### Database, payments, email, and media

For production, provide either:

```dotenv
DATABASE_URL=mysql://user:password@host:3306/jone_hotel
```

or `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`. A new MySQL
database should use `utf8mb4`:

```sql
CREATE DATABASE jone_hotel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

For Paystack, set `PAYSTACK_SECRET_KEY` on the backend only and configure the
webhook as `https://<api-host>/api/payments/webhook/`. Enable at least
`charge.success`, `refund.pending`, `refund.processing`, `refund.processed`,
and `refund.failed`. A missing key produces an honest
`503 PAYMENT_NOT_CONFIGURED`; there is no synthetic success path.

Choose `EMAIL_PROVIDER` and its documented credential variables from
`.env.example`. `DEFAULT_FROM_EMAIL` must be a sender verified with that
provider. Check it without revealing credentials:

```bash
cd backend
python manage.py email_check
python manage.py email_check --send you@example.com
```

For production media, configure the Backblaze B2/S3-compatible variables in
`.env.example` and verify them with `python manage.py check_media_storage`.

## 4. Provider-independent deployment

The API and static frontend may be deployed to any compatible host. The
application has no provider-specific database dependency and the web command
honours the platform-assigned `$PORT`:

```bash
cd backend
gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --timeout 60
```

Use a release phase, migration job, or one controlled administrative command to
run database migrations exactly once per release:

```bash
cd backend
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py check
```

Do not run concurrent migrations from every web replica. Configure the host's
health check to call `/api/health/ready/`; use `/api/health/live/` only for a
process liveness probe.

If scheduled hotel jobs are enabled in production, run the existing worker and
scheduler as separate processes against the same environment and Redis:

```bash
celery -A config worker -l info --concurrency=2
celery -A config beat -l info
```

Email delivery remains synchronous and does not require the worker. The
frontend is a static deployment: publish `frontend/` with `sw.js` and
`manifest.webmanifest` at the site root, preserve explicit `.html` routes, and
apply the no-store portal headers above. The included `frontend/vercel.json` is
an optional static-host example, not a deployment requirement.

## 5. Release workflow

Run the following against a staging-equivalent environment before production:

```bash
# Backend
cd backend
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test

# Frontend
cd ../frontend
python3 build.py --bump patch
python3 validate.py
node --test tests-js/
```

Then:

1. Review `git diff --check` and deploy the API/schema migration safely.
2. Verify readiness, a staff capability-protected action, and Paystack webhook
   configuration in the target environment.
3. Publish the versioned static frontend, including portal pages and static-host
   no-store/noindex headers.
4. Request a real staging portal link, verify the generic request response,
   consume flow, `X-Portal-Session` requests, logout, and intentional
   refresh/reopen-link behavior.
5. Confirm the public booking flow, multi-room booking, payment callback, and
   existing `my-bookings.html` journey still work independently of the portal.

`build.py` is the only supported frontend release mechanism. It synchronizes
`version.json`, `js/version.js`, and the service-worker cache namespace and
stamps local CSS/JS assets. It includes portal pages and `qr-service.html` for asset stamping but
deliberately does not inject persistent theme bootstrap or reload-capable
PWA/update-controller scripts into these credential-bearing shells, because a
reload could discard an in-memory portal session or a newly issued QR code.

## 6. Troubleshooting

- **Portal link opens the wrong site** — set the exact HTTPS
  `PORTAL_FRONTEND_URL`; blank intentionally falls back to `FRONTEND_URL`.
- **Portal request is blocked by CORS** — compare the browser origin exactly to
  one entry in `CORS_ALLOWED_ORIGINS`, including scheme and subdomain, and make
  sure the API host permits `x-portal-session` (already configured by Django).
- **Portal refresh signs the guest out** — expected by design; reopen a new
  magic link rather than trying to persist the opaque session.
- **Images disappear after a deploy** — production media storage is incomplete;
  run `python manage.py check_media_storage` and configure durable storage.
- **429 during development** — sensitive routes are throttled. Wait for the
  window to pass or restart the local development server/cache.
- **Email does not arrive locally** — console email is intentional. Configure a
  real provider only when testing delivery.
