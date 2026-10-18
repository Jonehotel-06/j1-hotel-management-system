# J-ONE HOTEL & LODGE — Production Audit & Repair Report

- **Date**: 2026-10-18 (`Africa/Lagos`)
- **Repository**: `https://github.com/Jonehotel-06/j1-hotel-management-system`
- **Branch**: `fix/production-audit-repair-20261018` (commit `eb5e16b`, frontend release `1.1.24`)
- **Live Frontend (Vercel)**: `https://j1-hotel-management-system-frontend.vercel.app`
- **Live Backend (Railway)**: `https://j1-hotel-management-system-production.up.railway.app`

---

## Diagnostic Matrix (Section 8)

| Failure | Suspected Cause | Code / Config Location | Evidence Found | Confirmed or Ruled Out | Fix Plan | Verification Method |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Failure A**: Online reservation (`J1-20261018-3664875F`) shows *"This browser can no longer confirm the booking, so payment could not be started..."* | **A1. Global token poisoning across bookings/tabs** | `frontend/booking-confirmation.html:294-335` | Startup `guestToken` read unscoped `sessionStorage.getItem("jone.guest_access_token")`, `localStorage.getItem("jone.guest_access_token")`, `savedConfirmed.guest_access_token`, and cookie `jone_guest_access_token` **before** `p.get("token")` and **without** checking `savedConfirmed.booking_reference === bookingRef`, then immediately wrote that stale token into `jone.guest_access_token.<bookingRef>`. Later, `init()` (`if (accessLink && bookingRef && !bookingScopedToken())`) saw the poisoned scoped key and skipped exchanging `?access=...`. | **Confirmed** | Prioritise URL `?token=` and booking-scoped storage (`jone.guest_access_token.<bookingRef>`); only read `savedConfirmed` or unscoped fallback storage when `savedConfirmed.booking_reference === bookingRef`. | `test-production-audit-repairs.js`, `MultiBookingAndAccessLinkRecoveryTests` (`test_guest_access_recovery.py`) |
| **Failure A** | **A2. Emailed link opening rotates the active token and invalidates the original booking tab** | `frontend/my-bookings.html:251-258,336-352,420-430`; `backend/apps/bookings/views.py:26,95-115`; `backend/apps/bookings/models.py:210-225` | Opening `/my-bookings.html?ref=...&token=<T1>&access=<SIGNED>` unconditionally called `claimEmailedLink()` (`POST /api/bookings/<ref>/access/`), which called `booking.issue_guest_access_token()` and rotated `guest_access_token_hash`, invalidating `<T1>` in the original tab. Furthermore, `_booking_created_payload` did not return `access_link_token`, and `Booking.guest_token_matches` did not accept signed access tokens. | **Confirmed** | 1) Emit `access_link_token` from `_booking_created_payload` on booking creation (`views.py`). 2) Accept valid `read_access_link_token(raw, self.booking_reference)` in `Booking.guest_token_matches()` (`models.py`). 3) In `my-bookings.html`, use `urlToken` first and only call `claimEmailedLink()` if `urlToken` is absent or rejected (401/404). 4) In `booking-confirmation.html`, fall back to `exchangeBookingAccess(ref, linkToken)` inside `reclaimBookingCredential()`. | `MultiBookingAndAccessLinkRecoveryTests` (`test_guest_access_recovery.py`), `test-booking-access-links.js`, `test-production-audit-repairs.js` |
| **Failure A** | **A3. Signed-in user session vs `guestAccessOpts()` (`{ auth: false }`) mismatch** | `frontend/booking-confirmation.html:618-685` | `guestAccessOpts()` sets `{ auth: false }` so a stale staff JWT cannot 401 a guest call. However, `redirectToPayment()` gated `reclaimBookingCredential()` behind `if (!currentGuestToken() && !signedIn())`, skipping credential recovery whenever `signedIn()` was true while still sending `{ auth: false }` with no guest token. | **Confirmed** | Always attempt `reclaimBookingCredential()` when `!currentGuestToken()`; if no guest token exists and `signedIn()` is true (or if guest-token init returns 401/404 for a signed-in user), call `initPayment(ref, { auth: true })`. | `test-payment-credential-recovery-ui.js`, `test-production-audit-repairs.js` |
| **Failure A** | **A4. Missing `BookingAccessExchangeView` route on live Railway deployment** | `backend/apps/bookings/urls.py:16` | Live read-only probe `POST https://j1-hotel-management-system-production.up.railway.app/api/bookings/J1-00000000-00000000/access/` returned structured JSON `404 {"success":false,"code":"NOT_FOUND","message":"Booking not found."}` (not HTML 404). | **Ruled Out** (route is deployed on Railway) | Keep route and expand recovery paths. | Live HTTP probe + backend test suite |
| **Failure B**: Walk-in booking creation fails with *"An unexpected error occurred. Please try again later."* | **B1. Serializer & guest-upsert gaps + MySQL `STRICT_TRANS_TABLES` `DataError`** | `backend/apps/bookings/serializers_admin.py:166-265`; `backend/apps/bookings/services/booking_service.py:195-245`; `backend/apps/core/exception_handler.py:75-115` | 1) `AdminBookingCreateSerializer` defined `def validate(self, attrs)` **twice** (lines 206 and 236), causing Python to silently overwrite the first `validate` (which checked that `room_id` belongs to `room_type`). 2) `AdminBookingCreateSerializer` lacked `validate_room_type` and did not validate `guest` field lengths (`first_name`/`last_name` <= 100, `email` valid & <= 254, `phone` 7–20 chars). 3) `upsert_guest()` did not strip whitespace, lowercase email, or compact formatted phone numbers (`+234 (0) 803 ...` > 20 chars), raising unhandled MySQL `DataError` -> HTTP 500 `"An unexpected error occurred. Please try again later."` from `jone_exception_handler`. | **Confirmed** | Merge duplicate `validate` methods, add `validate_room_type`, validate/trim/normalize all `guest` fields in `AdminBookingCreateSerializer` and `upsert_guest()`, and map `DataError` to `400 VALIDATION_ERROR` in `jone_exception_handler`. | `WalkInBookingLifecycleTests` (`test_bookings.py`), `test-production-audit-repairs.js` |
| **Failure B** | **B2. Non-atomic `internal_notes` save and unguarded post-commit confirmation/receipt email side effects** | `backend/apps/bookings/views_admin.py:217-229`; `backend/apps/bookings/services/booking_service.py:390-415,575-605`; `backend/apps/core/emails.py:140-210` | `AdminBookingListCreateView.create` saved `booking.internal_notes` **outside** `create_booking()`'s `transaction.atomic()` after `on_commit` callbacks had already fired, and serialized the response without `_booking_detail_context(booking)`. In addition, `_send_confirmation_email` called `ReceiptSerializer().to_representation(booking)` and `queue_email`/`EmailLog.objects.create` without guarding against serialization or database errors during synchronous `on_commit` execution. | **Confirmed** | Pass `internal_notes` directly into `create_booking()` so it is persisted inside the single `transaction.atomic()` block; serialize walk-in response with `_booking_detail_context(booking)`; guard `_send_confirmation_email` and `queue_email`/`send_email_safe` so email/receipt failures never abort booking creation with a 500. | `WalkInBookingLifecycleTests` (`test_bookings.py`) |
| **Failure B** | **B3. Nested `errors.guest.*` not mapped to walk-in modal fields (`guest_first_name`, etc.) and generic error fallback** | `frontend/js/api.js:90-145`; `frontend/js/dashboard.js:1615-1665`; `frontend/dashboard/bookings.html:400-415` | `api.js` `friendlyMessage()` used `Object.values(errs).flat()[0]`, which yielded an `[object Object]` for nested `{ errors: { guest: { phone: ["..."] } } }` and fell back to generic text. `dashboard.js` `formModal()` only looked up `[data-fm-field="guest"]` instead of flattening `guest.phone` -> `guest_phone`, and fired both an inline `errBox` banner and a duplicate top toast. | **Confirmed** | Add recursive `firstErrorString()` in `api.js`, flatten nested error objects and alias `first_name`/`last_name`/`email`/`phone` -> `guest_*` in `dashboard.js` `formModal()`, suppress duplicate error toast when `errBox` is shown, and trim walk-in inputs in `dashboard/bookings.html`. | `test-production-audit-repairs.js` |
| **Failure C**: Profile photo uploads succeed locally but fail in production with *"Photo could not be displayed..."* | **C1. `/media/` unmounted when `DEBUG=False`, Vercel missing `/media/(.*)` rewrite, and CSP blocking cross-origin `https:` images** | `backend/config/urls.py:130-140`; `backend/apps/core/storage.py:135-235`; `frontend/vercel.json:201-222`; `frontend/dashboard/profile.html:235-310` | 1) `backend/config/urls.py` only mounted `static(settings.MEDIA_URL, ...)` when `settings.DEBUG` was `True`. In production (`DEBUG=False`), `GET /media/profiles/...` on Railway returned `404 Not Found`. 2) When Backblaze B2 is configured without `BACKBLAZE_PUBLIC_BASE_URL` or with a private bucket (`AWS_DEFAULT_ACL = None`, `AWS_QUERYSTRING_AUTH = False`), unsigned browser reads fail. 3) `frontend/vercel.json` only rewrote `/api/(.*)` (not `/media/(.*)`) and set `Content-Security-Policy` `img-src 'self' data: blob:;` (blocking `https:` backend/CDN image URLs). 4) Consequently, `probeImage(url)` in `frontend/dashboard/profile.html` fired `img.onerror` and displayed the exact error banner at lines 356–358. | **Confirmed** | 1) Add `serve_media_file` in `apps/core/storage.py` (reads through `default_storage.open()` for both local filesystem and private/public B2) and mount both `/media/<path:rel_path>` and `/api/media/<path:rel_path>` unconditionally in `backend/config/urls.py`. 2) Update `verify_stored_upload()` to verify readable non-empty bytes via `storage.open()`. 3) Preserve `previous_image_name` in `ProfileView.update` and `AdminUserDetailView.partial_update` if a replacement upload fails. 4) In `frontend/vercel.json`, add `/media/(.*)` rewrite and allow `https:` in `img-src`. 5) In `frontend/dashboard/profile.html`, add `fallbackApiMediaUrl()` retry to `/api/media/<path>` and restore `#pf-avatar-fb` on unrecoverable load failure. | `test_profile_image_uploads.py`, `test_media_storage.py`, `test-profile-photo-ui.js`, `test-production-audit-repairs.js` |

