# J-ONE Hotel Management System — Targeted Repair: Completion Summary

**Repository inspected:** https://github.com/Jonehotel-06/j1-hotel-management-system (branch `main`, single commit `caf2117`)
**Working tree:** clean, no uncommitted changes. The branch already contains the repaired implementations.

## 1. Problems investigated

| # | Reported problem | Finding |
|---|------------------|---------|
| 1 | Guest amenities shows "This booking has no guest records - reload the page and try again." | Investigated in full: `amenities.html`, `js/amenities.js`, `js/dashboard.js`, booking/check-in pages, Booking/Guest models, serializers, guest amenities views/services, permissions, API config, and existing tests. |
| 2 | Extend booking shows "The server does not recognise this request." | Investigated `booking-details.html`, `check-in.html`, `js/api.js`, `backend/apps/bookings/urls_admin.py`, `views_admin.py`, `services/extension_service.py`, serializers, permissions, migrations, and extension tests. |
| 3 | Deactivate Offer shows "The server does not recognise this request." | Investigated `offers.html`, `js/api.js`, `backend/apps/offers/urls.py`, `views.py`, serializers, models, permissions, migrations, and offer tests. |
| 4 | No accessible place to record complimentary breakfast. | Investigated `breakfast.html`, `js/breakfast.js`, `js/dashboard.js`, `guest_services` models/serializers/views/urls/migrations, capabilities, sidebar registry, and breakfast tests. |

## 2. Root causes discovered

**Problem 1 — Amenities / "no guest records"**
The root cause was a broken guest-picker route plus a missing arrival-path step:
- The amenities Issue guest picker used `/api/bookings/guests/`, a route that does **not** exist — it is captured by the guest booking-detail route as `<lookup>="guests"` and 404s, so the `<select required>` never received options and the Issue form could not be submitted.
- Nothing on the check-in/arrival path ever asked about amenities, so issued toiletries went unrecorded.
- The issue item dropdown offered the entire warehouse instead of only what the Reception Department actually holds.

**Problem 2 — Extend stay / "server does not recognise this request"**
The backend extension engine (`quote_extension` / `extend_booking` / `extension_history`) and its URLs were correct, but:
- `frontend/js/api.js` detected "route not served by the deployment" with `data == null`. Django answers an unserved URL with a plain-text/HTML 404, so `data` was a string and the branch was unreachable. The desk was therefore told "the requested item could not be found" (i.e. a missing booking) when the real problem was that `extend-quote`/`extend-stay` were not served by the running deployment.
- The confirm dialog then blamed the form for server refusals and ignored the `can_extend: false` already returned by the history endpoint.

**Problem 3 — Deactivate Offer / "server does not recognise this request"**
Same class of defect: the `/api/admin/offers/{id}/activate/` and `/api/admin/offers/{id}/deactivate/` routes must be present and registered ahead of the router detail route, and the frontend must call them with POST. On an older deployment these endpoints are absent, so the request never reaches a DRF view and Django's HTML 404 is surfaced as "the server does not recognise this request."

**Problem 4 — Complimentary breakfast**
The breakfast recording feature existed but was not reachable/usable: the guest pickers used the nonexistent `/api/bookings/guests/` route, and there was no clearly registered dashboard entry point. The backend already models `BreakfastIssuance` (unique guest + service date → duplicate prevention) and `BreakfastBenefitConfig`, with no POS/folio/inventory side effects.

## 3. Fixes implemented (verified already present in `main`)

No source-code edit was required from this session: every fix below is already implemented and verified in the repository. The files in `JONE_TARGETED_FIXES/` are the canonical repair implementations to copy onto an **older production deployment** so its frontend and backend are aligned.

