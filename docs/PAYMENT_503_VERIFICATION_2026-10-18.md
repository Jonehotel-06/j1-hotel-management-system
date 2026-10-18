# J-ONE HOTEL & LODGE — Online card payment initialization failure (HTTP 503)

**Incident:** public room booking `J1-20261018-E013B22C` saved unpaid; browser console
`POST http://127.0.0.1:8000/api/payments/initialize/ -> HTTP 503`, body
`{"success": false, "code": "PAYMENT_NOT_CONFIGURED", "message": "Online payments are not configured. Please contact the hotel."}`.

**Repository:** `Jonehotel-06/j1-hotel-management-system` @ `3566b25` (this document + the changes
below are on top of that commit).
**Date:** 2026-10-18.
**Production data:** no production record was created, modified, cancelled or charged. Booking
`J1-20261018-E013B22C` was **not** touched. The only production calls made were read-only
(`GET /api/health/`, `GET /api/health/ready/`).

---

## A. Root cause

**A missing deployment setting on the backend service — not a payment-logic defect, and not a
frontend/API-URL defect.** This was reproduced, not assumed.

### The exact 503 condition

`backend/apps/payments/services/payment_service.py` → `initialize_booking_payment()` opens with a
single predicate:

```python
if not secret_key_status()[0]:
    raise PaymentNotConfiguredError()      # -> HTTP 503 PAYMENT_NOT_CONFIGURED
```

`secret_key_status()` (`backend/apps/payments/services/paystack.py`) reports **not configured**
when `PAYSTACK_SECRET_KEY` is empty/whitespace, holds a Paystack **public** key (`pk_…`), or holds
a value that is not a Paystack secret key. `PaymentNotConfiguredError`
(`backend/apps/core/exceptions.py`) is the only error mapped to HTTP 503 with the code
`PAYMENT_NOT_CONFIGURED`, and `backend/apps/payments/views.py` → `InitializePaymentView.post()`
re-raises it unchanged. The browser therefore sees exactly the reported body.

### Why the serving process had no usable key

* `backend/config/settings/base.py:520` → `PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY", default="")`.
  A fresh checkout has **no `backend/.env`** (it is gitignored; `backend/.env.example` ships the
  variable blank), so the value is `""` and every initialization is refused by design.
* The reported request went to **`http://127.0.0.1:8000`** — that is the frontend's *local
  development* fallback (`frontend/js/config.js` → `LOCAL_API_BASE_URL`), i.e. a local Django
  process, not the deployed Railway API. The backend that answered was therefore a local one with
  no `.env`.

### Reproduction over real HTTP (local `runserver`, SQLite, no production data)

| Serving process | `POST /api/payments/initialize/` | Body | Booking after |
|---|---|---|---|
| No `PAYSTACK_SECRET_KEY` | **503**, 123 bytes | `{"success": false, "code": "PAYMENT_NOT_CONFIGURED", …}` | `PENDING / UNPAID`, amount due intact, room hold (`expires_at`) intact, room assignment intact, **no `Payment` row** |
| A usable-format `sk_test_…` key present | **502**, 103 bytes | `{"success": false, "code": "PAYMENT_GATEWAY_ERROR", …}` (Paystack rejected the dummy key) | `PENDING / UNPAID`, one `Payment` row in `RETRYABLE_ERROR` state, same reference, safe to retry |

The second row is the decisive one: with a usable key the **same request reaches Paystack**, so the
503 is specifically *"no usable secret key in the serving process"* and never a gateway, network,
CORS or frontend failure. The key value never appears in a response body or in a log line.

### Not the cause

