# FRONTEND ↔ BACKEND CONTRACT — J-ONE HOTEL & LODGE

**This document is the binding agreement between the separated frontend
(HTML/CSS/Vanilla JS) and this API.** Field names, types, envelopes, error
codes, and state values below are contractual. Any intentional change must
update this file in the same commit (spec §137/§144).

Quick rules for the frontend (Vanilla `fetch`):

```js
const res = await fetch(url, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  },
  body: JSON.stringify(payload),
});
const body = await res.json();          // ALWAYS the same envelope
if (body.success) { /* body.data */ }
else { /* body.code, body.message, body.errors? */ }
```

* No special-case parsing per endpoint. HTTP status + `success` decide.
* **Types:** ids `int` · dates `"YYYY-MM-DD"` · datetimes ISO-8601 · **money strings `"25000.00"`** (never floats) · booleans JSON booleans.
* **Never send:** prices, totals, roles, availability claims — the server ignores/rejects them.
* Base URL is injected by the frontend (env var), e.g. `https://api.example.com/api/`.
* Media URLs are absolute and render directly in `<img src>`.

## Error codes the frontend must handle

`VALIDATION_ERROR` (400) · `UNAUTHORIZED` (401 — refresh & retry, else login) ·
`FORBIDDEN` (403) · `RESOURCE_NOT_FOUND` (404) · `RATE_LIMITED` (429 — show retry timer) ·
`SERVER_ERROR` (500) · and domain codes: `INVALID_DATES`, `CAPACITY_EXCEEDED`,
`ROOM_UNAVAILABLE`, `BOOKING_EXPIRED`, `INVALID_BOOKING_STATE`,
`CANCELLATION_NOT_ALLOWED`, `OFFER_NOT_APPLICABLE`, `OUTSTANDING_BALANCE`,
`PAYMENT_NOT_CONFIGURED`, `PAYMENT_FAILED`, `PAYMENT_ALREADY_COMPLETED`,
`PAYMENT_AMOUNT_MISMATCH`, `PAYMENT_GATEWAY_ERROR`, `TABLE_SESSION_CONFLICT`, `STORAGE_UPLOAD_FAILED`
(502 — the media bucket rejected the upload; show the message and let the user retry).

`errors` (when present) maps field → message list:

```json
{ "success": false, "code": "VALIDATION_ERROR", "message": "Validation failed.",
  "errors": { "check_in": ["Check-in date cannot be in the past."] } }
```

## Media URLs & image uploads

All image URLs the API returns (`primary_image_url`, `images[].image_url`,
`profile_image_url`, gallery/offer/facility `image_url`) are **absolute**, so the
separately hosted frontend can use them directly — never prefix them with the API
host. They are produced by one helper (`apps.core.storage.absolute_media_url`):

* remote storage (Backblaze B2 / CDN) already returns an absolute URL → passed through;
* otherwise the current request builds it, falling back to `MEDIA_PUBLIC_BASE_URL`
  when there is no request (management commands, availability engine);
* an empty image field yields `null` — render your own placeholder.

Uploads are `multipart/form-data`: build a `FormData`, append the `File` under the
documented field name, and **do not set `Content-Type` manually** (the browser must
add the multipart boundary). Keep the `Authorization` header.

Validation is backend-authoritative: type (jpg/png/webp), size (≤5 MB) and a real
decode check. Failures return `400 VALIDATION_ERROR` with `errors.image`. If the
storage backend itself fails, the API returns `502 STORAGE_UPLOAD_FAILED` and no
database row is created — a failed upload is never reported as success.

## Pagination envelope (all lists)

```json
{ "success": true, "message": "Success", "data": [ ... ],
  "pagination": { "count": 100, "page": 1, "page_size": 20,
                  "total_pages": 5, "next": "…/page=2", "previous": null } }
```

Request params: `?page=1&page_size=20` (max 100). Counts/ids only — `next`/`previous` may be URLs.

---

# PUBLIC WEBSITE

## 1. Hotel info — `GET /api/hotel/`

Renders: header/footer identity, contact block, map, times.

```jsonc
{ "success": true, "message": "Success",
  "data": {
    "hotel_name": "J-ONE HOTEL & LODGE", "tagline": "", "description": "",
    "address": "Plot 566 Mgbowo Street, off Ezike Street",
    "city": "", "state": "", "country": "Nigeria",
    "phone": "+234803 211 2874", "email": "jonathanonu76@gmail.com",
    "google_maps_url": "",                      // embed on contact page when non-empty
    "social_links": [{ "platform": "facebook", "url": "https://…" }],
    "check_in_time": "14:00:00", "check_out_time": "12:00:00",
    "currency": "NGN", "min_stay_nights": 1, "max_stay_nights": 30 } }
```

## 2. Policies — `GET /api/hotel/policies/`

`data`: `[{ "id": 1, "key": "cancellation", "title": "…", "content": "…", "is_active": true, "display_order": 2, "updated_at": "…" }]` (plain text — render safely, never `innerHTML`).

## 3. Facilities — `GET /api/facilities/`

`data`: `[{ "id", "name", "slug", "description", "icon": "wifi", "image_url": "https://…" | null, "is_active", "display_order" }]`
→ map `icon` to the frontend icon set; fall back gracefully when empty.

## 4. Gallery — `GET /api/gallery/?category=ROOMS`

Categories: `HOTEL` `ROOMS` `RESTAURANT` `FACILITIES` `EVENTS` `EXTERIOR` `OTHER`.

```jsonc
"data": { "categories": ["HOTEL", "ROOMS", …],
  "items": [ { "id", "title", "description", "category", "alt_text",
               "image_url", "display_order", "is_active", "created_at" } ] },
"pagination": { … }
```

## 5. Offers — `GET /api/offers/`

```jsonc
"data": [ { "id", "title", "slug", "short_description", "description",
  "discount_type": "PERCENTAGE" | "FIXED_AMOUNT", "discount_value": "10.00",
  "start_date", "end_date", "min_nights": 1, "max_nights": 5 | null,
  "applicable_room_types": [{ "id", "name", "slug" }] | [],   // [] = applies to all
  "is_featured": false, "image_url": null, "terms": "" } ]
```

