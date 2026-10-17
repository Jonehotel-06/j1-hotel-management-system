# J-ONE Hotel & Lodge — Delivery report, 17 October 2026

Batch: nine items raised against the staff console, procurement, finance and
storage. Every item below is implemented, migrated and covered by tests.

| # | Item | Status |
|---|---|---|
| 1 | Apps missing from django-admin | Done — 7 apps registered, 83 models, all changelists return 200 |
| 2 | Images not uploading to Backblaze B2 | Done — root cause fixed (object ACL), live probe added |
| 3 | What is slowing the system down | Done — measured, dashboard query count cut, 2 indexes added |
| 4 | Edit button on purchase orders | Done — hidden the moment a PO is approved, enforced server-side |
| 5 | Purchase-order line "quantity type" (pack/bag vs pieces) | Done — required field on create and edit |
| 6 | Purchase-order / receipt tables wrapping | Done — nowrap classifier + `.table-receipt` |
| 7 | Approval receipt/invoice upload | Done — upload field on approve, plus post-approval re-upload |
| 8 | Pieces recorded as pack/bag/crate | Done — base-unit contract + data migration |
| 9 | Optional receipt/invoice on expenses | Done — optional at creation, also on payment evidence |

---

## 1. Django admin coverage

Seven apps had models but no `admin.py`, so their tables were invisible to
staff. They now register:

`finance`, `inventory`, `pos`, `housekeeping`, `maintenance`,
`guest_services`, `staff_operations` — joining `accounts`, `bookings`,
`gallery`, `hotel`, `offers`, `reviews`, `rooms`, `stays`.

Design rules applied throughout:

- **Append-only tables are read-only in the admin.** Stock ledgers, financial
  transactions/lines/postings, cash movements, attendance and leave events,
  audit trails: a local `ReadOnlyAdmin` removes add/change/delete and the
  bulk-delete action. You can inspect them, never rewrite them — otherwise the
  ledgers stop reconciling.
- **Correction-friendly tables stay editable**: `Expense` (before posting),
  `PurchaseOrder` (with its line inline), `CashSession`, `Supplier`,
  `StockItem`, `StockRecipe`, `StockCount`, menu categories/items/modifiers,
  restaurant tables, kitchen tickets, `HousekeepingTask`,
  `MaintenanceWorkOrder`, `ServiceRequest`, `LaundryOrder`, `Department`,
  `StaffProfile`, shifts, attendance records, leave requests, disciplinary
  records, documents and performance reviews.
- FKs use `raw_id_fields` beyond a small page size, and every list has search,
  filters and a sane default ordering, so 82 changelists never do an unbounded
  `SELECT *`.

Verified by script: 82 registered changelists, all HTTP 200 as an ADMIN user,
0 failures.

## 2. Backblaze B2 uploads

**Root cause.** django-storages defaults `AWS_DEFAULT_ACL` to `public-read`,
and this project also passed it explicitly. Backblaze B2 supports canned ACLs
**at the bucket level only**: an object ACL that differs from the bucket's is
rejected with `400 InvalidArgument: Unsupported value for canned acl`. On a
private bucket — the common B2 default — that rejects *every* upload, and the
frontend only ever saw a generic failure.

**Fix** (`backend/config/settings/base.py`):

- `AWS_DEFAULT_ACL = None` — no ACL header at all, so each object inherits the
  bucket's own ACL, correct for both private and public buckets. Operators can
  still opt in with `BACKBLAZE_OBJECT_ACL=public-read` for a deliberately
  public bucket.
- `AWS_S3_CLIENT_CONFIG` pins `s3v4`, virtual addressing and
  `request_checksum_calculation="when_required"` (newer `boto3` sends
  `x-amz-checksum-*` by default, which older S3-compatible endpoints reject).
- Region derived from the endpoint; lowercase/CDN-safe `MEDIA_CUSTOM_DOMAIN`
  handling; `CacheControl: public, max-age=86400` on objects;
  `AWS_QUERYSTRING_AUTH=False` so URLs are plain, stable public URLs.
- `MEDIA_URL` is built from `MEDIA_CUSTOM_DOMAIN` when set, else the bucket's
  virtual-host endpoint, else `/media/` for local development.

**Verification tooling** — `python manage.py check_media_storage` prints the
backend, endpoint, region, bucket, ACL mode and `MEDIA_URL`, then writes, reads,
compares and deletes a probe object. `--skip-url-check` skips the anonymous
public-read check for private buckets; the anonymous check tells you
immediately whether a stored URL will actually open in a browser.