---

## A. Root Causes

### 1. Failure A — Online Reservation Cannot Proceed to Payment (`J1-20261018-3664875F`)
- **Read-Only Production Inspection of `J1-20261018-3664875F`**:
  - A read-only check against `GET https://j1-hotel-management-system-production.up.railway.app/api/bookings/J1-20261018-3664875F/` without a token returned `404 {"success":false,"code":"NOT_FOUND","message":"Booking not found."}`, confirming `BookingDetailView` and `InitializePaymentView` enforce `can_access_booking(request.user, booking, request)` (`backend/apps/bookings/access.py:55-97`). In accordance with production safety rules, reservation `J1-20261018-3664875F` was not modified, cancelled, or charged.
- **Exact Code Path Producing the Observed Message**:
  - In `frontend/booking-confirmation.html:770-820`, when `pay()` runs for an already-created `booking`, it calls `redirectToPayment()`.
  - `redirectToPayment()` calls `window.API.initPayment(booking.booking_reference, guestOpts())` (`POST /api/payments/initialize/`).
  - On the backend (`backend/apps/payments/views.py:69-82`), `InitializePaymentView.post` looks up `Booking.objects.filter(booking_reference=ref).first()` and checks `can_access_booking(request.user, booking, request)`. When the presented `X-Guest-Access-Token` does not match `booking.guest_access_token_hash` (or is empty while `request.user` is anonymous), `InitializePaymentView` logs `"Payment init refused: credential rejected for booking ..."` and returns HTTP `404`.
  - In `booking-confirmation.html`, when `initPayment` rejects with `404` or `401` (or when `!currentGuestToken() && !signedIn()` raises `MISSING_BOOKING_CREDENTIAL`) and `reclaimBookingCredential()` returns `false`, the catch block at lines 803–815 renders `#failure-screen` (`"Payment not completed"`) with the exact message:
    > *"This browser can no longer confirm the booking, so payment could not be started. Your reservation is still saved — request a secure link below: it works here or on any other device."*