Validity is display-only info — the **booking/quote endpoints decide** application.

## 6. Room catalog — `GET /api/rooms/`

```jsonc
"data": [ { "id": 2, "name": "Deluxe Room", "slug": "deluxe-room",
  "short_description": "…", "base_price": "25000.00", "max_guests": 2,
  "bed_type": "King Bed", "bed_count": 1, "room_size": "28 m²", "view": "Courtyard View",
  "is_featured": true, "primary_image_url": "https://…" | null,
  "amenities": [{ "name": "Wi-Fi", "icon": "wifi" }] } ]
```

## 7. Room detail — `GET /api/rooms/{slug or id}/`

All list fields **plus**:

```jsonc
{ "description": "…", "smoking_policy": "NON_SMOKING", "children_allowed": true,
  "extra_guest_allowed": true, "extra_guest_fee": "5000.00",
  "images": [ { "id", "image_url", "alt_text", "caption", "display_order", "is_primary" } ],
  "offers": [ { "id", "slug", "title", "short_description", "discount_type", "discount_value", "end_date" } ] }
```

## 7b. Physical rooms of a type — `GET /api/rooms/{slug or id}/rooms/` (public)

Registered **before** the room-type detail route, so `…/rooms/` is never
captured by `<slug:slug>/`. Powers "Book this room — Room 203": the guest picks
one physical room and its **stable id** travels through the booking flow.

Query params (both optional, both required together): `check_in`, `check_out`
(`YYYY-MM-DD`). Without dates `available` is `null`; with dates the
availability engine answers it.

```jsonc
"data": [ { "id": 12, "room_number": "203", "floor": 2, "available": true } ]
```

Only rooms the hotel can actually sell are listed (active room type, active
room, not under maintenance or out of service). Nothing internal —
housekeeping state, notes and status flags — is ever exposed publicly.

## 8. Availability — `GET /api/rooms/availability/`

Params: `check_in*`, `check_out*` (YYYY-MM-DD), `guests`≥1, `rooms`≥1, `room_type` (slug or id).

```jsonc
"data": { "check_in": "2026-10-15", "check_out": "2026-10-18", "nights": 3,
  "guests": 2, "rooms": 1,
  "results": [ {
    "room_type": { …same shape as catalog row… },
    "available_rooms": 3, "requested_rooms": 1,
    "max_guests_per_room": 2, "extra_guest_allowed": false,
    "bookable": true,
    "message": "Not enough rooms available for these dates." | null,
    "pricing": {                                // null when unsellable (see message)
      "nights": 3, "rooms": 1, "adults": 2, "children": 0, "guests": 2,
      "currency": "NGN", "price_per_night": "25000.00", "subtotal": "75000.00",
      "discount": "7500.00",
      "offer": { "id", "title", "code": "DEMO10", "discount_type", "discount_value" } | null,
      "extra_guests": 0, "extra_guest_fee": "0.00",
      "tax": "0.00", "service_fee": "0.00",
      "total": "67500.00", "required_payment": "67500.00", "amount_due_online": "67500.00" } } ] }
```

**No availability** ⇒ `200` with `results: []` or rows whose `bookable` is false.
Treat these as *preview* prices — the final word is `/api/bookings/quote/`.

## 8b. Calendar availability — `GET /api/rooms/<slug or id>/unavailable-dates/`

One request returns the per-night inventory for a whole window (never one
request per date). Params: either `start_date*` + `end_date*`
(`YYYY-MM-DD`, **end exclusive** — check-out semantics, max span 366 days)
or the legacy shorthand `days=N` (defaults to a today+365 window).

```jsonc
"data": {
  "room_type": { "id": 1, "name": "Standard Room", "slug": "standard-room" },
  "total_rooms": 4,                        // sellable physical rooms
  "from": "2026-09-01", "through": "2026-09-30",
  "dates": {                               // one entry PER date in the window
    "2026-09-18": { "available_rooms": 2, "available": true },
    "2026-09-26": { "available_rooms": 0, "available": false } },
  "unavailable_dates": ["2026-09-26", "2026-09-27", "2026-09-28"]  // legacy list (kept)
}
```

A date is `available: false` only when **every** sellable physical room of
the type is blocked that night (CONFIRMED / CHECKED_IN / live PENDING hold);
cancelled, expired, checked-out and no-show reservations never block, and a
check-out day never blocks the next guest's check-in. The sweep is the same
authoritative availability engine that answers the range search and guards
booking creation — the calendar is a UX aid, never a substitute for the
server's final check.

## 9. Enquiries and cancellation/refund requests — `POST /api/enquiries/`

General body: `{ "name": "…", "email": "…", "phone": "…", "subject": "…", "message": "…" }`
(optional hidden field `"website"`: leave blank — it is a spam honeypot).
→ `201 { success, message }`. Throttled; HTML stripped server-side.

Cancellation/refund body uses the same endpoint and adds structured fields:

```jsonc
{ "enquiry_type": "CANCELLATION", "subject": "Cancellation / refund request",
  "booking_reference": "J1-…",
  "payment_reference": "J1P-…",        // optional Paystack/internal payment reference
  "receipt_reference": "J1P-…",        // optional
  "cancellation_reason": "Travel changed",
  "preferred_contact_method": "EMAIL" | "PHONE" | "WHATSAPP",
  "refund_requested": true }
```

Success returns:

```jsonc
"data": { "cancellation_reference": "J1C-…",
  "status_url": "https://frontend/cancellation-result.html?ref=J1C-…&token=…",
  "booking_reference": "J1-…", "status": "NEW" }
```

Submitting this request never cancels the booking and never starts a refund. It
only creates a staff-review row.

**Public status** — `GET /api/enquiries/cancellation-status/{reference}/?token=…`
(or `X-Cancellation-Access-Token`) returns safe status fields and a guest-facing
message. It never reports refund completion unless Paystack has confirmed
`refund.processed`.

## 10. Auth endpoints