Uploads also pass through `apps/core/storage.py`, which raises
`StorageUploadError` (mapped to HTTP 502 `STORAGE_UPLOAD_FAILED` rather than a
500), verifies the object exists after write, and deletes quietly on rollback.

## 3. Performance

Measured on a seeded dev database as an ADMIN user (SQLite is noisy on wall
time, so query counts are the reliable signal):

| Endpoint | Before | After |
|---|---|---|
| `/api/admin/dashboard/` | 34 queries | **32 queries**, 10 KB payload |
| `/api/admin/capabilities/` | 1 query, 124 ms cold | unchanged (first-request cost) |

Changes in `apps/reports/services/dashboard.py`:

- The dirty-room count was a separate aggregate; it is now folded into the
  existing `room_stats` aggregate.
- `booking_exceptions` was two queries; it is one conditional aggregate
  (`overdue_checkins` = CONFIRMED and `check_in < today`, `late_checkouts` =
  CHECKED_IN and `check_out < today`).
- `_operational_alerts()` accepts the already-computed `dirty_rooms` and only
  queries when it is not supplied, so the dashboard never pays twice.

Index audit → migration `finance/0016_ledger_reporting_indexes`:

- `FinancialTransaction(-business_date, -created_at)` — the admin and reporting
  default ordering previously forced a full sort of the ledger.
- `FinancialLine(direction, account_code)` — the revenue-by-department report
  filters `direction=CREDIT` and scans `REVENUE.*` accounts.

Checked and **not** added, because the index already exists or no query needs
it: `Booking.created_at`, `PurchaseOrder.created_at` (both already indexed),
`Payment.created_at` (collections report filters on `status, paid_at`).

Largest remaining cost is the frontend, not the API: each dashboard page loads
~300 KB of shared JavaScript because there is no bundler and every page
re-serves the same assets. That is a build-tooling change (a bundle + hashed
filenames), not a query fix, and is left as a separate decision.

## 4. Purchase-order Edit button

`frontend/dashboard/procurement.html` gets an **Edit** action on each order and
a full edit dialog (header, lines, quantities, costs, notes, expected date).

- Visible only for `DRAFT` and `SUBMITTED`. It disappears as soon as the order
  is approved — no reload, no stale row.
- `PATCH /api/admin/inventory/purchase-orders/<reference>/` re-validates the
  replacement line set, rejects non-editable statuses, orders with any
  `GoodsReceipt`, or any `quantity_received > 0`, and keeps `currency`
  immutable.
- Emptied optional fields are sent explicitly (`notes: ""`,
  `supplier_reference: ""`, `expected_on: null`) so clearing a value in the
  dialog actually clears it on the server.

## 5. Quantity type on purchase-order lines

Every line in the create and edit dialogs now starts with a required
**Quantity type** selector:

- **Pieces** — "counted in *sachet*" — the quantity is stored as typed.
- **Pack / bag** — "holds 40 sachet" — offered only when the item declares
  `units_per_pack`; a second field asks for pieces per pack and the quantity and
  per-piece cost are derived.

The hint under the quantity field spells the conversion out before saving
("Records 200 sachet (pieces) on the order", "= ₦12.50 per sachet"), so the
person typing knows exactly what number to enter. If a pack line has no pack
size, the save is refused with a named error rather than guessing.

## 6. Table wrapping

`frontend/js/table-fit.js` classifies cells as atomic (references, dates,
quantities, money, statuses) or prose, and only prose is allowed to wrap.
Purchase-order tables, the record-receipt grid, goods-receipt tables and the
expense attachment table all use `.table-receipt`, whose header and body cells
are `white-space: nowrap` with horizontal scroll on the container. Wide tables
scroll sideways instead of breaking a reference across two lines.

## 7. Approval receipt / invoice upload

- Approving an order now opens a dialog with **Payment reference** and
  **Receipt / invoice** (PDF, JPG, PNG, WEBP; size capped by
  `MAX_DOCUMENT_UPLOAD_MB`). The approve call posts multipart, so approval and
  evidence land together.
- A receipt can also be attached *after* approval:
  `POST /api/admin/inventory/purchase-orders/<reference>/approval-receipt/`.
  Re-uploading replaces the file and deletes the old object.