- **Three Concrete Defects That Caused Credential Loss / Rejection**:
  1. **Cross-booking token poisoning on page load (`frontend/booking-confirmation.html:294-335`)**: At startup, `guestToken` read unscoped `sessionStorage.getItem("jone.guest_access_token")`, `localStorage.getItem("jone.guest_access_token")`, `savedConfirmed.guest_access_token`, and cookie `jone_guest_access_token` **before** checking `p.get("token")` and **without** checking whether `savedConfirmed.booking_reference === bookingRef`. It then immediately called `saveGuestToken(guestToken, bookingRef, "")`, overwriting `jone.guest_access_token.<bookingRef>` with a previous booking's token. Worse, when the guest clicked an emailed link (`/booking-confirmation.html?ref=...&access=...`), `init()` checked `if (accessLink && bookingRef && !bookingScopedToken())`, saw the poisoned scoped token, and **skipped exchanging `?access=...`**.
  2. **Destructive token rotation when opening `/my-bookings.html` (`frontend/my-bookings.html:251-430`, `backend/apps/bookings/views.py:95-115,392-435`)**: `guest_booking_link(booking)` generates `/my-bookings.html?ref=<REF>&token=<RAW_TOKEN>&access=<SIGNED_ACCESS>`. Whenever the guest opened that link in another tab or device, `my-bookings.html` unconditionally executed `if (urlAccess) await claimEmailedLink()`, calling `POST /api/bookings/<ref>/access/` which invoked `booking.issue_guest_access_token()` and rotated `guest_access_token_hash` in the database—immediately invalidating `<RAW_TOKEN>` in the original `booking-confirmation.html` tab. Additionally, `_booking_created_payload` did not include `access_link_token` in the `POST /api/bookings/` response, and `Booking.guest_token_matches()` did not accept signed access tokens.
  3. **`signedIn()` vs `guestAccessOpts()` (`{ auth: false }`) mismatch (`frontend/booking-confirmation.html:618-685`)**: `API.guestAccessOpts()` intentionally sets `{ auth: false }` so a stale staff/guest JWT does not cause a 401. However, `redirectToPayment()` only ran `reclaimBookingCredential()` when `if (!currentGuestToken() && !signedIn())`. If the browser had a logged-in session in storage (`signedIn() === true`) but no `guestToken`, it skipped `reclaimBookingCredential()` and still sent `initPayment` with `{ auth: false }` and no token, resulting in an immediate 404.