| Endpoint | Body | `data` on success |
|---|---|---|
| `POST /api/auth/register/` | email*, first_name*, last_name*, phone, password*, password_confirm* | `{ user, tokens }` |
| `POST /api/auth/login/` | email*, password* | `{ user, tokens }` |
| `POST /api/auth/token/refresh/` | refresh* | `{ tokens: { access, refresh? } }` |
| `POST /api/auth/logout/` | refresh* | — |
| `GET /api/auth/profile/` | — | user |
| `PATCH /api/auth/profile/` | any of first_name/last_name/phone | user |
| `POST /api/auth/password/change/` | current/new/new_confirm passwords | — |
| `POST /api/auth/password/reset/` | email | generic message (always same) |
| `POST /api/auth/password/reset/confirm/` | uid, token, new passwords | — |

`user`:

```jsonc
{ "id": 7, "email": "…", "first_name": "…", "last_name": "…", "full_name": "…",
  "phone": "…", "role": "GUEST" | "RECEPTIONIST" | "MANAGER" | "ADMIN",
  "email_verified": false, "profile_image_url": null,
  "date_joined": "…", "last_login": "…" }
```

`tokens`: `{ "access": "jwt…", "refresh": "jwt…" }` — store access in memory
(preferred) or sessionStorage; refresh per your policy.
Frontend role UI checks: show staff screens when `role != "GUEST"` — the API
still enforces everything server-side.

---

# BOOKING FLOW (guest)

## 11. Quote — `POST /api/bookings/quote/`

Body:
```jsonc
{ "room_type": "deluxe-room",     // slug or id (string)
  "check_in": "2026-10-15", "check_out": "2026-10-18",
  "rooms": 1, "adults": 2, "children": 0, "offer_code": "SAVE10" }
```

`data` = the `pricing` object shown in §8 **plus**:

```jsonc
{ "room_type": { "id", "name", "slug" }, "check_in": "…", "check_out": "…",
  "policies": { "check_in_time": "14:00", "check_out_time": "12:00",
    "cancellation_deadline_hours": 48, "cancellation_fee_percent": "0.00" },
  "hold_info": { "pending_booking_minutes": 15,
    "note": "Inventory is held for this many minutes once the booking is created." } }
```

Errors: `INVALID_DATES`, `CAPACITY_EXCEEDED`, `OFFER_NOT_APPLICABLE` (bad code). Nothing is persisted.

## 12. Create booking — `POST /api/bookings/` 🔑

Body = quote fields **plus** the optional `room_id` (see "Exact room" below):

```jsonc
{ "room_id": 12,            // optional — the physical room the guest picked
  "special_requests": "High floor",
  "guest": {                     // optional overrides for the contact record
    "first_name": "…", "last_name": "…", "email": "…", "phone": "*preferred*",
    "address": "", "city": "", "state": "", "country": "Nigeria",
    "identification_type": "NATIONAL_ID", "identification_number": "" } }
```

201 → full **booking detail**:

```jsonc
"data": {
  "id": 15, "booking_reference": "J1-20261015-3F9A2C1D",
  "guest": { "id", "first_name", "last_name", "full_name", "email", "phone",
             "address", "city", "state", "country", "identification_type", "created_at" },
  "room_type_name": "Deluxe Room", "room_type_slug": "deluxe-room",
  "room_assignments": [ { "id", "room_number": "201", "floor": 2, "check_in", "check_out" } ],
  "check_in": "2026-10-15", "check_out": "2026-10-18", "nights": 3,
  "number_of_rooms": 1, "adults": 2, "children": 0, "number_of_guests": 2,
  "price_per_night": "25000.00", "subtotal": "75000.00", "discount_amount": "7500.00",
  "extra_guest_fee_amount": "0.00", "tax_amount": "0.00", "fee_amount": "0.00",
  "total_amount": "67500.00", "required_payment": "67500.00",
  "amount_paid": "0.00", "amount_due": "67500.00", "refund_amount": "0.00",
  "currency": "NGN", "offer_title": "Demo Offer — 10% Off (development only)",
  "status": "PENDING", "payment_status": "UNPAID", "source": "WEBSITE",
  "special_requests": "High floor", "cancellation_reason": "",
  "expires_at": "2026-10-12T14:31:00+01:00",      // pay before this
  "checked_in_at": null, "checked_out_at": null, "cancelled_at": null,
  "created_at": "…", "can_pay": true, "can_cancel": false }
```

**Exact room ("Book this room — Room 203").** `room_id` is advisory: the server
re-checks availability inside the room-type lock and never trusts the client.

* The room is free → it is assigned first and `room_substitution` is absent.
* The room is gone → a deterministic replacement is selected by the
  availability engine, in this preference order:
  1. another room of the **same room type** with the **same nightly rate**
     (ties broken by room number — deterministic, never random),
  2. same room type, closest nightly rate,
  3. a different **active room type** with an **identical** rate that still
     covers the party (the stay total therefore does not change).
  A replacement at a *different* price is deliberately never auto-applied —
  a changed total may not be charged without the guest's consent, so the API
  returns `409 ROOM_UNAVAILABLE` instead and the guest picks another type.
* No equivalent room exists → `409 ROOM_UNAVAILABLE`.

When a substitution happens, `data` carries an extra key:

```jsonc
"room_substitution": {
  "requested_room_id": 12, "requested_room_number": "203",
  "assigned_room_id": 13,  "assigned_room_number": "204",
  "room_type": "Deluxe Room", "room_type_id": 3,
  "price_changed": false,
  "reason": "Room 204 is available in the same room type (Deluxe Room) for the same nightly rate."
}
```

A room is **never** switched silently — the frontend must surface this to the
guest, and it is mirrored into the audit log metadata.

`status` ∈ `PENDING` `CONFIRMED` `CHECKED_IN` `CHECKED_OUT` `CANCELLED` `EXPIRED` `NO_SHOW` ·
`payment_status` ∈ `UNPAID` `PARTIALLY_PAID` `PAID` `PARTIALLY_REFUNDED` `REFUNDED` `FAILED`.

Frontend rule of thumb: show **Pay** when `can_pay`; do not show direct
self-cancel buttons. Route active-stay cancellation/refund needs to
`contact.html?type=cancellation&booking_reference=…`. Show **Expired** when
`status == "EXPIRED"` (even mid-session).