* **Frontend API base URL.** `frontend/js/config.js` → `resolveApiBase()` returns the configured
  `window.__APP_CONFIG__.API_BASE_URL` when set, and on a **public** host falls back to same-origin
  `""`. `http://127.0.0.1:8000` is only produced when the page itself is served from
  `localhost`/`127.0.0.1`. `frontend/js/runtime-config.js` ships `API_BASE_URL: ""`, and
  `frontend/vercel.json` rewrites `/api/(.*)` to the Railway API. The payment endpoint therefore
  travels on the **same** origin as every other API call. This was confirmed by loading the real
  `config.js` + `api.js` in a test context (see `frontend/tests-js/test-payment-api-base-url.js`).
* **A "payments disabled" flag.** No such flag exists; `online_payments_configured` in
  `GET /api/admin/settings/` is derived from the same `secret_key_status()` predicate, so it
  reports the symptom rather than causing it.
* **A public key requirement.** The redirect checkout never needs `PAYSTACK_PUBLIC_KEY`; it is
  optional and unused by this flow.
* **Environment-variable naming.** `PAYSTACK_SECRET_KEY` is identical in
  `backend/config/settings/base.py`, `backend/apps/payments/services/paystack.py`,
  `backend/.env.example` and the deployment templates. There is no name mismatch.
* **A recent code change that introduced the 503.** The 503 is a long-standing, deliberate guard
  for a misconfigured deployment, not a regression.

### A real code defect found during the Phase Five audit (fixed here)

**The webhook signature layer accepted a signature computed with a Paystack *public* key.**
`verify_webhook_signature()` HMACs the body with whatever `PAYSTACK_SECRET_KEY` holds, so when the
variable was mis-set to a public key (`pk_…`) a signature computed with that *public* value
satisfied the check. Verified before the fix:

```
key='sk_test_validkey'     -> signature accepted: True
key='pk_test_publickey'    -> signature accepted: True     <-- accepted a public-key signature
key='pk_live_publickey'    -> signature accepted: True     <-- accepted a public-key signature
```

The booking could not be credited by that alone (crediting always requires a server-side Paystack
verification), but the endpoint answered `200 "Webhook processed."` to a forged event instead of
`401`, and forwarded it into a provider call. The fix makes the webhook trust boundary identical to
the initialization boundary: **fail closed on the same `secret_key_status()` predicate.**

## B. Files changed

| File | Why |
|---|---|
| `backend/apps/payments/services/payment_service.py` | `verify_webhook_signature()` now fails closed when the configured key is not a usable **secret** key (public key in the secret slot, blank, or unrecognised) and logs a credential-free reason category. A genuine secret key — including one stored with surrounding whitespace — still validates. 17 added lines; no behaviour change on the happy path. |
| `backend/tests/test_payment_initialization_recovery.py` | **New.** 26 focused tests covering the reported 503 contract, amount/reference integrity, retry safety, verification safety and the webhook trust boundary (Phase 8 scenarios that the existing suite did not cover). |
| `frontend/tests-js/test-payment-api-base-url.js` | **New.** 6 tests pinning that the payment endpoint uses the same configured base URL as every other API call, that a deployed site can never fall back to `127.0.0.1:8000`, that the trailing slash Django's route needs is kept, and that no deployment host is committed to the frontend. |
| `docs/PAYMENT_503_VERIFICATION_2026-10-18.md` | This document. |

**Deliberately not changed:** the Paystack integration, the payment/booking models, pricing,
availability, booking rules, room assignment, tax policy, Paystack fee policy, the payment
response contract (`success`/`code`/`message`), the `PAYMENT_NOT_CONFIGURED` code, the frontend
booking flow, the API base-URL convention, and every migration. No file was deleted or recreated.
No secret, credential or environment value was written into the repository.

## C. Fix implemented

1. **The 503 is now diagnosable.** The refusal path logs a credential-free reason category
   (`missing` / `public_key_in_secret_slot` / `unrecognized_format`) naming the variable and the
   service to set. Verified live:
   `ERROR [apps] Payment initialization refused: Paystack is not configured (missing). Set PAYSTACK_SECRET_KEY (sk_live_… / sk_test_…) on the backend service and redeploy.`