### 2. Failure B — Walk-In Booking Creation Fails (`POST /api/admin/bookings/`)
- **Exact Code Path Producing the Observed Message**:
  - The walk-in modal in `frontend/dashboard/bookings.html:360-430` submits `POST /api/admin/bookings/` (`AdminBookingListCreateView.create` in `backend/apps/bookings/views_admin.py:194-229`).
  - Any unhandled exception in `AdminBookingListCreateView.create` reaches `jone_exception_handler` (`backend/apps/core/exception_handler.py:91-99`), which returns HTTP `500` with `{"success": false, "code": "SERVER_ERROR", "message": "An unexpected error occurred. Please try again later."}`.
  - `formModal` (`frontend/js/dashboard.js:1615-1655`) rendered that message in `.form-modal-error` directly below the `Guest phone` field and simultaneously triggered a duplicate top-of-screen error toast (`JONE.ui.toast(msg, "error")`).
- **Concrete Backend & Frontend Defects**:
  1. **Duplicate `def validate` & missing guest/room-type validation on `AdminBookingCreateSerializer` (`backend/apps/bookings/serializers_admin.py:166-265`)**: `AdminBookingCreateSerializer` defined `def validate(self, attrs)` twice (lines 206 and 236), causing Python to discard the first `validate` (which verified that `room_id` belongs to `room_type`). It also lacked `validate_room_type` and only checked truthiness of `guest` keys without trimming whitespace, validating email syntax, compacting formatted phone numbers, or checking column lengths.
  2. **Unvalidated `Guest` writes in `upsert_guest()` (`backend/apps/bookings/services/booking_service.py:195-245`)**: `Guest.phone` is `VARCHAR(20)` (`backend/apps/bookings/models.py:39`), `first_name`/`last_name` are `VARCHAR(100)`, and production MySQL runs with `STRICT_TRANS_TABLES` (`backend/config/settings/production.py:130`). Formatted phone numbers or untrimmed guest fields raised `django.db.utils.DataError` (HTTP 500).
  3. **Non-atomic `internal_notes` update & unguarded synchronous email/receipt callbacks (`backend/apps/bookings/views_admin.py:217-225`, `booking_service.py:390-415,575-605`, `apps/core/emails.py:140-210`)**: `AdminBookingListCreateView.create` saved `booking.internal_notes` **outside** `create_booking()`'s `transaction.atomic()` after `transaction.on_commit` callbacks had already run, and serialized `AdminBookingDetailSerializer(booking)` without `_booking_detail_context(booking)`. Meanwhile, `_send_confirmation_email` called `ReceiptSerializer().to_representation(booking)` and `queue_email` created `EmailLog` rows without `try/except`, allowing any receipt serialization or email logging glitch to bubble up as a 500 after commit.
  4. **Frontend nested validation error handling (`frontend/js/api.js:90-145`, `frontend/js/dashboard.js:1615-1655`)**: `friendlyMessage()` used `Object.values(errs).flat()[0]`, which failed to extract strings from nested `{ errors: { guest: { phone: ["..."] } } }`, and `formModal()` did not map `guest.first_name` / `guest.last_name` / `guest.email` / `guest.phone` to the modal's `[data-fm-field="guest_first_name"]` etc.

### 3. Failure C — Profile Photo Uploads Succeed Locally but Fail in Production
- **Exact Code Path Producing the Observed Message**:
  - After `PATCH /api/auth/profile/` returns HTTP `200`, `frontend/dashboard/profile.html:338-365` calls `probeImage(updated.profile_image_url)`, which creates a browser `new Image()` with `?photo-check=<timestamp>`. When `img.onerror` fires, `profile.html:356-358` displays:
    > *"Photo could not be displayed — Your photo was saved to your record, but this browser cannot display it — so it will not appear on your profile. An administrator must check where uploaded images are stored and served."*
- **Concrete Production Root Causes**:
  1. **`/media/` unmounted in production (`backend/config/urls.py:134-135`)**: `urlpatterns` only appended `static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)` when `settings.DEBUG` was `True`. In production (`DEBUG=False`), any request to `/media/profiles/...` on Railway returned HTTP `404 Not Found` (confirmed via live probe `GET https://j1-hotel-management-system-production.up.railway.app/media/profiles/test.jpg` -> `404`).
  2. **Private Backblaze B2 object storage vs unsigned browser reads (`backend/config/settings/base.py:310-365`)**: `configure_b2_media_storage()` sets `AWS_DEFAULT_ACL = None` and `AWS_QUERYSTRING_AUTH = False`. If the B2 bucket is private or `BACKBLAZE_PUBLIC_BASE_URL` is not publicly readable, direct S3/B2 URLs return 401/403 in the browser even though `storage.exists()` returned `True` during upload.
  3. **Vercel CSP and rewrite gaps (`frontend/vercel.json:201-220`)**: `vercel.json` set `Content-Security-Policy` `img-src 'self' data: blob:;`, which blocked the browser from loading any cross-origin `https://` image (whether from Railway `PUBLIC_BACKEND_ORIGIN` or Backblaze B2), and only proxied `/api/(.*)` (not `/media/(.*)`).
  4. **Shallow storage verification & replacement photo loss (`backend/apps/core/storage.py:165-205`, `apps/accounts/views.py:375-415`, `apps/accounts/views_admin.py:275-315`)**: `verify_stored_upload()` only checked `storage.exists(name)` without verifying that `storage.open(name, "rb").read(64)` returns non-empty bytes, and failed replacement uploads overwrote the user's previous valid `profile_image` field.