## 13. My bookings — `GET /api/bookings/` 🔑

Rows:
```jsonc
{ "id", "booking_reference", "room_type_name", "room_type_slug",
  "check_in", "check_out", "nights", "number_of_rooms", "number_of_guests",
  "total_amount", "amount_paid", "amount_due", "currency",
  "status", "payment_status", "created_at", "expires_at" }
```
(`?status=CONFIRMED` filter supported, paginated, empty list = `200 []`.)

## 14. Booking detail — `GET /api/bookings/{id or reference}/` 🔑

Same payload as §12. Unknown or unauthorized lookups both return 404.

## 15. Legacy guest cancel endpoint — `POST /api/bookings/{id or reference}/cancel/` 🔑

This endpoint remains only as a non-destructive compatibility path. It enforces
owner/guest-token privacy, then returns `400 CANCELLATION_NOT_ALLOWED` with
guidance to submit the Contact cancellation/refund request. Do not call it from
new frontend UI.

## 16. Payments

**Initialize** — `POST /api/payments/initialize/` (guest token or owner/staff JWT)
`{ "booking_reference": "J1-…" }` → 201:

```jsonc
"data": { "reference": "J1P-20261012-9A1B2C3D4E",
  "booking_reference": "J1-…",
  "authorization_url": "https://checkout.paystack.com/…",   // redirect the guest here
  "amount": "67500.00", "currency": "NGN", "reused": false }
```

Redirect to `authorization_url`. Paystack secret/public credentials are not
needed by this hosted-checkout frontend.

**Verify** — `GET /api/payments/verify/{reference}/` (guest token or owner/staff JWT; call when Paystack
redirects back to `PAYMENT_CALLBACK_URL`; safe to call repeatedly):

```jsonc
"data": { "payment_reference": "J1P-…", "booking_reference": "J1-…",
  "transaction_status": "success", "booking_status": "CONFIRMED", "payment_status": "PAID",
  "amount_paid_this_transaction": "67500.00",
  "booking_amount_paid": "67500.00", "booking_amount_due": "0.00",
  "booking_total": "67500.00", "currency": "NGN", "paid_at": "…" }
```

Terminal failures return `transaction_status` failed/abandoned/reversed;
processing states remain pending. **Never trust the browser redirect; only this
endpoint (or the signed webhook) confirms.**

## 17. Receipt — `GET /api/bookings/{id or reference}/receipt/` 🔑

```jsonc
"data": { "hotel": { "name", "address", "city", "state", "country", "phone", "email" },
  "booking_reference", "booking_status", "booking_status_label",
  "payment_status", "payment_status_label",
  "guest": { "name", "email", "phone", "address", "city", "state", "country" },
  "room_type", "room_numbers": ["203"], "rooms", "check_in", "check_out",
  "nights", "number_of_guests", "adults", "children", "price_per_night",
  "subtotal", "discount", "tax", "fees", "total",
  "amount_paid", "amount_due", "currency",
  "issued_at", "receipt_reference",
  "latest_payment": { "reference", "transaction_id", "provider_reference", "amount",
                      "provider", "provider_label", "channel", "status",
                      "paid_at", "recorded_by", "notes" } | null,
  "previous_payments_total": "0.00",
  "payments": [ { "reference", "transaction_id", "provider_reference", "amount",
                  "status", "provider", "provider_label", "channel",
                  "paid_at", "recorded_by", "notes" } ] }
```

Every field previously documented is still present — the additions are purely
additive, so existing consumers keep working. Render as the Access-inspired
professional receipt page: J-ONE logo lockup, centred title/generated line,
gold labels, blue values, ruled rows, and receipt-only image/PDF/print exports.

## 18. Notifications — `GET /api/notifications/` 🔑

```jsonc
"data": { "unread_count": 2,
  "notifications": [ { "id", "type": "BOOKING_CREATED" | "BOOKING_CONFIRMED" |
    "BOOKING_CANCELLED" | "BOOKING_MODIFIED" | "PAYMENT_SUCCESS" | "PAYMENT_FAILED" |
    "CANCELLATION_REQUEST_CREATED" | "CANCELLATION_REQUEST_REVIEWED" |
    "CANCELLATION_REQUEST_APPROVED" | "CANCELLATION_REQUEST_REJECTED" |
    "REFUND_PENDING" | "REFUND_PROCESSING" | "REFUND_PROCESSED" |
    "REFUND_FAILED" | "REFUND_NEEDS_ATTENTION" |
    "CHECK_IN" | "CHECK_OUT" | "ENQUIRY_NEW" | "SYSTEM",
    "title", "message", "link": "/my-booking.html?ref=J1-…",
    "is_read": false, "created_at" } ] }, "pagination": { … }
```

`GET /unread-count/` → `{ unread_count }` (cheap polling) ·
`POST /{id}/read/` and `POST /read-all/` → `{ unread_count }`.
`link` is a frontend-relative path you may route to.

**Detail** `GET /api/notifications/{id}/` 🔑 (registered before
`/{id}/read/`) — the notification details page. The queryset is scoped to the
signed-in recipient, so another user's id is a **404, never a leak**. Opening
the detail marks it read and returns the fresh unread count, so the sidebar
badge can update without another request.

```jsonc
"data": { "id", "type", "type_label", "title", "message", "link",
  "is_read", "created_at", "unread_count": 0,
  "related": { "kind": "booking"|"payment"|"guest"|"room"|"enquiry"|"notification",
               "reference": "J1-…", "link": "/dashboard/booking-details.html?ref=J1-…" } | null }
```

---

# STAFF DASHBOARD (`/api/admin/`)

Requires `role` of RECEPTIONIST/MANAGER/ADMIN. All lists are paginated and
support `search` where it makes sense. Receptionists get identical structures;
manager/admin-only blocks are simply absent (see §19).

## 19. Dashboard — `GET /api/admin/dashboard/`