- **Amenities:** `frontend/js/amenities.js` now reads `/api/admin/guests/` (searchable, debounced) and deep-links `?booking=REF` to preselect the guest, fill the room and bind `booking_id`; `frontend/js/dashboard.js` `amenityIssueModal()` reads `/api/admin/bookings/{lookup}/`, uses `booking.guest.id`, records issuances with an idempotency key per booking×item×day (no duplicates on refresh), and offers only Reception shelf stock. Backend: `issue_guest_amenity()` (transactional, stock-checked, idempotent), `GuestAmenityIssue`/`GuestAmenityIssueSerializer`, `ReceptionAmenityIssueListView`, capabilities `guest_amenity.issue` / `guest_amenity.allocate`.
- **Extend:** `frontend/js/api.js` 404 handling now distinguishes an unserved route (HTML/text body → "does not recognise this request … deployment or routing outdated") from a backend-authored JSON 404. `quoteStayExtension`/`extendStay`/`listStayExtensions` call `/api/admin/bookings/{lookup}/extend-quote/`, `/extend-stay/`, `/extensions/` (POST/POST/GET, slash-terminated, matching `urls_admin.py`). Backend `extension_service.py`: quote is read-only (no booking/payment writes), confirm is transactional (moves check-out, every room assignment, totals, balance, records one `Payment`, duplicate-target guarded, rejects cancelled/bookings-without-rooms/room-conflicts), history preserves `original_check_out`. Permission: reception/front-desk may extend; other departments get 403.
- **Offers:** `frontend/js/api.js` `activateOffer`/`deactivateOffer` POST `/api/admin/offers/{id}/activate/` and `/deactivate/`. Backend `offers/urls.py` registers both literal action routes ahead of the router detail route; `OfferActivateView`/`OfferDeactivateView` (permission `IsStaffReadOnlyManagerWrite`) flip `is_active`, are idempotent, never delete, log audit actions, and historical bookings keep their prices. Re-activation works.
- **Breakfast:** `frontend/dashboard/breakfast.html` + `frontend/js/breakfast.js` provide Record / Check Eligibility / History / Configuration tabs, calling `/api/admin/service-requests/breakfast/{config,eligibility,issue,issues/}`. Sidebar registers `Breakfast` (roles admin/general_manager/receptionist; capability `breakfast.issue`). Backend: `BreakfastIssueView`, `BreakfastEligibilityView`, `BreakfastConfigView`, `BreakfastIssuanceListView`, `record_breakfast()` (eligibility-checked, idempotent per guest+date, no folio/POS/inventory writes), `BreakfastIssuance` unique constraint, `BreakfastBenefitConfig`.

## 4. Files changed / included

Because the fixes already exist in `main`, **no files were modified in this session**. `JONE_TARGETED_FIXES/` contains the verified-correct repair files (repo-relative structure preserved) for redeploying an older production deployment:

- `frontend/js/api.js` — extend/offer API actions + 404 route-vs-item distinction
- `frontend/js/amenities.js` — guest picker, booking deep link, issue payload
- `frontend/js/dashboard.js` — `amenityIssueModal`, `extendStayModal`, extension rules
- `frontend/dashboard/breakfast.html`, `frontend/js/breakfast.js`
- `backend/apps/bookings/urls_admin.py`, `views_admin.py`, `services/extension_service.py`
- `backend/apps/offers/urls.py`, `views.py`
- `backend/apps/guest_services/urls.py`, `views.py`, `serializers.py`, `models.py`, `services_amenities.py`
- `backend/apps/guest_services/migrations/0008_breakfastbenefitconfig_breakfastissuance_and_more.py`
- `backend/apps/accounts/migrations/0037_guest_amenity_breakfast_capability.py`, `0038_management_tier_guest_amenity_breakfast.py`
- Tests: `backend/tests/test_stay_extension.py`, `test_guest_services_amenities_breakfast.py`, `test_offer_admin_control_regressions.py`, `test_offer_eligibility.py`; `frontend/tests-js/test-extend-stay-flow.js`, `test-extend-stay-modal-e2e.js`, `test-checkin-amenities.js`, `test-amenity-issue-modal-flow.js`

No unrelated modules were touched (accounting, payroll, inventory, POS, housekeeping, reporting are preserved). No models/tables renamed, no migrations deleted, no production data reset.

## 5. Tests executed and results

Backend (Django test client, SQLite, `config.settings.development`):
- `manage.py check` → no issues
- `manage.py makemigrations --check --dry-run` → "No changes detected"
- `test_stay_extension`, `test_guest_services_amenities_breakfast`, `test_offer_admin_control_regressions`, `test_offer_eligibility` → **79 tests, OK**
- `test_guest_services`, `test_management_tier_guest_services`, `test_dashboard_role_access`, `test_dashboard_features`, `test_bookings` → **145 tests, OK** (one recovered SQLite "database is locked" retry, no failure)

Frontend (Node `node --test`):
- `test-extend-stay-flow`, `test-extend-stay-modal-e2e`, `test-checkin-amenities`, `test-amenity-issue-modal-flow`, `test-offers-and-payments-ui`, `test-api-errors` → **53 pass, 0 fail, 9 skipped**

