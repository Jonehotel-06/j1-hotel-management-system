# J-ONE HOTEL & LODGE — Online card payment initialization failure (HTTP 503)

**Incident:** public booking `J1-20261018-E013B22C` saved unpaid; browser console
`POST http://127.0.0.1:8000/api/payments/initialize/ -> HTTP 503`,
body `{"success": false, "code": "PAYMENT_NOT_CONFIGURED", ...}`.
**Repo:** `Jonehotel-06/j1-hotel-management-system` @ `54b85a8` (plus this fix, uncommitted working tree).
**Date:** 2026-10-18. No production record was created, modified, cancelled, or charged for this work.

---

## Phase One — Complete payment flow (traced in code)

| # | Step | File / function |
|---|---|---|
| 1 | Public booking submit | `frontend/booking-confirmation.html` → `pay()` (create → initialize → redirect) |
| 2 | Booking create + reference | `POST /api/bookings/` → `backend/apps/bookings/views.py`; reference `J1-YYYYMMDD-XXXXXXXX` from `generate_booking_reference()` in `backend/apps/core/utils.py` |
| 3 | Frontend payment-init request | `frontend/js/api.js` → `API.initPayment()` → `POST /api/payments/initialize/` with guest token via `guestAccessOpts()` |
| 4 | API client / base URL | `frontend/js/runtime-config.js` (`API_BASE_URL: ""`) → `frontend/js/config.js` `resolveApiBase()`: localhost → `http://127.0.0.1:8000`, any other host → same-origin `""` |
| 5 | Django route | `backend/config/urls.py` → `api/payments/` → `backend/apps/payments/urls.py` → `initialize/` |
| 6 | DRF view | `InitializePaymentView.post()` in `backend/apps/payments/views.py` (booking lookup → `can_access_booking` 404-gate → service) |
| 7 | Paystack service | `backend/apps/payments/services/payment_service.py` `initialize_booking_payment()` → `backend/apps/payments/services/paystack.py` `initialize_transaction()` |
| 8 | Settings / env loading | `backend/config/settings/base.py:520` `PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY", default="")` (python-decouple; `.env` preloaded by `manage.py`/`wsgi.py` for local runs, real env vars on the host) |
| 9 | Transaction create/status | `_reserve_initialization()` (booking row lock, reuse of one PENDING attempt, 2-min initialization lease) → `Payment` model (`backend/apps/payments/models.py`) |
| 10 | Callback / redirect | Paystack → `settings.PAYMENT_CALLBACK_URL` (default `{FRONTEND_URL}/payment-verify.html`) → `frontend/payment-verify.html` |
| 11 | Verification | `VerifyPaymentView` → `payment_service.process_verification()` → `paystack.verify_transaction()` (server-side, reference/amount/currency checked) |
| 12 | Booking/payment update | `booking_service.register_successful_payment()`; only verified `success` credits the booking; browser redirect alone never marks paid |
| 13 | Failure / retry | Gateway failure keeps the same payment reference in `RETRYABLE_ERROR` state for safe retry; duplicate initialize reuses the issued `authorization_url`; frontend `PAYMENT_NOT_CONFIGURED` branch preserves the unpaid booking and withdraws Retry |
| 14 | Tests / deploy | `backend/tests/test_payments.py`, `test_payment_config_visibility.py`, `test_guest_access*.py`; deploy: Vercel (`frontend/vercel.json` rewrite `/api/*` → Railway API) + Railway Django API (`backend/render.yaml` is an unused Render alternative) |

**Exact 503 condition** (`payment_service.initialize_booking_payment`, first statement):

```python
if not secret_key_status()[0]:
    raise PaymentNotConfiguredError()   # 503 PAYMENT_NOT_CONFIGURED
```

`secret_key_status()` (`paystack.py`) is `False` when `PAYSTACK_SECRET_KEY` is empty/whitespace,
a public key (`pk_…`), or anything not starting `sk_`. This matches Paystack's documented key
model: backend calls use the secret key (`sk_live_…` / `sk_test_…`) as a Bearer token, amounts are
in kobo, and webhooks are HMAC-SHA512 of the raw body under the same secret key.

## A. Root cause

**Deployment configuration, not a payment-logic defect — proven, not assumed:**

1. The reported request went to **`http://127.0.0.1:8000`** — the frontend's *local-development*
   fallback (`frontend/js/config.js`). That backend is a local Django process. A fresh checkout has
   **no `backend/.env` at all** (gitignored; `.env.example` ships `PAYSTACK_SECRET_KEY=` blank), so
   `PAYSTACK_SECRET_KEY` defaults to `""` and every initialization is refused with 503 by design.
2. Reproduced exactly over real HTTP locally (see E): booking `J1-20261018-8ED25231` created →
   `POST /api/payments/initialize/` → **503, 123-byte body**, identical to the incident; booking
   stayed `PENDING / UNPAID` with the full amount due; no `Payment` row was created.