- New model fields on `PurchaseOrder`: `payment_reference`,
  `approval_receipt`, `approval_receipt_uploaded_by`,
  `approval_receipt_uploaded_at`.
- The PO detail response exposes `approval_receipt_url` and
  `approval_receipt_uploaded_at`, and the order panel shows a link to open the
  file.

## 8. Pieces never recorded as pack/bag/crate

The rule is now an invariant, not a UI convention:

- **One base unit per stock item.** Order lines, receipts and the stock ledger
  store quantities in the item's base unit (pieces for counted goods). The API
  exposes `base_unit`, `units_per_pack` and `quantity_outstanding` so the
  client renders "pieces" everywhere.
- **Conversion happens exactly once, before the request.** `POST`/`PATCH` line
  payloads are always in base units; `quantity_ordered` on a PO is pieces from
  the first save, which is why an order still reads in pieces after it is
  received and after it is approved.
- **Record receipt** keeps its PACK / BAG / CRATE entry modes — a storekeeper
  may count in whatever the delivery arrived in — but the mode, pieces-per-pack
  and pack price are captured per line, converted to pieces, and previewed
  ("Posts to stock: 240 pieces") before submitting. Outstanding quantities are
  capped in pieces, so an over-receipt is impossible.
- **Data migration** `inventory/0010_normalize_pack_counted_items` converts
  existing pack-counted stock: quantity × pack size, and value ÷ pack size to
  keep total value unchanged. Items with no declared pack size are deliberately
  left untouched and reported in the migration output; the migration is
  one-way by design and has no guessed reverse.

## 9. Optional receipt/invoice on expenses

- `POST /api/admin/finance/expenses/` accepts multipart with an **optional**
  `receipt_file`. Nothing is required; an expense with no paperwork still
  posts.
- `Expense.receipt_file` + the expense detail UI show the attachment, and
  `/expenses/<reference>/payment-evidence/` accepts evidence when a payable is
  settled later.
- Same validation as other documents (extension, size, real PDF/image check),
  and the same 502 `STORAGE_UPLOAD_FAILED` behaviour if B2 rejects the object.

---

## Migrations to apply

```bash
python manage.py migrate          # applies, in order:
#   inventory/0009_purchaseorder_approval_receipt_and_more
#   inventory/0010_normalize_pack_counted_items   (data migration, one-way)
#   finance/0015_expense_receipt_file
#   finance/0016_ledger_reporting_indexes
#   hotel/0008_alter_hotelsettings_front_desk_desktop_ip
```

`makemigrations --check` reports no pending changes.

## Verification

```bash
cd backend && .venv/bin/python manage.py test        # full suite
cd frontend && node --test tests-js/                 # 484 tests, 0 failures
cd backend && .venv/bin/python manage.py check_media_storage   # live B2 probe
```

Frontend suite: 484 tests, 483 pass, 1 skipped, 0 fail — including
`test-procurement-editing-ui.js` (12 tests) which pins the Edit-button gating,
the pack→piece conversion, the approval multipart and the nowrap tables.

## Open items needing a human, not code

1. **Live B2 check.** No production credentials in this environment. Run
   `check_media_storage` against the real bucket; a failure there now names the
   stage (config, write, read-back, or anonymous public read).
2. **Payroll rule `RULE-20261017-94078BA455D2`** is seeded `PENDING_REVIEW` and
   needs approval from a second staff member holding `payroll.rules.review` —
   the maker-checker rule prevents the author from approving their own rule.

---

# Follow-up (same day): room guests in multi-room bookings

Request: *"guests that booked many rooms should be able to attach emails of
those in the other room(s), so they can make a request for something, and so we
have a proper record of whether they actually checked in in all the rooms they
booked."*

## What was built

**`RoomOccupant`** (`apps/bookings`, migration `0005`) — one row per named guest
per reserved room: name, **email**, phone, lead-guest flag, and
`portal_invited_at`. Emails are stored lowercase so the portal lookup is exact.

**Per-room check-in** on `BookingRoom`: `checked_in_at`, `checked_in_by`,
`not_arrived_at`, `arrival_note`, plus a derived `arrival_status`
(EXPECTED / AWAITING_ARRIVAL / CHECKED_IN / CHECKED_OUT / DID_NOT_ARRIVE).
Migration `0006` backfills existing in-house bookings from their booking-level
timestamp so no legacy stay looks unoccupied.