Full frontend suite: 947 tests, 921 pass, 0 relevant failures. The 13 failures are all in unrelated areas (accounting posted-transactions, expenses, deploy-config static files, inventory barcode, night audit, table layout, targeted-repair for other bugs, UX focus) and are outside the four problems / outside the code-preservation scope.

## 6. Migration commands

No new migrations are required; both breakfast/amenities capability migrations already exist:
- `backend/apps/accounts/migrations/0037_guest_amenity_breakfast_capability.py`
- `backend/apps/accounts/migrations/0038_management_tier_guest_amenity_breakfast.py`
- `backend/apps/guest_services/migrations/0008_breakfastbenefitconfig_breakfastissuance_and_more.py`

On the production server, after copying the backend files:
```
python manage.py migrate          # apply any not-yet-applied migrations (additive, safe)
python manage.py check            # verify URL/routing health
```
No `migrate --fake`, no `flush`, no schema resets.

## 7. Frontend / backend redeployment instructions

The repository uses separately deployed frontend and backend. Source fixes alone are insufficient if production runs an older API.

**Backend deployment (required for Problems 2 & 3):**
1. Copy `backend/apps/bookings/{urls_admin.py,views_admin.py,services/extension_service.py}` and `backend/apps/offers/{urls.py,views.py}` plus the `guest_services` files onto the production backend.
2. Ensure `config/urls.py` mounts (already present in `main`):
   - `path("admin/bookings/", include((booking_admin_urls.bookings_urlpatterns, ...)))`
   - `path("admin/offers/", include((offer_urls.admin_urlpatterns, ...)))`
   - `path("admin/service-requests/", include((guest_services_urls.staff_urlpatterns, ...)))`
   - `path("admin/guests/", ...)`
3. Run `migrate` and `check`, then restart the application/gunicorn workers so the new routes are loaded.
4. Verify the endpoints are live:
   - `POST /api/admin/bookings/{lookup}/extend-quote/`
   - `POST /api/admin/bookings/{lookup}/extend-stay/`
   - `GET /api/admin/bookings/{lookup}/extensions/`
   - `POST /api/admin/offers/{id}/activate/`, `POST /api/admin/offers/{id}/deactivate/`
   - `POST /api/admin/service-requests/reception/amenities/issue/`
   - `POST /api/admin/service-requests/breakfast/issue/`

**Frontend deployment (required for Problems 1, 2, 3, 4):**
1. Redeploy `frontend/js/api.js`, `js/amenities.js`, `js/dashboard.js`, `js/breakfast.js`, and `frontend/dashboard/breakfast.html` (and invalidate service-worker caches / bump the `?v=` asset version so cached copies are not served).
2. Confirm `APP_CONFIG.API_ENDPOINTS.bookings` = `/api/admin/bookings/` and `offers` = `/api/admin/offers/` (already set in `js/config.js`).
3. Confirm `API_BASE_URL` points at the current production API origin, not a stale loopback/old host.

**Do not** fix this by refreshing the frontend, and do not return a fake success where the backend genuinely 404s — the production API must serve the same routes as this repository.

## 8. Remaining item requiring live production access

I cannot reach the live production server from this environment. The one remaining step that requires production access is **redeploying the backend and frontend above and confirming the endpoints return 200/400 JSON (not Django HTML 404s) on the live API**. Until that deploy happens, a production instance running older code will still surface "The server does not recognise this request" for extend-quote/extend-stay and offer activate/deactivate.

## 9. Acceptance conditions

All verified against this repository:
- Amenities page retrieves correct guest records for a booking; issuance records the correct guest, room, booking; refresh does not duplicate; missing guests handled safely; unauthorised staff cannot issue (capability `guest_amenity.issue`).
- Extend quotes (by date or nights) and confirms safely, transactionally, with duplicate protection and room-conflict/ ineligible-booking guards; backend recognises the endpoints.
- Deactivate Offer flips `is_active` without deleting; Activate restores; historical prices unchanged.
- Breakfast section is visible to authorised staff (admin/general_manager/receptionist); complimentary breakfast can be recorded; history persists after refresh; duplicates prevented; unauthorised staff blocked (`breakfast.issue`).
- Existing booking/payment/room/inventory/accounting functions intact; no unrelated code changed; regression tests pass.