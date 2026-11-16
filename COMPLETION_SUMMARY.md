# J-ONE Hotel Management System — Targeted Fixes Completion Summary

## Overview

Two newly reported issues were investigated and fixed. All changes are minimal, targeted repairs with no rebuilds, no unrelated changes, no permission weakening, and no duplicate endpoints or DB records. All files are output under `JONE_TARGETED_FIXES/` preserving original repo-relative structure.

---

## Issue #5: Create booking failing — "An unexpected error occurred. Please try again later."

### Root cause

The error message "An unexpected error occurred. Please try again later." is the catch-all 500 response from `jone_exception_handler` in `backend/apps/core/exception_handler.py`. It is returned whenever an unhandled exception propagates out of a view.

In `create_booking()` (`backend/apps/bookings/services/booking_service.py`), several code paths can raise exceptions that are **not** DRF `APIException`s, `Http404`, `DjangoPermissionDenied`, `DjangoValidationError`, `StorageUploadError`, or `DataError` — the only exception types the handler converts to meaningful responses. Everything else falls through to the 500 catch-all.

Three unhandled exception sources were identified and fixed:

1. **`RoomType.DoesNotExist`** — `RoomType.objects.select_for_update().get(pk=candidate.pk)` (line ~440): if the room type row is deleted between `resolve_room_type()` returning the object and this `get()` call (race condition), `RoomType.DoesNotExist` is unhandled → 500.

2. **`RoomType.DoesNotExist`** — `RoomType.objects.select_for_update().get(pk=alt_type.pk)` in the cross-type relocation block (line ~500): same race condition when re-locking a substitute room's type after `find_substitute_room()` returns it.

3. **`IntegrityError`** — from `Booking.objects.create()`, `BookingRoom.objects.bulk_create()`, `GuestDiscountApplication.objects.create()`, and `RoomOccupant.objects.create()` (via `attach_room_guests()`): database integrity failures (unique constraint violations, etc.) are not `DataError` (which IS handled) — they are separate `DatabaseError` subclasses that fall through to the 500 catch-all.

4. **Uncaught exception from `log_action()`** — audit logging failure must never cause a successfully created booking to return a 500 to the user.

### Fix

`JONE_TARGETED_FIXES/backend/apps/bookings/services/booking_service.py`

- Wrapped both `RoomType.objects.select_for_update().get()` calls in `try/except RoomType.DoesNotExist` → re-raise as DRF `NotFound` (404), matching what `resolve_room_type()` already returns when the type is missing.
- Wrapped `Booking.objects.create()`, `BookingRoom.objects.bulk_create()`, `GuestDiscountApplication.objects.create()`, and the `attach_room_guests()` call in `try/except IntegrityError` → re-raise as DRF `APIException` with a meaningful user-facing message instead of a 500.
- Wrapped `log_action()` (BOOKING_CREATED audit entry) in `try/except Exception` — logging must never fail a successfully created booking.
- Added `IntegrityError` to the imports from `django.db`.

### Files changed

- `backend/apps/bookings/services/booking_service.py` — all Issue #5 fixes

### Tests

- Backend: `tests.test_bookings` — 28 tests OK
- Full backend suite: 112 tests OK (bookings, guest services, amenities/breakfast, offers, extensions)
- No migrations needed (`makemigrations --check` → no changes)

### Redeployment instructions

1. Copy `backend/apps/bookings/services/booking_service.py` from `JONE_TARGETED_FIXES/` to the live deployment path, preserving repo-relative structure.
2. Restart the application server (gunicorn/uwsgi) to pick up the change.
3. No `makemigrations`/`migrate` needed (no model changes).
4. No data backfill needed.

### Remaining issues needing live server access

The exact trigger could not be reproduced locally across ~15 scenarios (all returned proper 201/4xx). The fix makes `create_booking()` resilient to ALL known unhandled exception sources that could produce the catch-all 500. Verify on the live deployment that booking creation no longer returns "An unexpected error occurred."

---

## Issue #6: Amenities/Laundry sidebar links only display on mobile