3. With any `sk_…`-format key present in the same process, the identical request passes the config
   gate and reaches Paystack (a dummy key is rejected by Paystack itself → **502
   `PAYMENT_GATEWAY_ERROR`**, a different code). The 503 is therefore specifically "no usable secret
   key in the serving process", never a gateway, network, CORS, or frontend failure.
4. Production was probed read-only: `GET /api/health/ready/` → 200 `{"database":"up"}`,
   `GET /api/rooms/` → 200 with live room data, `POST /api/payments/initialize/` with an unknown
   reference → the designed 404. The Railway API and its database are up and the route is deployed.
   Whether the **Railway** service also lacks the key cannot be determined from outside (by design,
   config state is only visible to staff via `GET /api/admin/settings/` →
   `online_payments_configured`, or on the host via `check_payment_config`). If guests on the live
   site see the same message, the same missing variable on the Railway API service is the cause.

**Two genuine code defects found during the Phase Five audit (both fixed):**

- **D1 — Webhook HMAC used the unstripped key.** `verify_webhook_signature()` read
  `PAYSTACK_SECRET_KEY` raw, while every API call strips whitespace (`paystack._secret_key()`).
  A key pasted into the host dashboard with a trailing newline/space would initialize payments fine
  but fail *every* genuine Paystack webhook with 401 — paid bookings would then reconcile only via
  the browser verify path.
- **D2 — The 503 path logged nothing.** `initialize_booking_payment()` raised
  `PaymentNotConfiguredError` silently; the exception handler does not log domain errors. The only
  trace was the access-log 503, which cannot distinguish *missing* vs *public-key-in-secret-slot* vs
  *unrecognised value* — three different operator fixes.

## B. Files changed

| File | Why |
|---|---|
| `backend/apps/payments/services/paystack.py` | Added `secret_key_problem()` (value-free reason category: `missing` / `public_key_in_secret_slot` / `unrecognized_format`); `_secret_key()` now logs the category for *every* unusable-key state (previously only the unreachable public-key branch logged) |
| `backend/apps/payments/services/payment_service.py` | (1) Initialization refusal now logs `Payment initialization refused: Paystack is not configured (<category>). Set PAYSTACK_SECRET_KEY …` before raising the unchanged 503. (2) `verify_webhook_signature()` strips the stored key, matching `_secret_key()` normalisation (fixes D1) |
| `backend/tests/test_payment_config_visibility.py` | +11 tests: problem categories, refusal logging (category present, credential value never logged), unpaid-booking preservation, webhook signature/endpoint with a whitespace-padded stored key |
| `backend/tests/test_payments.py` | +3 wrapper tests: Paystack rejection, initialize timeout, verify timeout → `PAYMENT_GATEWAY_ERROR` (502), never a config error or fake success |
| `docs/PAYMENT_503_FIX_REPORT_2026-10-18.md` | This report |

No frontend files changed. No migrations. No pricing, availability, booking-rule, or inventory changes.

## C. Fix implemented

- Paystack initialization flow is unchanged in contract and sequence (booking-locked single PENDING
  attempt → server-side initialize with backend-calculated NGN amount converted exactly once via
  `_amount_to_kobo()` → validated `authorization_url`/`access_code`/reference → frontend redirect →
  server-side verification). Initialization success still never marks a booking paid.
- The config gate now emits an operator-actionable, credential-free log line at the moment of
  refusal, e.g. (from the live reproduction):
  `ERROR [apps] Payment initialization refused: Paystack is not configured (missing). Set PAYSTACK_SECRET_KEY (sk_live_… / sk_test_…) on the backend service and redeploy.`
- Webhook signature verification now succeeds for genuine Paystack deliveries even when the hosted
  variable carries surrounding whitespace; invalid signatures still return 401, and a missing key
  still fails closed.

## D. Environment configuration (exact names; values never disclosed)

| Variable | Where | Public/Secret | Required? |
|---|---|---|---|
| `PAYSTACK_SECRET_KEY` | **Railway → the API service** (`j1-hotel-management-system-production`) → Variables; locally `backend/.env` | **Secret** — backend only, never frontend/`runtime-config.js` | **Yes — this is the missing one.** `sk_live_…` for real charges, `sk_test_…` for rehearsal (do not switch modes as part of this fix) |
| `PAYSTACK_PUBLIC_KEY` | Backend (same service) | Public | Optional; unused by the current redirect checkout |
| `FRONTEND_URL` | Backend | Public | Yes (already required in production); `PAYMENT_CALLBACK_URL` defaults to `{FRONTEND_URL}/payment-verify.html` and production settings refuse to boot unless it is public HTTPS |
| `PAYMENT_CALLBACK_URL` | Backend | Public | Optional override of the callback above |
| Paystack dashboard webhook | Paystack → Settings → API Keys & Webhooks | — | Webhook URL `https://<api-host>/api/payments/webhook/`, events `charge.success` (+ `refund.*` set in `backend/.env.example`) |
| Frontend `API_BASE_URL` | Vercel-served `js/runtime-config.js` | Public | Leave `""` (same-origin; `vercel.json` rewrites `/api/*` to Railway). Do **not** hardcode any host in code |

## E. Tests (actually executed)