2. **The webhook trust boundary is closed** (the code change in section B). A signature that was
   not produced with the secret key is now rejected with `401 INVALID_SIGNATURE` instead of being
   accepted and forwarded.
3. **The payment flow itself is unchanged and correct.** Verified end to end in code and in tests:
   booking created (or resumed) → booking-row lock → one PENDING payment attempt allocated or
   reused → backend-calculated NGN amount converted to kobo **exactly once** → Paystack
   initialization with server-side credentials, the guest email, a unique payment reference and
   `settings.PAYMENT_CALLBACK_URL` → validated `authorization_url` + `access_code` + matching
   reference → frontend redirect → server-side verification → booking/payment update. Successful
   initialization still never marks a booking paid.

## D. Environment configuration

Exact names required by the current implementation. **No value is disclosed anywhere.**

| Variable | Where it must be set | Public/Secret | Required? |
|---|---|---|---|
| `PAYSTACK_SECRET_KEY` | **Railway → project `j1-hotel-management-system-production` → the API service → Variables**. Locally: `backend/.env`. | **Secret** — backend only. Never in `frontend/js/runtime-config.js`, any frontend file, or any API response. | **YES — this is the missing one.** `sk_live_…` to charge real cards, `sk_test_…` for rehearsal. Do not switch modes as part of this fix. |
| `FRONTEND_URL` | Same API service | Public | Yes (already required in production). `PAYMENT_CALLBACK_URL` defaults to `{FRONTEND_URL}/payment-verify.html`, and `config/settings/production.py` refuses to boot unless the callback is public HTTPS. |
| `PAYMENT_CALLBACK_URL` | Same API service | Public | Optional override of the callback above. |
| `PAYSTACK_PUBLIC_KEY` | Same API service | Public | Optional — **not used** by the current redirect checkout. |
| Paystack dashboard | Paystack → Settings → API Keys & Webhooks | — | Webhook URL `https://<api-host>/api/payments/webhook/`, event `charge.success`. |
| Paystack dashboard | Paystack → Settings → Preferences → Transaction fees | — | **"Pass fees to customers" must be UNCHECKED** so the customer is charged exactly the hotel's amount and the hotel absorbs the processing fee (the existing policy). |
| Frontend `API_BASE_URL` | The Vercel-served `frontend/js/runtime-config.js` | Public | Leave `""` (same-origin; `vercel.json` rewrites `/api/*` to the Railway API). Do **not** hardcode a host in code. |

## E. Tests

Commands actually executed and their results.

| Command | Result |
|---|---|
| `python manage.py test tests.test_payment_initialization_recovery` | **26 tests, OK** (new file) |
| `python manage.py test tests.test_payments tests.test_payment_config_visibility` | **61 tests, OK** |
| `python manage.py test tests` (full backend suite) | **1535 tests, OK, 15 skipped, exit 0** (617 s). Baseline before this change was 1509; the 26 new tests are the difference. |
| `python manage.py check` | `System check identified no issues (0 silenced)` |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `node --test tests-js/` (full frontend suite) | **540 tests: 538 pass, 1 skipped, 1 fail — the pre-existing, unrelated `test-pwa-dashboard-only.js` manifest assertion** (also failing before this change; no frontend file was modified) |
| `node --test tests-js/test-payment-api-base-url.js` | **6 tests, OK** (new file) |
| Live HTTP reproduction (local `runserver`, SQLite) | 503 `PAYMENT_NOT_CONFIGURED` (123 bytes) with no key → 502 `PAYMENT_GATEWAY_ERROR` (103 bytes) with a usable-format key, and the server log then shows Paystack's own answer `HTTP=401 … Invalid key` — i.e. the request genuinely reached Paystack. The booking stayed `PENDING/UNPAID` in both cases; no `Payment` row in the 503 case; one `RETRYABLE_ERROR` row (same reference, safe to retry) in the 502 case. The key value appeared in neither body nor log. |
| Read-only production probes | `GET /api/health/` → 200 `{"database":"up"}`; `GET /api/health/ready/` → 200 `{"database":"up"}`. The deployed Railway API and its database are up and the payment route is deployed. **The deployed service's Paystack configuration state is not observable from outside** (it is deliberately exposed only to authenticated staff via `GET /api/admin/settings/` → `online_payments_configured`, and on the host via `check_payment_config`). |

