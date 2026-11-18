# Changed files manifest

Repository: `https://github.com/Jonehotel-06/j1-hotel-management-system`
Branch: `main` · Base commit inspected: `3a53209` ("other revenew from event")
Repair date: 2026-11-18

10 files: **7 modified**, **3 added**. No file outside the Event / Mini Mart scope
was touched. No model changes → **no database migration is required**
(`manage.py makemigrations --check` → "No changes detected").

Copy each file below over the same relative path in your project root.

## Modified

| # | Path | Why |
|---|------|-----|
| 1 | `frontend/js/config.js` | Added the missing `events: "/api/admin/events/"` endpoint entry (one line). |
| 2 | `frontend/js/events.js` | Event API base now resolved from `APP_CONFIG.API_ENDPOINTS.events` with a literal fallback, instead of `ReceptionServices.base + "events/"`; real Retry controls for locations and the events list; empty/error option text separated; refresh button reloads both. |
| 3 | `frontend/js/minimart.js` | Room-charge payment path: guest+booking required, tender fields suppressed, checkout button gated on a valid guest/stay. |
| 4 | `frontend/dashboard/minimart.html` | "Room charge (guest folio)" payment option + explanatory hint; caption explains the uplift is settled with the guest's bill. |
| 5 | `backend/apps/guest_services/reception_views.py` | `CheckoutSerializer` accepts and validates `ROOM_CHARGE`; catalogue advertises it; `sale_payload()` reports a folio charge honestly (method `ROOM_CHARGE`, `amount_paid` `0.00`) instead of as a tender-less unpaid sale. |
| 6 | `backend/apps/guest_services/minimart_service.py` | `complete_sale()` room-charge settlement: in-house stay + open main folio resolution, folio posting, no tender/cash movement, idempotency preserved. |
| 7 | `backend/tests/test_reception_minimart_repair.py` | One assertion updated: the catalogue now advertises `ROOM_CHARGE` alongside the four existing methods. No test was removed, skipped or weakened. |

## Added

| # | Path | Purpose |
|---|------|---------|
| 8 | `backend/tests/test_minimart_room_charge.py` | 14 backend tests for room-charge settlement, refusals, idempotency, folio/ledger/stock integrity. |
| 9 | `frontend/tests-js/test-events-api-routing.js` | 5 jsdom tests proving every event call targets `/api/admin/events/` and that Retry re-issues the request. |
| 10 | `frontend/tests-js/test-minimart-room-charge.js` | 6 jsdom tests for the room-charge UI contract and the unchanged walk-in cash path. |

## Not modified (deliberately)

- `backend/apps/events/*` — the events API, models, services and seed migration were
  already correct; the failures originated in the frontend base URL.
- `backend/apps/pos/*`, `backend/apps/finance/*`, `backend/apps/inventory/*` — reused
  as-is (existing tender, folio and stock-movement services).
- `frontend/js/reception-services.js` — `S.base` remains correct for guests, laundry and
  Mini Mart; only the event page needed a different namespace.
- No `.env`, secrets, keys or production data are included.