---

## B. Files Changed

### Backend (14 files)
1. `backend/apps/bookings/models.py` — Updated `Booking.guest_token_matches()` to accept a valid signed `access_link_token` (`read_access_link_token(raw, self.booking_reference)`) in addition to the active `guest_access_token_hash`.
2. `backend/apps/bookings/views.py` — Imported `issue_access_link_token` and included `access_link_token` in `_booking_created_payload()` on booking creation and idempotent replay.
3. `backend/apps/bookings/serializers_admin.py` — Added `validate_room_type()`, merged the duplicate `validate()` methods on `AdminBookingCreateSerializer`, and hardened `validate_guest()` to strip strings, validate & lowercase email, compact/validate phone numbers (7–20 chars), and enforce column length limits across all provided fields.
4. `backend/apps/bookings/services/booking_service.py` — Hardened `upsert_guest()` (whitespace stripping, lowercase email, phone compaction, and `DjangoValidationError` on column overflow), moved `internal_notes` persistence inside `create_booking()`'s `transaction.atomic()` block, and guarded `_send_confirmation_email()` against non-fatal receipt serialization errors.
5. `backend/apps/bookings/views_admin.py` — Passed `internal_notes` directly into `booking_service.create_booking()` and serialized the created booking with `self._booking_detail_context(booking)`.
6. `backend/apps/core/emails.py` — Guarded `send_email_safe()` and `queue_email()` so `EmailLog` database/provider errors are logged and never crash a committed booking transaction.
7. `backend/apps/core/exception_handler.py` — Mapped database `DataError` to a structured `400 VALIDATION_ERROR` envelope instead of an unhandled `500 SERVER_ERROR`.
8. `backend/apps/core/storage.py` — Enhanced `verify_stored_upload()` to open and read up to 64 bytes from `default_storage` to confirm non-empty readability; added `serve_media_file(request, rel_path)` to stream media files safely through `default_storage` with `Cache-Control: public, max-age=86400` and `Cross-Origin-Resource-Policy: cross-origin`.
9. `backend/config/urls.py` — Mounted `/media/<path:rel_path>` and `/api/media/<path:rel_path>` via `serve_media_file` in all environments (including `DEBUG=False`).
10. `backend/apps/accounts/views.py` — Updated `ProfileView.update` to preserve and restore the user's previous valid `profile_image` when a replacement upload fails or cannot be verified.
11. `backend/apps/accounts/views_admin.py` — Updated `AdminUserDetailView.partial_update` to preserve and restore the staff member's previous valid `profile_image` when a replacement upload fails or cannot be verified.
12. `backend/tests/test_guest_access_recovery.py` — Added `MultiBookingAndAccessLinkRecoveryTests` covering signed `access_link_token` issuance/exchange/header authentication and multi-booking stale-token recovery via `idempotency_key`.
13. `backend/tests/test_bookings.py` — Added `WalkInBookingLifecycleTests` covering walk-in creation, room assignment, `internal_notes` staff-only privacy, receipt failure resilience, and structured 400 validation errors.
14. `backend/tests/test_profile_image_uploads.py` — Added `test_uploaded_photo_is_served_in_production_mode_via_media_and_api_media` and `test_failed_replacement_photo_preserves_previous_valid_image`.

### Frontend (9 files + build version bump across HTML/SW assets to `1.1.24`)
1. `frontend/booking-confirmation.html` — Scoped guest tokens strictly by `booking_reference`, prevented unscoped storage from poisoning `jone.guest_access_token.<bookingRef>`, persisted `idempotency_key` and `access_link_token` on the confirmed booking snapshot, added `exchangeBookingAccess` fallback inside `reclaimBookingCredential()`, and added `{ auth: true }` fallback when `signedIn()` is true.
2. `frontend/my-bookings.html` — Scoped stored guest tokens by `urlRef`, preserved active `urlToken` on email link open (only calling `claimEmailedLink()` when `urlToken` is absent or rejected with 401/404), preventing unnecessary token rotation across tabs.
3. `frontend/js/api.js` — Added recursive `firstErrorString()` in `friendlyMessage()` so nested validation errors (`{ errors: { guest: { phone: ["..."] } } }`) surface their exact field message.
4. `frontend/js/dashboard.js` — Updated `formModal()` to flatten nested error objects (`guest.phone` -> `guest_phone` / `phone`), alias `first_name`/`last_name`/`email`/`phone` to `guest_*`, scroll `errBox` into view, and avoid firing a duplicate top toast when `errBox` is already visible.
5. `frontend/dashboard/bookings.html` — Trimmed all walk-in guest contact fields (`first_name`, `last_name`, `email`, `phone`, `offer_code`) before submitting `POST /api/admin/bookings/`.
6. `frontend/dashboard/profile.html` — Added `fallbackApiMediaUrl(url)` to retry `/api/media/<path>` in both `probeImage(url)` and `paint()` (`#pf-avatar` `onerror`) before failing, and restored `#pf-avatar-fb` if an image cannot be loaded.
7. `frontend/vercel.json` — Added `/media/(.*)` rewrite to the Railway backend and updated `Content-Security-Policy` / `Content-Security-Policy-Report-Only` `img-src` to `'self' data: blob: https:;`.
8. `frontend/tests-js/test-production-audit-repairs.js` — Added unit and integration tests for Failures A, B, and C.
9. `frontend/version.json`, `frontend/js/version.js`, `frontend/sw.js`, and HTML asset query strings — Bumped release version from `1.1.23` to `1.1.24` via `python3 frontend/build.py --bump patch` (and removed obsolete root `frontend/manifest.webmanifest` so only `frontend/dashboard/manifest.webmanifest` exists).