```jsonc
"data": {
  "today": { "date": "2026-10-12", "arrivals": 4, "departures": 2, "in_house": 8, "no_shows": 0 },
  "rooms": { "total": 9, "occupied": 4, "available": 5, "out_of_order": 0,
             "occupancy_rate_percent": 44.4 },
  "bookings": { "created_today": 3, "pending": 2, "confirmed_upcoming": 11, "cancelled_today": 1 },
  "recent_bookings": [ { "booking_reference", "guest_name", "room_type_name",
      "check_in", "check_out", "status", "payment_status", "total_amount",
      "currency", "created_at" } ],
  "notifications": { "unread_count": 3 },
  // —— only when role is MANAGER or ADMIN ——
  "revenue": { "today": "150000.00", "this_month": "2125000.00",
               "outstanding_total": "90000.00", "outstanding_bookings": 2 },
  "recent_payments": [ { "reference", "booking_reference", "amount", "currency",
      "provider", "channel", "paid_at" } ],
  "alerts": [ { "type": "OVERDUE_CHECKIN" | "LATE_CHECKOUT" | "HOUSEKEEPING",
                "message": "…" } ] }
```

Do not assume `revenue` exists — check.

## 20. Bookings table — `GET /api/admin/bookings/`

Filters: `status`, `payment_status`, `source`, `room_type`, `date_from`,
`date_to`, `check_in`, `check_out` (exact date), `search`, `ordering`
(`created_at`, `check_in`, `check_out`, `total_amount` — prefix `-` desc).

**`search` is the operational "find it from any scrap of information" box.**
One term is matched (case-insensitively, partially) against:

| matches | field |
|---|---|
| booking reference (or a prefix of it) | `booking_reference` |
| guest first / last name | `guest__first_name`, `guest__last_name` |
| guest email | `guest__email` |
| guest phone | `guest__phone` |
| physical room number | `room_assignments__room__room_number` |
| room type name or slug | `room_type__name`, `room_type__slug` |
| payment / receipt reference | `payments__reference` |
| gateway transaction id | `payments__transaction_id` |

Multi-word input ("amina yusuf") is ANDed across the guest's name parts, so it
finds that guest without also matching everyone whose surname is Yusuf. Results
are de-duplicated (`DISTINCT`) because the payment/assignment joins can match a
booking more than once. The frontend must debounce this and must never download
the full list to filter locally.

**Checkout desk** reuses this endpoint instead of a bespoke one:
`GET /api/admin/bookings/?status=CHECKED_IN,CONFIRMED&search=203&page_size=25`
returns only bookings that can be checked out, already matched by room number,
booking reference, guest name, phone or email.

Row (everything the table needs — no per-row requests):

```jsonc
{ "id", "booking_reference", "guest_name", "guest_phone", "guest_email",
  "room_type_name", "room_numbers": ["201"], "check_in", "check_out", "nights",
  "number_of_rooms", "number_of_guests", "total_amount", "amount_paid",
  "amount_due", "currency", "status", "payment_status", "source", "created_at" }
```

**Detail** `GET …/bookings/{id|ref}/` adds: full `guest` object,
`room_assignments`, `adults/children`, price-breakdown fields,
`required_payment`, `refund_amount`, `offer_title`, `special_requests`,
`internal_notes`, lifecycle timestamps.

**Create manual booking** `POST …/bookings/`:

```jsonc
{ "room_type": "deluxe-room", "check_in": "…", "check_out": "…", "rooms": 1,
  "adults": 2, "children": 0, "source": "WALK_IN" | "PHONE",
  "status": "CONFIRMED" | "PENDING", "offer_code": "", "special_requests": "",
  "internal_notes": "", "room_id": 12,          // optional exact physical room
  "guest": { "first_name": "*", "last_name": "*",
  "email": "*", "phone": "*", …optional fields… } }
```

The `201` response is the full booking **detail** (guest, room assignments,
totals and balance), so the receptionist can go straight into the payment
dialog with **no extra request and no bookings-table reload**. `room_id`
follows exactly the same substitution rules as §12 (`room_substitution`).

**Modify** `PATCH …/bookings/{id|ref}/` — any of `check_in, check_out,
number_of_rooms, adults, children, special_requests, internal_notes`
(re-availability + re-priced automatically; returns updated detail).

**Actions** (POST, body `{}` or `{ "reason": "…" }`, checkout also accepts
`{ "allow_balance_due": false }`):

| Endpoint | Result state | Guards |
|---|---|---|
| `…/confirm/` | CONFIRMED | only PENDING |
| `…/cancel/` | CANCELLED | audited |
| `…/check-in/` | CHECKED_IN, rooms OCCUPIED | CONFIRMED + date reached |
| `…/check-out/` | CHECKED_OUT, rooms AVAILABLE+DIRTY | balance settled or override |
| `…/no-show/` | NO_SHOW | past check-in date |
| `…/assign-room/` | `{ assignment_id, room_number }` | type match, no overlap |

## 21. Guests — `GET /api/admin/guests/` · `GET/PATCH /api/admin/guests/{id}/`

List rows: `{ id, first_name, last_name, full_name, email, phone, city, state,
country, bookings_count, last_booking_at, created_at }` (`?search=`).

**Detail** (`GET /api/admin/guests/{id}/`) returns the **complete** guest
profile — every non-sensitive column the `Guest` model stores:

```jsonc
{ "id", "user_id", "has_account", "first_name", "last_name", "full_name",
  "email", "phone", "address", "city", "state", "country",
  "identification_type", "identification_type_label", "identification_number",
  "special_requests", "bookings_count", "last_booking_at", "total_stays",
  "total_spent", "outstanding_balance", "created_at", "updated_at",
  "bookings": [ { "id", "booking_reference", "room_type_name",
                  "room_numbers": ["203"], "check_in", "check_out", "nights",
                  "status", "payment_status", "total_amount", "amount_paid",
                  "amount_due", "currency", "source", "created_at" } ] }
```

**Privacy:** `identification_number` is masked for `RECEPTIONIST` (only the
last four characters) and full for `MANAGER` / `ADMIN`. The frontend never
un-masks it — the masking is the backend's job.

PATCH accepts contact + identification fields.

## 22. Payments — `GET /api/admin/payments/` · `GET …/payments/{id|ref}/` · `POST …/payments/record/`