**One booking, several arrivals.** `POST …/rooms/<assignment_id>/check-in/`
hands over a single room: the first room opens the Stay and folio, later rooms
append their own `StayRoom` evidence, and the booking-level **Check in** still
completes every remaining room at once. Checkout records rooms nobody arrived in
as `DID_NOT_ARRIVE` and does **not** mark them dirty for housekeeping.

**Guest-facing capture.** `booking-review.html` asks for a name + email per
*additional* room (room 1 is the booker), sends `room_guests` with the booking,
and shows them on the review step. The desk can edit the same list at any time
before the booking closes (**Booking → Rooms & guests**).

**Portal identity.** A recorded room guest signs in with their own email
(`request_access_challenge` now accepts `RoomOccupant` as well as `Guest`), sees
the booking, is told which room is theirs, and can raise requests **for their
own room only** — the request names them (`ServiceRequest.occupant`) while still
belonging to the booking for billing. They receive a portal note when their room
is checked in.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/api/admin/bookings/<ref>/room-guests/` | Replace the per-room guest list (draft/confirmed/in-house only) |
| `POST` | `/api/admin/bookings/<ref>/rooms/<assignment_id>/check-in/` | Hand over one room |
| `POST` | `/api/bookings/` | now accepts `room_guests: [{email, full_name?, room_index?}]` |

Booking detail adds `rooms_checked_in`, `rooms_awaiting_arrival`,
`rooms_not_arrived`, and per-room `checked_in_at`, `arrival_status`,
`checked_in_by_name`, `occupants`.

## Migrations

```bash
python manage.py migrate
#   bookings/0005_bookingroom_arrival_note_bookingroom_checked_in_at_and_more
#   bookings/0006_backfill_room_check_ins   (data migration, deliberately one-way)
#   guest_services/0007_servicerequest_occupant
```

## Reporting

- **Arrivals queue** rows now carry `rooms_checked_in` / `rooms_awaiting_arrival`
  (`_with_room_arrival_counts()` — one annotated query for the whole page, pinned
  by a test that fails if the list grows a query per row). Multi-room rows show
  *“1 of 3 room(s) in house · 2 awaiting arrival”* and link straight to the room
  panel.
- **Occupancy report** keeps the commercial definition (`occupied_rooms` = rooms
  sold per night) and adds `arrived_rooms`, `unarrived_rooms`,
  `arrived_room_nights`, `unarrived_room_nights`, `average_arrival_percent` per
  day and for the period, plus a `definitions` block. Excel and PDF exports
  carry the same columns. A room that closed without a guest is counted as
  *never arrived* — never as occupied.

## Verification

- `backend/tests/test_room_guests_and_per_room_checkin.py` — **35 tests**: guest
  capture, validation, replace semantics, portal identity and room scoping,
  partial arrivals, checkout of an unarrived room, legacy whole-booking check-in,
  list-row counts (with a per-row query-count guard) and occupancy arrivals.
- `frontend/tests-js/test-room-guests-ui.js` — **17 tests** pinning the
  additional-room rows, payload shape, per-room gating and the two endpoints.
- **Full backend suite: 1431 tests, OK** (15 skipped, ~8 min); `makemigrations
  --check` → "No changes detected".
- **Frontend:** `node --test tests-js/` → **511 tests, 510 pass, 1 skipped, 0 fail**.
- **Django admin:** 83 registered models, **0 failing changelists** as an ADMIN
  user; the new `RoomOccupant` register is searchable by email in one place.

---

# Follow-up (same day): "online bookings fail at payment"

Symptom: `POST /api/payments/initialize/` → **404** (95-byte body) on the live
site, after the guest submitted an online booking.

## What that 404 actually is

The response body in the log is the API's own envelope — 95 bytes is exactly
`{"success":false,"code":"RESOURCE_NOT_FOUND","message":"The requested resource was not found."}`
— so the route exists and the view ran. `InitializePaymentView` raises 404 in
two cases, and it must keep doing so (a 32-bit booking reference is not a
secret, so "no such booking" and "not your booking" have to look identical):

1. no booking matches the reference;
2. the booking exists but the caller presented no valid credential.

Reproducing both against a live server: with the guest token the request reaches
the gateway (503 `PAYMENT_NOT_CONFIGURED` here, because the dev `.env` has no
Paystack key); **without** it, the exact 95-byte 404 from the report.

## Root causes fixed

- **Emailed links carried no usable credential.** `guest_booking_link()` put only
  `?ref=` in every email; the raw token exists just once, in the create
  response, so any later email link was useless on any device — and the link
  target, `my-bookings.html`, ignored `?access=` entirely (and spun forever when
  it had a reference but no token). Links now carry a signed `access=` value
  (bound to the reference, 14-day expiry) that both pages exchange for a real
  session token.
- **The token was read once, at page load.** A credential written later (by the
  link exchange, a second tab, or the payment-return page) was ignored. The
  confirmation page now resolves it at call time from every place it is stored.
- **No recovery path.** The failure screen now offers **Email me a secure link**
  (`POST /api/bookings/<ref>/access-link/`, non-enumerating, capped per booking).
- **A request that could only 404 was still sent.** With no guest token and no
  signed-in owner, the page now says so and offers the link instead.
- **The 404 could not be diagnosed.** Both refusal paths now log the reason, the
  booking status, whether a guest token was presented, and the client address —
  the response stays exactly the same.

## New API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/bookings/<ref>/access/` | Exchange the signed `access` value for a guest token |
| `POST` | `/api/bookings/<ref>/access-link/` | Email a fresh secure link (always 200) |