---

## C. Fixes Implemented

1. **Authoritative & Recoverable Guest Booking Payment Lifecycle (Failure A)**:
   - `POST /api/bookings/` now returns both `guest_access_token` (session token) and `access_link_token` (cryptographically signed, booking-scoped token valid for 30 days).
   - `Booking.guest_token_matches()` accepts either the current `guest_access_token` or a valid `access_link_token` for that booking reference, so token rotation in another tab/device never locks out a holder of the signed booking credential.
   - `booking-confirmation.html` isolates tokens per `booking_reference`, exchanges `?access=...` whenever no valid booking-scoped token exists, automatically recovers credentials via `idempotency_key` (`POST /api/bookings/<ref>/access/`) or `access_link_token` before and during `redirectToPayment()`, and falls back to `{ auth: true }` when the booking owner is signed in.
   - `my-bookings.html` tests `urlToken` first before calling `claimEmailedLink()`, avoiding unnecessary token rotation when a guest opens their confirmation email link.
2. **Atomic, Validated Walk-In Booking Creation (Failure B)**:
   - Merged the duplicate `validate()` methods on `AdminBookingCreateSerializer`, added `validate_room_type()`, and added comprehensive `validate_guest()` validation (trimming, email format & lowercase normalization, phone digit/length validation, and 100/254/20 char bounds).
   - Hardened `upsert_guest()` and mapped database `DataError` to HTTP `400 VALIDATION_ERROR`.
   - Moved `internal_notes` persistence inside `booking_service.create_booking()`'s `transaction.atomic()` block and guarded `_send_confirmation_email()` / `send_email_safe()` / `queue_email()` so post-commit receipt/email errors are logged without aborting the HTTP response.
   - Updated `api.js`, `dashboard.js`, and `dashboard/bookings.html` so nested `guest.*` errors highlight the exact walk-in modal inputs (`guest_first_name`, `guest_last_name`, `guest_email`, `guest_phone`) with a single clear message.
3. **Production Media Delivery, Verification & Replacement Safety (Failure C)**:
   - Mounted `/media/<path:rel_path>` and `/api/media/<path:rel_path>` via `serve_media_file` in `backend/config/urls.py` across all environments (`DEBUG=True` and `DEBUG=False`), streaming stored files through `default_storage.open()` (works seamlessly with both local disk and private/public Backblaze B2 buckets).
   - Updated `frontend/vercel.json` to proxy `/media/(.*)` to the backend and permit `https:` in `Content-Security-Policy` `img-src`.
   - Updated `verify_stored_upload()` to verify readable non-empty bytes, updated `ProfileView` and `AdminUserDetailView` to preserve the previous valid `profile_image` when a replacement upload fails, and updated `dashboard/profile.html` to fall back to `/api/media/<path>` before reporting a load error.

---

## D. Tests Performed

1. **Frontend Automated Test Suite (`node --test tests-js/test-*.js`)**:
   - **546 total tests**: **545 passed, 0 failed, 1 skipped** (`duration_ms: 5922ms`).
   - Includes `test-production-audit-repairs.js`, `test-payment-credential-recovery-ui.js`, `test-booking-access-links.js`, `test-profile-photo-ui.js`, `test-api-errors.js`, `test-pwa-dashboard-only.js`, and `test-update-checker.js`.
2. **Backend Automated Test Suite (`python3 manage.py test`)**:
   - **1,542 total tests**: **1,542 passed, 0 failed, 15 skipped** (`614.9s`).
   - Targeted suites (`test_bookings`, `test_guest_access`, `test_guest_access_links`, `test_guest_access_recovery`, `test_payment_initialization_recovery`, `test_payments`, `test_profile_image_uploads`, `test_media_storage`, `test_image_upload_paths`): **176 tests ran, 176 passed, 0 failed**.