Payment and refund list/detail routes require the `payment.read` capability.
The staff receipt screen uses `GET /api/admin/bookings/{id|ref}/receipt/`, also
`payment.read`-gated; it is separate from the guest-owned
`GET /api/bookings/{id|ref}/receipt/` route. Receipt email delivery requires
`payment.receipt.send`. Recording an offline room-booking payment and staff-
initiated online payment initialization/verification each require both
`booking.manage` and `payment.capture`. Guest-owner and guest-token checkout
flows are unchanged. A `payment.capture` capability by itself does not grant
access to the booking payment APIs. The frontend awaits capability resolution
before loading the Receipts list or deriving its links/actions, and hides
read-only/send/record controls from roles without their respective
capabilities; the API remains authoritative.

Row: `{ id, reference, booking_reference, guest_name, staff_email, provider:
PAYSTACK|CASH|POS|BANK_TRANSFER, amount, currency, status: PENDING|SUCCESS|
FAILED|PARTIALLY_REFUNDED|REFUNDED, channel, gateway_response, transaction_id,
paid_at, notes, created_at }` — filters `status, provider, date, date_from,
date_to, paid_on, search`.

Refund rows are read-only here: `GET /api/admin/payments/refunds/` and
`GET /api/admin/payments/refunds/{id}/` return `{ id, booking_reference,
payment_reference, cancellation_reference, amount, currency, status:
PENDING|PROCESSING|PROCESSED|FAILED|NEEDS_ATTENTION, paystack_transaction_id,
paystack_transaction_reference, paystack_refund_id, paystack_refund_reference,
customer_note, merchant_note, failure_reason, requested_by_email, submitted_at,
processed_at, failed_at, created_at }`. Refund initiation happens from an
approved cancellation request, not from the generic payment list.

Record offline payment:

```jsonc
POST { "booking_reference": "J1-…", "amount": "15000.00",
       "provider": "CASH" | "POS" | "BANK_TRANSFER", "notes": "…" }
→ 201 payment row **plus** the authoritative booking snapshot, so the payment
  modal refreshes itself from one response (no second round-trip, no full
  bookings reload):

  { …payment row…,
    "booking": { "id", "booking_reference", "status", "payment_status",
                 "currency", "total_amount", "amount_paid", "amount_due",
                 "guest_name", "guest_email", "guest_phone",
                 "room_type_name", "room_numbers", "check_in", "check_out",
                 "nights", "number_of_guests" },
    "receipt_reference": "J1P-…", "has_receipt": true }
```

Validation (server-side — a client-computed total is never trusted):
`amount` must be > 0 and ≤ the booking's outstanding balance (otherwise `400
PAYMENT_FAILED` and **nothing is written**); the booking must be `PENDING`,
`CONFIRMED` or `CHECKED_IN` (otherwise `409`); unknown reference → `404`.
Confirms the booking when the deposit is covered.

## 23. Rooms & room types (staff)

* `GET/POST /api/admin/rooms/` · `GET/PATCH/DELETE /api/admin/rooms/{id}/`
  Row: `{ id, room_number, room_type, room_type_name, room_type_slug, floor,
  status, housekeeping_status, notes, is_active, created_at, updated_at,
  room_type_base_price, room_type_max_guests, room_type_is_active,
  primary_image_url, images[] }`
  (`status`: AVAILABLE/OCCUPIED/RESERVED/MAINTENANCE/OUT_OF_SERVICE ·
  `housekeeping_status`: CLEAN/DIRTY/CLEANING). Filters: `status`,
  `housekeeping_status`, `room_type`, `is_active`, `search`. **DELETE
  deactivates** (soft). Writes need MANAGER/ADMIN.
  `room_type_*` are read-only mirrors of the assigned type so the dashboard can
  label the room-type selector without a second request; `primary_image_url`
  falls back to the room type's cover when the room has no photo of its own.
  Accepts **JSON or multipart/form-data**; send `image` (file) to set/replace
  this room's own primary photo.
* `GET/POST /api/admin/room-types/` · `GET/PATCH/DELETE …/{id}/` — full type
  incl. pricing/capacity/policies + `amenity_ids[]` + `images[]` + `room_count`
  + `primary_image_url`. DELETE deactivates.
  Accepts **JSON or multipart/form-data** on POST/PATCH/PUT. Multipart write-only
  fields: `image` (file, jpg/png/webp ≤5MB) sets or replaces the cover image,
  `image_alt_text` (string) and `remove_image=true` clears it. They map onto the
  existing `RoomTypeImage` rows (`is_primary=true`) — there is no separate
  `ImageField` on the model, and `images[]`/`primary_image_url` keep their shape.
  In multipart, repeat `amenity_ids` once per id; send a single empty
  `amenity_ids` value to clear the selection.
* `POST /api/admin/room-types/{id}/images/` — **multipart/form-data**:
  `image*` (jpg/png/webp ≤5MB), `alt_text`, `caption`, `display_order`,
  `is_primary` → 201 image row. `PATCH/DELETE /api/admin/room-images/{id}/`.
  Still supported for managing the full gallery; the `image` field above is the
  shortcut for the single cover image.
* `GET/POST/PATCH/DELETE /api/admin/amenities/…` — `{ id, name, slug, icon, is_active }`.

## 24. Content management

* `GET/POST /api/admin/facilities/…` `{ id, name, slug, description, icon, image_url, is_active, display_order }`
* `GET/POST /api/admin/policies/…` `{ id, key, title, content, is_active, display_order }`
* `GET/POST /api/admin/offers/…` `{ id, title, slug, description, short_description, code, discount_type, discount_value, start_date, end_date, min_nights, max_nights, room_type_ids[], is_active, is_featured, image/image_url, terms }` — dates/percent/stay rules validated with `VALIDATION_ERROR`.
* `GET/POST /api/admin/gallery/…` (`POST` multipart with `image`) `{ id, title, description, category, alt_text, image/image_url, display_order, is_active }` · filter `category`, `is_active`.
* `GET/PATCH /api/admin/settings/` — GET manager+, PATCH **ADMIN-only**:
  ```jsonc
  { "hotel_name", "tagline", "description", "address", "city", "state",
    "country", "phone", "email", "google_maps_url", "social_links",
    "check_in_time", "check_out_time", "min_stay_nights", "max_stay_nights",
    "currency", "tax_rate_percent", "service_fee", "deposit_percent",
    "pending_booking_minutes", "cancellation_deadline_hours",
    "cancellation_fee_percent", "updated_at" }
  ```

## 25. Enquiries / cancellation review — `GET /api/admin/enquiries/` · `GET/PATCH …/{id}/`

General enquiry fields remain: `{ id, name, email, phone, subject, message,
status: NEW|IN_PROGRESS|RESOLVED|CLOSED, internal_notes, created_at, updated_at }`;
PATCH only accepts `status` and `internal_notes`. Filters: `status`, `type`,
`enquiry_type`, `cancellation_status`, `refund_status`, `search`.

Cancellation/refund rows add: `enquiry_type=CANCELLATION`,
`cancellation_reference`, supplied booking/payment/receipt references,
`cancellation_reason`, `preferred_contact_method`, `refund_requested`, linked
`related_booking_detail`, linked `related_payment_detail`, `cancellation_status`,
`refund_status`, calculated fee/refund amount, latest `refund_detail`,
`cancellation_policy`, and `actions`.

Action endpoints (all POST):

| Endpoint | Role | Effect |
|---|---|---|
| `/api/admin/enquiries/{id}/review/` | staff | mark under review |
| `/api/admin/enquiries/{id}/approve-cancellation/` | staff | cancel linked booking; refund remains separate |
| `/api/admin/enquiries/{id}/reject-cancellation/` | staff | reject request and email guest |
| `/api/admin/enquiries/{id}/process-refund/` | manager/admin | submit eligible Paystack refund backend-only; amount is server-derived |
| `/api/admin/enquiries/{id}/close/` | staff | close request |

Never display refund completion to a guest from an approval/process-click alone;
only `refund_status=PROCESSED` / `cancellation_status=REFUNDED` means Paystack
confirmed it.

## 26. Reports (MANAGER+)

### Ledger-authoritative financial summary

`GET /api/admin/finance/reports/summary/?start=YYYY-MM-DD&end=YYYY-MM-DD&currency=NGN`
requires the `reports.financial.view` capability. `start` and `end` are required,
ordered business dates and the range may not exceed 366 days; `currency` defaults
to `NGN`. The report uses only posted immutable ledger evidence, never the legacy
booking/payment projections below:

```jsonc
{ "basis": "posted_immutable_ledger_business_date", "start_date", "end_date", "currency": "NGN",
  "ledger_transactions": 5,
  "guest_charges": { "amount": "110.00", "transactions": 1 },
  "recognized_revenue": { "amount": "100.00", "transactions": 1 },
  "tax_accrued": { "amount": "10.00", "transactions": 1 },
  "collections": { "amount": "110.00", "transactions": 1 },
  "refunds": { "amount": "20.00", "transactions": 1 },
  "discounts": { "amount": "0.00", "transactions": 0 },
  "operating_expenses": { "amount": "15.00", "transactions": 1 },
  "inventory_acquisitions": { "amount": "30.00", "transactions": 1 },
  "cash_paid_out": { "amount": "45.00", "transactions": 2 },
  "net_collections": "90.00", "gross_operating_result": "85.00",
  "by_day": [ { "date", "guest_charges", "recognized_revenue", "collections",
                 "refunds", "operating_expenses", "net_collections", "gross_operating_result" } ] }