Migration: `notifications/0015_alter_emaillog_kind` (adds the
`BOOKING_ACCESS_LINK` email kind so the delivery log can filter it).

## Verification

- `backend/tests/test_guest_access_links.py` — **14 tests**: link is signed,
  exchange works, tampered/expired/cross-booking values are rejected, an
  exchanged token can start a payment, resend is non-enumerating, emails the
  reservation address and is capped, and both refusal reasons are logged.
- `frontend/tests-js/test-booking-access-links.js` — **10 tests**.
- **Full backend suite: 1431 tests, OK** (15 skipped); `makemigrations --check` clean.
- **Frontend:** `node --test tests-js/` → **511 tests, 510 pass, 1 skipped, 0 fail**.
- Live reproduction against a running server: a fresh device holding only the
  emailed link reaches the payment gateway; a request with no credential still
  returns the same 404, now with the reason in the log.

# Follow-up (same day): front desk sign-in refused — "the address changes on every refresh"

Symptom: front desk staff could not sign in. The refusal was
`STAFF_SIGN_IN_RESTRICTED` and named **`100.64.0.18`**. An administrator saved
that value in Settings, tried again, and the reported address had changed. No
value ever worked.

## What that address is

`100.64.0.18` is in `100.64.0.0/10`, the RFC 6598 *shared address space* used
for carrier-grade NAT. It is **not** the desk. It is the address Railway's edge
proxy uses to reach the container, and it is a different proxy instance on
nearly every request — which is exactly why the value moved on every refresh.

It appeared in the refusal because it was the only address the server could
establish: `REMOTE_ADDR`. The forwarding headers that carried the visitor's real
address were being ignored, because the peer (`100.64.0.x`) was **not in the
trusted-proxy list**. On top of that, `X-Real-IP` — the header Railway's edge
actually rewrites — was **not in the trusted-header list**, so even a correctly
identified proxy would have had nothing to read.

## The five defects

| # | Defect | Effect |
|---|---|---|
| 1 | `100.64.0.0/10` was not a trusted proxy range | Every request looked like it came from the platform; forwarding headers were discarded |
| 2 | `x-real-ip` was not a trusted edge header | Railway's rewritten client address was never read, even from a trusted proxy |
| 3 | A single `REMOTE_ADDR` (or an empty chain) short-circuited the resolver before the edge header was consulted | An edge header **on its own** was ignored; only a request carrying `X-Forwarded-For` resolved correctly |
| 4 | The setting accepted **one exact address** | A desk whose provider renumbers it could never stay registered; no way to say "this network" |
| 5 | Nothing said the reported address was unusable | The UI invited the administrator to save the proxy address, which can never match — the circle the report describes |

Defect 3 was found by the live end-to-end run, not by the unit tests: a request
with `X-Real-IP` and **no** `X-Forwarded-For` still resolved to the proxy.
Railway does not guarantee both headers, so this would have left the fix
ineffective in production.

## What changed