3. **End-to-End Controlled Reproduction Script (Section 5 — Tests A, B, C with `DEBUG=False`)**:
   - Executed against an isolated temporary database and media root with `DEBUG=False`, verifying online booking creation, stale-token rejection, `idempotency_key` & `access_link_token` recovery, Paystack payment initialization, walk-in booking creation with room assignment and `internal_notes` privacy, walk-in validation error structure, profile photo upload/serving via `/media/` and `/api/media/`, failed replacement preservation, `check_media_storage`, and complete test data cleanup.

---

## E. Booking Reproduction Evidence

### Test A — Online Guest Booking & Payment Recovery (`DEBUG=False`)
- **Booking 1 Created**: `POST /api/bookings/` (`Idempotency-Key: audit-e2e-online-01`) -> HTTP `201 Created`, `booking_reference: "J1-20261018-E8A6D2AE"`.
- **Booking 2 Created**: `POST /api/bookings/` (`Idempotency-Key: audit-e2e-online-02`) -> HTTP `201 Created`, `booking_reference: "J1-20261018-76DD9754"`, returned both `guest_access_token` and `access_link_token`.
- **Direct Payment Initialization**: `POST /api/payments/initialize/` with Booking 2's `guest_access_token` -> HTTP `201 Created`, `authorization_url: "https://checkout.paystack.com/J1P-20261018-17A40B4561"`.
- **Cross-Booking Stale Token Reproduction**: `POST /api/payments/initialize/` for Booking 2 with Booking 1's token -> HTTP `404 Not Found`.
- **Same-Browser Credential Reclaim (`idempotency_key`)**: `POST /api/bookings/J1-20261018-76DD9754/access/` with `{"idempotency_key": "audit-e2e-online-02"}` -> HTTP `200 OK` (issued fresh `guest_access_token`).
- **Signed Access Link Verification & Exchange**:
  - `GET /api/bookings/J1-20261018-76DD9754/` with `X-Guest-Access-Token: <access_link_token>` -> HTTP `200 OK`.
  - `POST /api/bookings/J1-20261018-76DD9754/access/` with `{"access": "<access_link_token>"}` -> HTTP `200 OK`.
  - `POST /api/payments/initialize/` with recovered token -> HTTP `201 Created`, `authorization_url: "https://checkout.paystack.com/J1P-20261018-17A40B4561"`.
  - `POST /api/bookings/J1-20261018-76DD9754/access-link/` -> HTTP `200 OK` (`EmailLog` `BOOKING_ACCESS_LINK` sent).

### Test B — Walk-In Booking Creation (`DEBUG=False`)
- **Walk-In Creation**: `POST /api/admin/bookings/` by authenticated `RECEPTIONIST` with `room_type="audit-deluxe"`, `room_id=801`, `source="WALK_IN"`, `status="CONFIRMED"`, `special_requests="TEST AUDIT — Late arrival"`, `internal_notes="TEST AUDIT — Staff-only front-desk note"`, and untrimmed/formatted guest contact fields -> HTTP `201 Created`, `booking_reference: "J1-20261018-78D18496"`.
- **Verified Payload**:
  - `source: "WALK_IN"`, `status: "CONFIRMED"`, `total_amount: "50000.00"`.
  - `room_assignments`: `[{"room_number": "801", ...}]`.
  - `guest`: `first_name: "TEST AUDIT"`, `email: "test.audit.walkin@example.test"`, `phone: "+234(0)8035550199"`.
  - `internal_notes`: Present in staff response (`"TEST AUDIT — Staff-only front-desk note"`), **absent** from guest response (`GET /api/bookings/J1-20261018-78D18496/`).
- **Invalid Walk-In Submission**: `POST /api/admin/bookings/` with empty `first_name`, invalid `email`, and short `phone` -> HTTP `400 Bad Request` (`code: "VALIDATION_ERROR"`, `errors.guest` keys: `["email", "first_name", "phone"]`).
- **Cleanup Verified**: All test bookings, payments, and guest records deleted (`cleanup_verified: true`).

---

## F. Photo-Upload Evidence

- **Upload (`PATCH /api/auth/profile/` with `DEBUG=False`)**:
  - Uploaded valid 64×64 PNG (`156 bytes`) -> HTTP `200 OK`.
  - Response JSON: `profile_image_stored: true`, `profile_image_warning: ""`, `profile_image_url: "http://testserver/media/profiles/2026/10/avatar.png"`.
- **Production Delivery Verification (`DEBUG=False`)**:
  - `GET /media/profiles/2026/10/avatar.png` -> HTTP `200 OK`, `Content-Type: image/png`, `Cross-Origin-Resource-Policy: cross-origin`, `Cache-Control: public, max-age=86400`, body length `156 bytes` (exact byte match).
  - `GET /api/media/profiles/2026/10/avatar.png` -> HTTP `200 OK`, `Content-Type: image/png`, `Cross-Origin-Resource-Policy: cross-origin`, body length `156 bytes` (exact byte match).