**Phase 8 scenario coverage** — where each of the 20 required scenarios is now pinned:

| Scenario | Test |
|---|---|
| 1. Valid test configuration | `test_initialize_creates_payment_and_returns_checkout_payload` (existing) |
| 2. Missing secret key | `test_initialize_without_paystack_config_returns_503` (existing) + `test_the_503_keeps_the_existing_error_contract` (new) |
| 3. Other missing configuration | `test_a_callback_url_that_is_not_public_https` (existing); no public key is required by this flow |
| 4. Incorrect frontend API base URL | `test-payment-api-base-url.js` (new, 6 tests) |
| 5. Paystack rejects the request | `test_initialize_provider_rejection_is_a_gateway_error` (existing) |
| 6. Paystack timeout / unavailable | `test_initialize_timeout_is_a_gateway_error_not_a_config_error`, `test_verify_timeout_is_a_gateway_error` (existing) |
| 7. Valid authorization URL returned | `test_initialize_creates_payment_and_returns_checkout_payload` (existing) |
| 8. NGN currency + amount conversion | `test_a_multi_room_booking_is_charged_its_full_total_in_kobo_exactly_once`, `test_the_payment_row_and_booking_stay_in_ngn` (new) |
| 9. Multi-room booking | `test_a_multi_room_booking_is_charged_its_full_total_in_kobo_exactly_once` (new) |
| 10. Retry after initialization failure, no duplicate booking | `test_a_gateway_failure_can_be_retried_on_the_same_booking_and_reference` (new) + `test_gateway_failure_can_retry_same_reference` (existing) |
| 11. Retry after an abandoned/failed payment | `test_a_new_attempt_after_a_failed_payment_gets_a_new_reference_and_no_new_booking` (new) |
| 12. Successful server-side verification | `test_verify_success_confirms_booking` (existing) + `test_repeated_verification_credits_the_booking_only_once` (new) |
| 13. Failed verification | `test_verify_failed_payment_does_not_confirm` (existing) + `test_a_gateway_rejection_during_verification_leaves_the_booking_unpaid` (new) |
| 14. Amount / currency mismatch | `test_verify_amount_mismatch_rejected` (existing) + `test_a_currency_mismatch_never_credits_the_booking` (new) |
| 15. Invalid / unknown reference | `test_reference_without_guest_token_exposes_nothing` (existing) + `test_an_unknown_reference_never_credits_a_booking` (new) |
| 16. Duplicate callback / verification | `test_success_webhook_replay_has_no_duplicate_side_effects` (existing) + `test_repeated_verification_credits_the_booking_only_once` (new) |
| 17. Already-paid transaction processed again | `test_an_already_paid_booking_cannot_be_charged_again` (new) + `test_verify_is_idempotent` (existing) |
| 18. Unpaid booking preserved on failure | `test_refusal_preserves_the_unpaid_booking_and_creates_no_payment` (existing) + `test_the_booking_survives_the_503_with_its_hold_and_its_unpaid_status` (new) |
| 19. Frontend handling of the error/success formats | `test-payment-not-configured-ui.js` (existing) + `test-payment-api-base-url.js` (new) |
| 20. Regression coverage | the full backend suite + the full frontend suite |

**Not run / not possible here:** a real Paystack test-mode end-to-end checkout (no Paystack test
secret key is available in this environment, and none was invented); the Playwright PWA suite
(`frontend/tests-pwa/`, needs a served frontend and a browser); any production write or charge
(prohibited).