```

`net_collections` is collections less refunds. `gross_operating_result` is
recognized revenue less operating expenses; discounts, inventory acquisitions,
and cash paid-outs remain distinct visible measures. The staff Reports page uses
this endpoint for money KPIs and ledger-measure export while retaining the
operational projections below for bookings and occupancy.

### Legacy booking/payment and operational projections — `?start_date&end_date` (YYYY-MM-DD, ≤ 366 days)

**Revenue** `GET /api/admin/reports/revenue/` (compatibility only; not an authoritative financial summary):
```jsonc
{ "start_date", "end_date", "currency": "NGN", "grand_total": "2125000.00",
  "transactions": 41, "refunds": { "total": "15000.00", "count": 1 },
  "by_day": [ { "date", "total", "transactions" } ],
  "by_provider": [ { "provider", "total", "transactions" } ] }
```

**Occupancy** `GET /api/admin/reports/occupancy/`:
```jsonc
{ "start_date", "end_date", "days": 7, "active_rooms": 9,
  "occupied_room_nights": 38, "available_room_nights": 63,
  "average_occupancy_percent": 60.3,
  "by_day": [ { "date", "occupied_rooms": 6, "occupancy_rate_percent": 66.7 } ] }
```

**Bookings** `GET /api/admin/reports/bookings/`:
```jsonc
{ "total_bookings": 25, "cancelled": 3, "no_shows": 1,
  "cancellation_rate_percent": 12.0,
  "by_status": [ { "status", "count" } ],
  "by_payment_status": [ { "payment_status", "count" } ],
  "by_room_type": [ { "room_type", "bookings", "revenue" } ] }