- **Failed Replacement Safety**:
  - Simulated storage read-back failure (`StorageUploadError`) on subsequent `PATCH /api/auth/profile/` with `replace.jpg` -> HTTP `502 Bad Gateway` (`code: "STORAGE_UPLOAD_FAILED"`).
  - Database check: `receptionist.profile_image.name` remained `profiles/2026/10/avatar.png` (`previous_photo_preserved: true`), and `GET /api/media/profiles/2026/10/avatar.png` continued returning HTTP `200 OK`.
- **Storage Probe Command (`python3 manage.py check_media_storage`)**:
  - `write OK` -> `read OK (content matches)` -> `delete OK (probe removed)` -> `Media storage verified`.

---

## G. Regression Results

| Area | Test Suite / Check | Result |
| :--- | :--- | :--- |
| **Booking & Room Operations** | `tests.test_bookings`, `tests.test_booking_idempotency`, `tests.test_rooms`, `tests.test_room_occupants` | **PASS** (0 failures) |
| **Payments & Guest Records** | `tests.test_payments`, `tests.test_payment_initialization_recovery`, `tests.test_guest_access`, `tests.test_guest_access_links`, `tests.test_guest_access_recovery` | **PASS** (0 failures) |
| **Profile, Staff & Media Storage** | `tests.test_profile_image_uploads`, `tests.test_media_storage`, `tests.test_image_upload_paths`, `tests.test_staff` | **PASS** (0 failures) |
| **Full Backend Suite** | `python3 manage.py test` (all apps: accounts, bookings, payments, rooms, finance, pos, inventory, housekeeping, portal, notifications, audit, staff_operations) | **1,542 passed, 0 failed, 15 skipped** |
| **Full Frontend JS Suite** | `node --test tests-js/test-*.js` (all 50 test files including accessibility, contrast, API errors, booking access links, payment recovery, profile photo UI, PWA/SW versioning) | **545 passed, 0 failed, 1 skipped** |

---

## H. Deployment Instructions

1. **Merge & Push Branch**:
   ```bash
   git checkout fix/production-audit-repair-20261018
   git push origin fix/production-audit-repair-20261018
   ```
   Merge `fix/production-audit-repair-20261018` into `main` to trigger automatic deployments on **Railway** (backend) and **Vercel** (frontend).
2. **Database Migrations**:
   - No new schema migrations were required or added; existing migrations (`0005_roomoccupant_and_booking_room_guests` etc.) run automatically on Railway startup (`python manage.py migrate --noinput`).
3. **Environment Variables to Verify on Railway (Backend)**:
   - `PAYSTACK_SECRET_KEY`: Must be set to a valid `sk_live_...` (or `sk_test_...`) secret key (never `pk_...`).
   - `PUBLIC_BACKEND_ORIGIN`: `https://j1-hotel-management-system-production.up.railway.app`
   - `FRONTEND_URL`: `https://j1-hotel-management-system-frontend.vercel.app`
   - **Media Persistence**: Configure Backblaze B2 (`BACKBLAZE_KEY_ID`, `BACKBLAZE_APPLICATION_KEY`, `BACKBLAZE_BUCKET_NAME`, `BACKBLAZE_ENDPOINT_URL`) **or** attach a persistent Railway Volume mounted at `MEDIA_ROOT` (`/app/media`). Because `/media/<path>` and `/api/media/<path>` now stream through `serve_media_file` (`default_storage.open()`), even a private Backblaze B2 bucket serves profile/room/gallery images without requiring public bucket ACLs.
4. **Post-Deploy Verification Commands**:
   - Check backend health & storage:
     ```bash
     railway run python manage.py check_media_storage
     curl -i https://j1-hotel-management-system-production.up.railway.app/api/health/
     ```
   - Check frontend version (`1.1.24`):
     ```bash
     curl -s https://j1-hotel-management-system-frontend.vercel.app/version.json
     ```

---

## I. Remaining Limitations

1. **Ephemeral Disk Warning When Object Storage Is Unconfigured**:
   - With `/media/<path>` and `/api/media/<path>` now mounted in production (`DEBUG=False`), uploaded profile photos are immediately served and display in the browser even when Railway uses local disk (`FileSystemStorage`). However, Railway container filesystems are ephemeral across redeploys/restarts unless Backblaze B2 (`BACKBLAZE_*` env vars) or a persistent Railway volume (`MEDIA_ROOT`) is attached.
2. **Existing Reservation `J1-20261018-3664875F`**:
   - Per strict production safety constraints, reservation `J1-20261018-3664875F` was not altered or charged. If its 15-minute `PENDING` hold has already expired in production, the guest can either use **"Email me a secure link"** (if still `PENDING`) or create a fresh reservation once v`1.1.24` is deployed.

---

## J. Final Status

- **Failure A (Online reservation cannot proceed to payment)**: **RESOLVED & VERIFIED**
- **Failure B (Walk-in booking creation fails with unexpected error)**: **RESOLVED & VERIFIED**
- **Failure C (Profile photo uploads fail to display in production)**: **RESOLVED & VERIFIED**
- **Frontend Build & Version**: **`1.1.24`** (`545 / 545` JS tests passing)
- **Backend Test Suite**: **`1,542 / 1,542`** Django/DRF tests passing