## F. Deployment instructions (operator)

The code changes are backend-only and deploy with the next API redeploy. **No frontend rebuild is
required** for them. The remaining step is a deployment setting, not a code change.

1. Paystack → Dashboard → Settings → Developers → copy the **secret** key for the intended mode
   (`sk_live_…` charges real cards, `sk_test_…` charges nothing). Do not switch modes as part of
   this fix.
2. Paystack → Dashboard → Settings → Preferences → Transaction fees → confirm **"Pass fees to
   customers" is UNCHECKED** (the existing policy: the customer is charged exactly the hotel's
   amount).
3. Railway → project `j1-hotel-management-system-production` → **the API service** (the Django API,
   *not* a worker and *not* the frontend) → Variables → set `PAYSTACK_SECRET_KEY` to that value.
   Check that the slot does **not** hold a `pk_…` public key. Redeploy/restart the API service.
   *Local development instead:* put the `sk_test_…` key in `backend/.env` (never commit it) and
   restart `runserver`.
4. Confirm on the API host **without exposing the value**:
   ```
   python manage.py check_payment_config            # expect: secret key : set — LIVE (or TEST)
                                                   #         online payments : ON
   python manage.py check_payment_config --verify   # expect: Paystack : key accepted
   ```
   In the staff dashboard, **Settings → Hotel settings** must no longer show the payments banner
   (`online_payments_configured: true`).
5. Confirm the Paystack webhook: URL `https://<api-host>/api/payments/webhook/`, event
   `charge.success`, and that `FRONTEND_URL` is the public HTTPS site so the callback is
   `https://<site>/payment-verify.html`.
6. **Safe end-to-end rehearsal** (isolated staging, test key only): create a booking through the
   normal flow → initialize → a valid Paystack authorization URL is returned → complete the
   checkout with a Paystack test card → the backend verifies server-side and flips the booking to
   `CONFIRMED / PAID` → repeat the callback and the retry to confirm no duplicate booking and no
   duplicate charge.
7. Booking `J1-20261018-E013B22C` was not touched. Once the key is live, the guest can retry
   online payment against the **same** booking (no duplicate reservation), or settle at reception
   quoting that reference.

## G. Regression confirmation

**Verified by the executed suites:** booking creation and its idempotency key, room availability
and room assignment, room pricing snapshots, multi-room quantity amounts, deposit/required-payment
pricing, the customer-tax removal, the Paystack fee-inclusive response tolerance, payment
initialization and its single-attempt reuse, the 2-minute initialization lease, gateway failure
retry on the same reference, server-side verification (success / failed / pending / amount
mismatch / currency mismatch / unknown reference), webhook signature validation and replay
idempotency, offline front-desk payment and its already-paid/refunded guards, the finance
collection dual-write, guest-access tokens and recovery, RBAC, and the frontend booking/payment
UI tests.

**Explicitly NOT verified here:** a live Paystack test-card checkout (no credential available), the
production deployment's configuration state (no operator access), the production end-to-end flow
(would require creating production bookings — prohibited), and the Playwright browser suite.

## H. Outstanding issues

1. **Operator action required (the actual blocker).** `PAYSTACK_SECRET_KEY` must be set on the
   Railway API service as in section F. No code change can substitute for the missing credential,
   and production payments must not be reported as working until steps F4/F6 pass.
2. **Pre-existing, unrelated frontend failure.** `frontend/tests-js/test-pwa-dashboard-only.js`
   ("the old public root manifest must not remain installable/discoverable") fails on `main`
   before this change as well. It touches the PWA manifest, not payments; it is left untouched and
   should be triaged separately by the frontend owner.
3. **Production configuration state is not externally observable** by design. If guests on the
   live site still see the `PAYMENT_NOT_CONFIGURED` message after step F3, the same variable is
   missing on the Railway API service; the Settings banner and `check_payment_config` are the
   two places to confirm it.