### Root cause

In `frontend/js/dashboard.js`, `navItemAllowed()` has this logic:

```javascript
if (!canOpenPage(it.href)) return false;
if (it.capabilities && auth.state && auth.state.capabilitiesLoaded) {
    return !!(auth.hasAnyCapability && auth.hasAnyCapability(it.capabilities));
}
```

`canOpenPage()` already verified the user's role can open the target page (e.g., receptionist can open `amenities.html`, `laundry.html`). But when `capabilitiesLoaded` becomes true (after the async `loadCapabilities()` API call resolves), the capability check **replaces** the role check entirely — it returns based ONLY on capabilities. If the user lacks the specific capabilities (e.g., `guest_amenity.issue`/`guest_amenity.allocate`, or `guest_request.manage`/`guest_request.assign`), `navItemAllowed` returns `false`, hiding the sidebar link even though the role gate already passed.

**Mobile-only symptom explained:** On mobile, the sidebar is a drawer opened by user action. If the drawer is opened before `loadCapabilities()` resolves, `capabilitiesLoaded` is `false`, the capability block is skipped, and links appear based on roles alone. On desktop, the sidebar is always visible and `renderSidebar()` runs after capabilities load, so links are filtered by capabilities and disappear. This race condition makes links appear to work only on mobile.

Server-side permission enforcement is unchanged — each amenities/laundry page still checks capabilities for individual actions. The sidebar link is purely a navigation aid; the real access control happens on the server.

### Fix

`JONE_TARGETED_FIXES/frontend/js/dashboard.js`

Removed the early `return` on the capability check in `navItemAllowed()`. `canOpenPage()` (roles) remains the gate for showing the navigation link. Capabilities refine what a user can DO on a page and are enforced server-side for each action — they must not hide a navigation link that the role gate already allows. The function now falls through to entry-level role checks (`it.role`, `it.minRole`, `it.roles`) instead of returning early on capabilities alone.

### Files changed

- `frontend/js/dashboard.js` — Issue #6 fix

### Tests

- Frontend: `tests-js/test-department-page-access.js` — 24/24 pass, including the laundry access test
- Full frontend suite: 947 tests, 921 pass, 13 fail (all pre-existing and unrelated to this change), 13 skipped — **no new failures**
- Backend: unaffected by frontend change

### Redeployment instructions

1. Copy `frontend/js/dashboard.js` from `JONE_TARGETED_FIXES/` to the live deployment path, preserving repo-relative structure.
2. Clear browser cache or bump the cache-busting query parameter (`?v=1.1.29` → new value) on dashboard HTML pages that load `dashboard.js`, so clients fetch the updated file.
3. No build step required (plain JavaScript).
4. No `makemigrations`/`migrate` needed.

### Remaining issues needing live server access

Verify on the live deployment that:
- Amenities and Laundry sidebar links appear on desktop for roles with `canOpenPage` access.
- Users who lack specific capabilities can still navigate to the pages but are blocked from individual actions (issue/allocate amenities, manage/assign laundry requests) by existing server-side checks.

---

## Files in JONE_TARGETED_FIXES/

Only new/changed files are included. No unchanged files, no reports/build artefacts/secrets.

| File | Purpose |
|------|---------|
| `backend/apps/bookings/services/booking_service.py` | Issue #5: catch `RoomType.DoesNotExist` (2 locations) → `NotFound` (404); catch `IntegrityError` (4 write locations) → `APIException`; protect `log_action()` with try/except |
| `frontend/js/dashboard.js` | Issue #6: don't hide sidebar links based on capabilities alone when role check passes |

Pre-existing repair files from the four already-fixed issues are also present in this directory (unchanged).

---

## Test results

| Suite | Result |
|-------|--------|
| Backend `tests.test_bookings` | 28/28 OK |
| Backend full suite | 112/112 OK |
| Frontend `test-department-page-access.js` | 24/24 OK |
| Frontend full suite | 921/947 pass (13 pre-existing unrelated failures, no regressions) |

No migration needed. No data backfill needed. No permission weakening.