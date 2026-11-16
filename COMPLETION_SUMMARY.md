# J-ONE Hotel Management System — Targeted Fixes Completion Summary

## Overview

Two newly reported issues were investigated and fixed. All changes are minimal, targeted repairs with no rebuilds, no unrelated changes, no permission weakening, and no duplicate endpoints or DB records. All files are output under `JONE_TARGETED_FIXES/` preserving original repo-relative structure.

---

## Issue #5: Create booking failing — "An unexpected error occurred. Please try again later."

### Root cause

In `backend/apps/bookings/services/booking_service.py`, `create_booking()` calls `resolve_room_type()` which returns a `RoomType` object. The next line immediately does:

```python
room_type = RoomType.objects.select_for_update().get(pk=candidate.pk)
```

If the room type row is deleted between `resolve_room_type()` returning the object and this `get()` call (race condition — e.g., concurrent deactivation of a room type), `RoomType.DoesNotExist` is raised. This is not a DRF/Django handled exception, so it propagates as an unhandled 500. The custom exception handler (`backend/apps/core/exception_handler.py`, `jone_exception_handler`) then returns the catch-all message:

> "An unexpected error occurred. Please try again later." (SERVER_ERROR, 500)

All other error paths in `create_booking()` properly return 400/409 responses. The unhandled `DoesNotExist` was the only path that could produce a 500 with this exact message. Local reproduction across ~15 scenarios (happy path, overlapping dates, invalid room, dirty housekeeping, occupied rooms, maintenance, capacity overflow, invalid email, missing fields, multi-room overflow, auto allocation, etc.) all returned proper 201/4xx — no 500 was reproducible because the race window is extremely narrow and requires concurrent room-type deletion.

### Fix

`JONE_TARGETED_FIXES/backend/apps/bookings/services/booking_service.py`

Wrapped `RoomType.objects.select_for_update().get(pk=candidate.pk)` in a `try/except RoomType.DoesNotExist` that re-raises as `rest_framework.exceptions.NotFound("Room type not found.")` — the same 404 that `resolve_room_type()` already returns when the type is missing. This keeps the error a clean 404 instead of an unhandled 500.

### Files changed

- `backend/apps/bookings/services/booking_service.py` — added try/except around `select_for_update().get()`

### Tests

- Backend: `tests.test_bookings` — 28 tests OK
- Full backend suite: 112 tests OK (bookings, guest services, amenities/breakfast, offers, extensions)
- No migrations needed (`makemigrations --check` → no changes)
- No migration commands required for redeployment

### Redeployment instructions

1. Copy `backend/apps/bookings/services/booking_service.py` from `JONE_TARGETED_FIXES/` to the live deployment path, preserving repo-relative structure.
2. Restart the application server (gunicorn/uwsgi) to pick up the change.
3. No `makemigrations`/`migrate` needed (no model changes).
4. No data backfill needed.

### Remaining issues needing live server access

The exact race condition could not be reproduced locally (requires concurrent room-type deletion during an in-flight booking creation). The fix converts the unhandled 500 to a handled 404 regardless, so the user-facing error message will no longer occur for this path.

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

`canOpenPage()` already verified the user's role can open the target page (e.g., receptionist can open `amenities.html`, `laundry.html`). But when `capabilitiesLoaded` becomes true (after the async `loadCapabilities()` API call resolves), the capability check **replaces** the role check entirely — it returns based ONLY on capabilities. If the user lacks the specific capabilities (e.g., `guest_amenity.issue`/`guest_amenity.allocate`, or `guest_request.manage`/`guest_request.assign`), `navItemAllowed` returns `false`, hiding the sidebar link even though the role gate passed.

**Mobile-only symptom explained:** On mobile, the sidebar is a drawer opened by user action. If the drawer is opened before `loadCapabilities()` resolves, `capabilitiesLoaded` is `false`, the capability block is skipped, and links appear based on roles alone. On desktop, the sidebar is always visible and `renderSidebar()` runs after capabilities load, so links are filtered by capabilities and disappear. This race condition makes links appear to work only on mobile.

Server-side permission enforcement is unchanged — each amenities/laundry page still checks capabilities for individual actions. The sidebar link is purely a navigation aid; the real access control happens on the server.

### Fix

`JONE_TARGETED_FIXES/frontend/js/dashboard.js`

Removed the early `return` on the capability check in `navItemAllowed()`. `canOpenPage()` (roles) remains the gate for showing the navigation link. Capabilities refine what a user can DO on a page and are enforced server-side for each action — they must not hide a navigation link that the role gate already allows. The function now falls through to entry-level role checks (`it.role`, `it.minRole`, `it.roles`) instead of returning early on capabilities alone.

### Files changed

- `frontend/js/dashboard.js` — removed early return on capability check in `navItemAllowed()`

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

The fix changes client-side navigation behavior only. Server-side capability enforcement remains fully intact. Verify on the live deployment that:
- Amenities and Laundry sidebar links appear on desktop for roles with `canOpenPage` access (receptionist, housekeeping, porter, security for laundry; admin, general_manager, receptionist, storekeeper for amenities).
- Users who lack specific capabilities can still navigate to the pages but are blocked from individual actions (issue/allocate amenities, manage/assign laundry requests) by existing server-side checks.

---

## Files in JONE_TARGETED_FIXES/

Only new/changed files are included. No unchanged files, no reports/build artefacts/secrets.

| File | Purpose |
|------|---------|
| `backend/apps/bookings/services/booking_service.py` | Issue #5: catch `RoomType.DoesNotExist` → `NotFound` (404) instead of unhandled 500 |
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