| Command | Result |
|---|---|
| `python manage.py test tests.test_payments tests.test_payment_config_visibility tests.test_guest_access_recovery tests.test_guest_access tests.test_guest_access_links tests.test_multi_room_quantity_regression tests.test_pending_window tests.test_payment_rbac tests.test_customer_tax_removal` (pre-fix baseline) | 130 tests OK |
| `python manage.py test tests.test_payments tests.test_payment_config_visibility` (post-fix) | 61 tests OK (14 new) |
| `python manage.py test tests` (full backend suite, post-fix) | **1509 tests OK, 15 skipped** |
| `python manage.py check` / `makemigrations --check --dry-run` | 0 issues / no changes detected |
| `node --test tests-js/` (full frontend suite) | 532 pass, 1 skipped, **1 fail — pre-existing and unrelated**: `test-pwa-dashboard-only.js` (root `manifest.webmanifest` assertion; no frontend file was changed by this fix) |
| Live local HTTP reproduction (real `runserver`, SQLite) | No key → booking 201 `PENDING/UNPAID` → initialize **503 `PAYMENT_NOT_CONFIGURED`** (123-byte body) → booking still `PENDING/UNPAID`, no Payment row; new `(missing)` log line present. Dummy `sk_test_…` key → initialize **502 `PAYMENT_GATEWAY_ERROR`** (Paystack rejected the dummy key: log `HTTP=401 … Invalid key`), proving the config gate passes when a key exists |
| Read-only production probes | health/rooms 200; initialize route answers designed 404 for unknown reference |

Scenarios from the brief already covered by the existing suite (verified passing): valid test-config
initialization; missing secret; public-key-in-secret-slot; Paystack rejection; gateway timeout;
valid `authorization_url`; NGN/kobo conversion exactly once (₦50,000 → 5,000,000 kobo); multi-room
amounts; retry after gateway failure reusing one reference/payment; duplicate initialize reusing one
gateway transaction; successful/failed/idempotent verification; amount & currency mismatch;
unknown reference (404, no leakage); duplicate webhook replay; already-paid reprocessing; unpaid
booking preserved on initialization failure; frontend `PAYMENT_NOT_CONFIGURED` handling
(`tests-js/test-payment-not-configured-ui.js`).

**Not run:** a real Paystack test-mode end-to-end payment (no test secret key available in this
environment — none was invented); Playwright PWA suite (`frontend/tests-pwa/`, needs a served
frontend + browser); production end-to-end (would require creating production bookings — prohibited).

## F. Deployment instructions (operator — the remaining production step)

1. Paystack dashboard → Settings → Developers: copy the **secret** key for the intended mode
   (`sk_live_…` charges real cards; `sk_test_…` charges nothing).
2. Railway → project `j1-hotel-management-system-production` → **the API service** → Variables →
   set `PAYSTACK_SECRET_KEY` to that value (check it is on the API service, not a worker/frontend,
   and that no `pk_…` public key is in this slot). Redeploy/restart the API service.
3. Local development instead: put the `sk_test_…` key in `backend/.env` (never commit it) and
   restart `runserver`.
4. Confirm without exposing the value, on the API host:
   `python manage.py check_payment_config --verify` →
   expect `secret key : set — LIVE` (or `TEST`), `online payments : ON`, `Paystack : key accepted`.
   In the staff dashboard, Settings → Hotel settings must no longer show the payments banner
   (`online_payments_configured: true`).
5. Confirm the Paystack dashboard webhook URL/events (Section D) and that `FRONTEND_URL` is the
   public HTTPS site so the callback is `https://<site>/payment-verify.html`.
6. Safe verification: with a **test** key in an isolated/staging environment, make a test booking,
   initialize, pay with a Paystack test card, confirm server-side verification flips the booking to
   `CONFIRMED/PAID`, and repeat the callback/retry to confirm no duplicate booking or charge.
   Booking `J1-20261018-E013B22C` was not touched; the guest can settle it at reception or retry
   online once the key is live (same booking, no duplicate).

Backend code changes above deploy with the next API redeploy; no frontend redeploy is required for
them (the log line and webhook normalisation are backend-only). No database migration is involved.

## G. Regression confirmation

Verified by the full suites above: booking creation/idempotency, room pricing snapshots, multi-room
quantity amounts, payment initialization/verification/refunds, offline front-desk payments, folio
and finance dual-write, guest-access recovery, RBAC, and the frontend booking/payment UI tests.
Behaviour **not** verified: live Paystack test-card checkout (no credentials), production
configuration state (no operator access), Playwright browser suite.

## H. Outstanding issues

1. **Operator action required:** `PAYSTACK_SECRET_KEY` must be set on the Railway API service (and/or
   local `backend/.env`) as in Section F — no code can substitute for the missing credential, and
   production payments cannot be claimed working until step F4/F6 passes.
2. The code changes are in the local working tree of this checkout; they still need to be committed
   and deployed through the normal pipeline.
3. Pre-existing unrelated frontend failure: `tests-js/test-pwa-dashboard-only.js` (root manifest) —
   untouched by this fix; recommend the frontend owner triage separately.