| File | Change |
|---|---|
| `apps/core/client_ip.py` | `100.64.0.0/10` added to the trusted defaults; `x-real-ip` added to the default edge headers; the resolver now consults the edge header **before** looking at `X-Forwarded-For` and no longer bails out when the chain is empty; new `is_infrastructure_address()` / `is_infrastructure_network()` |
| `config/settings/base.py` | Same two defaults, with the reasoning written down |
| `apps/hotel/models.py` + `hotel/0008` | `front_desk_desktop_ip` now stores a canonical comma-separated list of addresses and networks (`max_length` 255) |
| `apps/hotel/serializers.py` | Validates and canonicalises that list; rejects hostnames and garbage |
| `apps/accounts/desktop_policy.py` | `configured_front_desk_desktop_networks()`; `front_desk_desktop_ip_allowed()` matches **any** configured network; `unusable_saved_desktop_addresses()` finds saved values that no public browser can ever match |
| `apps/core/exceptions.py` | `STAFF_SIGN_IN_RESTRICTED` errors may carry `client_ip_is_infrastructure`, `configured_address_unusable` and `configured_front_desk_address` |
| `apps/accounts/views.py` | `GET /api/auth/client-address/` also returns `client_ip_is_infrastructure` |
| `apps/hotel/views_admin.py` | Settings payload also returns `request_ip_is_infrastructure` and `front_desk_desktop_ip_unusable` |
| `frontend/dashboard/settings.html` | Field relabelled "address or network"; warning when the saved value can never match; "Use this address" disabled when the server reports the platform's own proxy |
| `frontend/dashboard/terminals.html` | "Register this address" refuses the platform's proxy address and explains why |
| `frontend/login.html` | Refusal text distinguishes three cases: unusable address, stale saved value, genuine mismatch |

## What an administrator does after deploying

1. **Redeploy the backend.** No new environment variables are required: the
   defaults now cover Railway. If `TRUSTED_PROXY_CIDRS` is set explicitly on
   Railway, it **must** include `100.64.0.0/10`; if `TRUSTED_CLIENT_IP_HEADERS`
   is set, it **must** include `x-real-ip`. Deleting either variable restores
   the defaults.
2. Open **Settings → Front Desk desktop** *on the desk PC*. The value under
   "IP address seen by the server for this browser" is now the desk's public
   address and stops changing.
3. Replace the old `100.64.x` value. The page warns that it can never match;
   "Use this address for Front Desk desktop" now copies the real one.
4. If the ISP keeps renumbering the desk, save its **network** (`102.89.34.0/24`)
   instead of one address. Several entries may be comma-separated (office link
   plus a backup).

**If the address still is not the desk's**, the API is being reached through
another proxy (this deployment serves the site from Vercel, whose rewrite
forwards `/api/*` to Railway). In that case the address the API can establish is
the fronting proxy's, not the desk's, and the reliable route is to enrol the
desk browser once: **Workstations → Issue desktop key → paste once on the desk
PC**. Staff then sign in with email and password alone, and the address binding
is not involved.

## Verification

- `backend/tests/test_receptionist_desktop_ip.py` — **50 tests**, including:
  peer `100.64.0.18` + `X-Real-IP` resolves to the desk; a forged `X-Real-IP`
  from an untrusted peer is ignored; an appended chain still yields the client;
  network and multi-entry bindings match addresses inside them; an
  infrastructure address is flagged in both the 403 and the settings payload; a
  stale saved value is named in the refusal and in Settings; a private LAN desk
  binding is **not** called broken.
- `backend/tests/test_front_desk_desktop_ip.py` — **11 tests**, including the
  settings round-trip for a `/24`, a comma-separated list, and the rejection of
  malformed entries and hostnames.
- `frontend/tests-js/test-desktop-address-ui.js` — **17 tests**, including the
  "never offer the platform's proxy for registration" guard and the three
  login refusal messages.
- Live end-to-end against a running server (real HTTP, not the test client):
  - `X-Real-IP: 102.89.34.7` **alone** (no `X-Forwarded-For`) → `200`;
  - `X-Forwarded-For: 102.89.34.7` alone → `200`;
  - a different address → `403` naming the true address, with no
    infrastructure flag;
  - with `100.64.0.18` saved in Settings → `403` carrying
    `configured_address_unusable: true` and `configured_front_desk_address:
    "100.64.0.18"`; clearing it restores sign-in.
- Migration `hotel/0008_alter_hotelsettings_front_desk_desktop_ip` applied;
  `makemigrations --check` clean; `manage.py check` 0 issues.
- **Full backend suite: 1451 tests, OK** (15 skipped).
- **Frontend: `node --test tests-js/` → 514 tests, 513 pass, 1 skipped, 0 fail.**