```

## 26b. Restaurant table register and service sessions — POS

These endpoints extend the existing shared POS workflow; table sessions do not
create a separate order total, ledger, or payment record. Table configuration is
manager-controlled (`restaurant.table.manage`). Restaurant order staff
(`restaurant.order.manage`) can see active tables, open sessions, and attach
multiple checks to one open session.

| Method & path | Body / query | Result and rules |
|---|---|---|
| `GET /api/admin/pos/restaurant-tables/` | `active=true\|false\|all` (default `true`), `search`, `page`, `page_size` | Paginated `{id, code, name, section, seats, is_active, created_at, updated_at}`. |
| `POST /api/admin/pos/restaurant-tables/` | `{code, name?, section?, seats}` | Creates an active table; code is trimmed and uppercased. Codes use letters/numbers/dot/underscore/hyphen; seat capacity is 1–60. |
| `PATCH /api/admin/pos/restaurant-tables/{id}/` | Any mutable table fields; `{is_active:false}` deactivates without deleting history. | While a session is open, no table edits are allowed. After the first session, code/name/section/capacity are frozen to preserve historical context; only activation changes remain. |
| `GET /api/admin/pos/restaurant-table-sessions/` | `status=OPEN\|CLOSED\|ALL` (default `OPEN`), optional `table_id`, pagination | Paginated table/session state and bounded counts for linked, in-progress, and unpaid orders. |
| `POST /api/admin/pos/restaurant-table-sessions/` | `{table_id, covers?, notes?, idempotency_key}` | Opens one session on an active table if covers fit its capacity. Same key + same payload returns the original session; changed payload is rejected. A table can have only one open session. |
| `GET /api/admin/pos/restaurant-table-sessions/{reference}/` | — | One session snapshot with its table identity and counts. |
| `POST /api/admin/pos/restaurant-table-sessions/{reference}/close/` | `{close_note?}` | Closes idempotently only when every linked order is terminal and every delivered direct-sale order is fully settled. Otherwise returns `409 TABLE_SESSION_CONFLICT`. |
| `POST /api/admin/pos/orders/` (additive fields) | Existing POS order body plus optional `table_session_reference` and `service_request_reference` | `table_session_reference` is accepted only for `mode=RESTAURANT`; the server verifies the session is open and derives `table_number` from the registered table. An optional `service_request_reference` requires the caller to have both `guest_request.manage` and the mode's POS capability; it links one eligible anonymous table QR Food & Beverage request to one draft. Restaurant mode requires the QR label to equal the exact registered table code and a matching open session; bar mode preserves its free-text table/pickup label. The request remains open, the order is not submitted automatically, and no charge is posted until the existing delivery workflow. Legacy requests without either field remain supported. |

A table session is operational grouping only. Each POS check keeps its existing
server-calculated line snapshots, kitchen ticket, ledger charge and tender. Bar,
takeaway, and room-service contracts are unchanged. Multi-table merge/split and
a bar-specific table registry remain out of scope; table QR intake itself still
accepts no menu lines or payment details.

## 27. User administration (ADMIN only) — `GET/POST /api/admin/users/` · `GET/PATCH …/{id}/`

`GET /api/admin/users/staff/{id}/` 🔑 — the **staff profile card** (registered
before the numeric detail route). Read-only; mutations stay on the ADMIN-only
`…/users/{id}/` endpoint.

* `ADMIN` — every account, `can_manage: true`.
* `MANAGER` — every account, read-only, `can_manage: false`.
* `RECEPTIONIST` — **their own** account only; anything else is `403`.

```jsonc
"data": { "id", "email", "first_name", "last_name", "full_name", "phone",
  "role", "role_label", "profile_image_url", "is_active", "email_verified",
  "is_staff", "is_admin", "is_staff_member", "date_joined", "last_login",
  "updated_at", "can_manage", "guest_bookings_count",
  "stats": { "bookings_created", "payments_recorded", "last_payment_at",
             "actions_logged", "last_action_at" } }
```

No credential material (password hash, tokens, secrets) is ever serialised.

`GET /api/admin/users/?role__in=ADMIN,MANAGER,RECEPTIONIST` (additive filter,
also accepts `roles=`) lists only staff accounts, so guest accounts never
appear on the staff-management screen. An absent parameter changes nothing.

List rows: `{ id, email, first_name, last_name, full_name, phone, role,
is_active, email_verified, bookings_count, date_joined, last_login }`
(filters `role`, `is_active`, `search`, `ordering`).
Create: `{ email, first_name, last_name, phone, role, password, is_active }`.
Patch: any of `first_name, last_name, phone, role, is_active, email_verified`
(self-lockout/demotion refused; role changes are audited).

## 28. Audit logs (ADMIN, read-only) — `GET /api/admin/audit-logs/` · `.../{id}/`

`{ id, actor_email, actor_name, action, object_type, object_id, summary,
changes: { field: [old, new] }, metadata: {…}, ip_address, created_at }` —
filters `action`, `object_type`, `actor`, `date_from`, `date_to`.
Actions emitted today: `USER_CREATED` `USER_UPDATED` `USER_ROLE_CHANGED`
`SETTINGS_CHANGED` `BOOKING_CREATED` `BOOKING_MODIFIED` `BOOKING_CONFIRMED`
`BOOKING_CANCELLED` `BOOKING_EXPIRED` `CHECK_IN` `CHECK_OUT` `NO_SHOW`
`ROOM_ASSIGNED` `PAYMENT_INITIALIZED` `PAYMENT_VERIFIED` `PAYMENT_RECORDED`
`ROOM_CREATED/UPDATED/DEACTIVATED` `ROOMTYPE_* (CREATED/UPDATED/DEACTIVATED/IMAGE_ADDED/IMAGE_REMOVED)`
`AMENITY_*` `FACILITY_*` `HOTELPOLICY_*` `GALLERYITEM_*` `OFFER_CREATED/UPDATED`
`ENQUIRY_STATUS_CHANGED` `GUEST_UPDATED`.

---

# Contract change control

* Additive changes (new fields/endpoints) may ship after a note here;
  frontend must tolerate unknown fields.
* Renames/removals/meaning changes require a coordinated frontend deploy and a version note here.
* The single source of truth for machine-readable structure is `/api/schema/`;
  for human behavior, this file.

## Changelog

### 2026-09-13 — operational search, staff profiles, receipt & exact-room booking

All changes are **additive**; no field was renamed or removed.

| Change | Where |
|---|---|
| `GET /api/rooms/{slug}/rooms/` — physical rooms of a type (public) | §7b |
| `room_id` (exact physical room) + `room_substitution` on booking create | §12, §20 |
| Receipt gains `room_numbers`, `issued_at`, `receipt_reference`, `latest_payment`, `previous_payments_total`, `*_label`, guest/stay detail | §17 |
| `GET /api/notifications/{id}/` — owner-scoped detail with `related` | §18 |
| Admin bookings `search` now also matches room number, room type and payment/receipt reference | §20 |
| `POST /api/admin/payments/record/` returns the booking snapshot + `receipt_reference` | §22 |
| Guest detail is complete and masks the ID number for receptionists | §21 |
| `GET /api/admin/users/staff/{id}/` — credential-free staff profile card | §27 |
| `GET /api/admin/users/?role__in=…` — staff-only listing | §27 |
