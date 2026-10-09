# J-ONE HOTEL & LODGE — API Reference

Base URL: `https://<host>/api/` · Format: JSON · Auth: JWT endpoints use `Authorization: Bearer <access_token>`; verified portal endpoints use `X-Portal-Session`

Live interactive version: **`/api/docs/`** (Swagger UI, generated from code — it can never drift).

**User-facing request/response exactness lives in [`FRONTEND_CONTRACT.md`](FRONTEND_CONTRACT.md).**
This file is the complete endpoint inventory with permissions and behaviors.

Legend — 🔓 public · 🔑 any authenticated user · 🛎 staff (any role) · 🧰 manager+admin · 👑 admin only

## Response envelope (every endpoint)

```jsonc
// success (object)
{ "success": true, "message": "...", "data": { } }

// success (paginated list)
{ "success": true, "message": "...", "data": [ ],
  "pagination": { "count": 100, "page": 1, "page_size": 20,
                  "total_pages": 5, "next": "...", "previous": null } }

// error
{ "success": false, "code": "MACHINE_CODE", "message": "Human message.",
  "errors": { "field": ["problem"] } }
```

Standard codes: `VALIDATION_ERROR` (400) · `UNAUTHORIZED` (401) · `FORBIDDEN` (403) ·
`RESOURCE_NOT_FOUND` (404) · `RATE_LIMITED` (429) · `SERVER_ERROR` (500) plus
domain codes: `INVALID_DATES`, `CAPACITY_EXCEEDED`, `ROOM_UNAVAILABLE` (409),
`BOOKING_EXPIRED` (409), `INVALID_BOOKING_STATE` (409), `CANCELLATION_NOT_ALLOWED`,
`OFFER_NOT_APPLICABLE`, `OUTSTANDING_BALANCE`, `TABLE_SESSION_CONFLICT` (409), `PAYMENT_NOT_CONFIGURED` (503),
`PAYMENT_FAILED`, `PAYMENT_ALREADY_COMPLETED` (409), `PAYMENT_AMOUNT_MISMATCH`,
`PAYMENT_GATEWAY_ERROR` (502).

Conventions: dates `YYYY-MM-DD` · datetimes ISO 8601 · money as strings
(`"25000.00"`) · IDs are integers · references are strings
(`J1-YYYYMMDD-XXXXXXXX`, payments `J1P-…`). Pagination params everywhere:
`page`, `page_size` (max 100). Every `/api/` response is `private, no-store`.
Authenticated staff requests may include `X-JONE-Terminal: <registered-reference>`
for best-effort workstation attribution; an absent/unknown/inactive terminal never
grants or denies account permissions. The server assigns an `X-Request-ID` and
captures actor role, department, and terminal snapshots in audit evidence where
available. Do not use a workstation reference as an account credential.

---

## Meta

| Method & path | Auth | Description |
|---|---|---|
| `GET /api/` | 🔓 | API index |
| `GET /api/health/live/` | 🔓 | Process liveness, no dependency check: `{ "success": true, "status": "ok" }` |
| `GET /api/health/ready/` | 🔓 | Database readiness: `{ "success": true, "status": "ok", "database": "up" }`; returns 503 when DB is down |
| `GET /api/health/` | 🔓 | Backwards-compatible alias of `/api/health/ready/` |
| `GET /api/schema/` | 🔓 | OpenAPI 3 schema |
| `GET /api/docs/` | 🔓 | Swagger UI |

## Auth (`/api/auth/`)

| Method & path | Auth | Body → Returns | Notes |
|---|---|---|---|
| `POST /register/` | 🔓 | email, first_name, last_name, phone?, password, password_confirm → `{user, tokens}` | Role is always `GUEST`. Throttled. |
| `POST /login/` | 🔓 | email, password → `{user, tokens}` | `UNAUTHORIZED` on bad credentials. 5/min/IP. |
| `POST /token/refresh/` | 🔓 | refresh → `{tokens.access(, refresh)}` | Rotation: old refresh is blacklisted. |
| `POST /logout/` | 🔑 | refresh | Blacklists the refresh token. |
| `GET /profile/` | 🔑 | → user | |
| `GET /capabilities/` | 🔑 staff | Server-resolved role capability codes for current-user UI hints; the API still enforces every request. |
| `PATCH /profile/` | 🔑 | first_name?, last_name?, phone?, profile_image? → user | Role/active flags NOT editable. |
| `POST /password/change/` | 🔑 | current_password, new_password, new_password_confirm | |
| `POST /password/reset/` | 🔓 | email | Generic response (no account-existence leak). |
| `POST /password/reset/confirm/` | 🔓 | uid, token, new_password, new_password_confirm | Throttled. |

## Public content

| Method & path | Auth | Query/Notes |
|---|---|---|
| `GET /api/hotel/` | 🔓 | Hotel identity, contact, times, currency, stay rules, maps URL, social links |
| `GET /api/hotel/policies/` | 🔓 | Active policies (array) |
| `GET /api/facilities/` | 🔓 | Active facilities (array, `icon` identifiers) |
| `GET /api/gallery/` | 🔓 | `?category=HOTEL\|ROOMS\|RESTAURANT\|FACILITIES\|EVENTS\|EXTERIOR\|OTHER` — paginated `{categories, items}` |
| `GET /api/offers/` | 🔓 | Currently-running offers |
| `GET /api/rooms/` | 🔓 | Room-type catalog (lightweight rows) |
| `GET /api/rooms/<slug or id>/` | 🔓 | Room-type detail: images, amenities, applicable offers |
| `GET /api/rooms/availability/?check_in&check_out&guests&rooms&room_type` | 🔓 | **Authoritative availability + live pricing**. Throttled 240/h. |
| `GET /api/rooms/<slug or id>/unavailable-dates/?start_date&end_date` or `?days=N` | 🔓 | **Per-date calendar inventory for one room type** (powers the booking calendar). `end_date` is exclusive (check-out semantics); legacy `days=N` defaults to a today+365 window. Max span 366 days. Throttled 240/h. |
| `POST /api/enquiries/` | 🔓 | General enquiry or structured cancellation/refund request. Cancellation body includes `enquiry_type=CANCELLATION`, `booking_reference`, optional payment/receipt refs, reason/contact fields. Returns `cancellation_reference` + secure `status_url`; does **not** cancel the booking. Throttled 10/h. |
| `GET /api/enquiries/cancellation-status/<reference>/?token=...` | 🔓 + token | Safe public status page payload. Never says refund complete until Paystack confirms `processed`. |
| `POST /api/reviews/verify/` | 🔓 verified | `{booking_reference, email}` (or owner JWT / `X-Guest-Access-Token`) → safe eligible-stay summary for the review page. The reference alone is never enough; every failure is a uniform `404`. Throttled 20/h. |
| `POST /api/reviews/` | 🔓 verified | Same verification + `{rating 1–5, comment 10–2000 chars}` → 201. One review per booking (DB-enforced, `409 ALREADY_REVIEWED`); only `CHECKED_OUT` stays are eligible (`409 STAY_NOT_COMPLETED`). HTML is stripped; honeypot `website` field silently drops bots. Throttled 5/h. **There is no public review listing endpoint** — submitted reviews are private to hotel management. |

## Guest bookings (`/api/bookings/`)

| Method & path | Auth | Body → Returns |
|---|---|---|
| `POST /quote/` | 🔓 | room_type, check_in, check_out, rooms?, adults?, children?, offer_code? → full price breakdown + policies + hold info (nothing persisted) |
| `GET /` | 🔑 | my bookings (paginated; `?status=`) |
| `POST /` | 🔓 guest / 🔑 optional | stay fields + `guest{}`? + special_requests? → 201 booking (PENDING, inventory held, `expires_at` set). Throttled. |
| `GET /<id or reference>/` | 🔑 owner/token; staff `booking.read` or `booking.manage` | full booking detail with `can_pay`; public `can_cancel` is always false |
| `POST /<id or ref>/cancel/` | 🔑 owner/token; staff `booking.read` or `booking.manage` | Legacy non-destructive endpoint: returns `CANCELLATION_NOT_ALLOWED`; guests must submit Contact cancellation/refund requests. |
| `GET /<id or ref>/receipt/` | 🔑 owner/token; staff `booking.read` or `booking.manage` | hotel + guest + stay + payment lines receipt |

Ownership and staff capability are enforced **object-level** — another guest's
booking reference/id returns `404` to avoid confirming that the object exists.
Staff status by itself is not a booking-access grant; cashier, housekeeping,
maintenance, and finance roles without a booking capability cannot use these
routes to read full booking records.

The staff finance workspace uses a separate receipt projection:
`GET /api/admin/bookings/<id or ref>/receipt/` requires `payment.read` and does
not grant access to booking detail/actions. Sending a receipt uses
`POST /api/admin/bookings/<id or ref>/send-receipt/` and requires
`payment.receipt.send`.

## Payments (`/api/payments/`)

| Method & path | Auth | Notes |
|---|---|---|
| `POST /initialize/` | guest token or owner JWT; staff requires `booking.manage` + `payment.capture` | `{booking_reference}` → `{reference, authorization_url, amount, currency}`. Amount is computed server-side; retries reuse the pending attempt. |
| `GET /verify/<reference>/` | guest token or owner JWT; staff requires `booking.manage` + `payment.capture` | Server verifies directly with Paystack; **idempotent**; confirms booking on success. |
| `POST /webhook/` | 🔓 signed | Paystack → `x-paystack-signature` HMAC-SHA512 validated before parsing. Handles `charge.success` plus `refund.pending/processing/processed/failed/needs-attention` idempotently. |

### Staff payment ledger (`/api/admin/payments/`)

`GET /` and `GET /<id|reference>/`, plus read-only `GET /refunds/` and
`GET /refunds/<id>/`, require `payment.read`. Read access is seeded for front
desk/management and accounting roles. `POST /record/` requires both
`booking.manage` and `payment.capture`; a capture capability alone (for example
on a cashier/POS role) is not enough to charge a room booking. Manual amounts
remain server-validated against the locked booking balance.

## Notifications (`/api/notifications/`)

| Method & path | Auth | Notes |
|---|---|---|
| `GET /` | 🔑 | `{unread_count, notifications[]}` paginated (`?unread=true`) |
| `GET /unread-count/` | 🔑 | `{unread_count}` |
| `POST /<id>/read/` · `POST /read-all/` | 🔑 | returns `{unread_count}` |

---

## Verified-email Guest Portal (`/api/portal/`)

Portal sessions are short-lived opaque credentials sent only in `X-Portal-Session`.
They are verified-email scoped and are never interchangeable with a booking access
token or staff JWT.

| Method & path | Access | Description |
|---|---|---|
| `POST /auth/request/` · `POST /auth/consume/` · `POST /auth/logout/` | Public / portal session | Non-enumerating magic-link request, one-use consume, and revocation |
| `GET /me/` | Portal session | Bounded booking overview owned by the verified email |
| `GET /folios/` · `GET /folios/<ref>/` · `postings/` | Portal session | Paginated authoritative folios and statement items owned by the verified email |
| `GET/POST /requests/` | Portal session | Paginated owned guest-service requests; creates only against an in-house owned stay, with scoped idempotency |
| `GET /requests/<ref>/` | Portal session | Request detail plus guest-visible events only |
| `POST /requests/<ref>/comments/` · `cancel/` | Portal session | Guest-visible comment or eligible open/acknowledged-request cancellation |

---

## Guest-service QR (`/api/service-qr/`)

Printed codes use a high-entropy bearer stored only as a SHA-256 digest. The
raw token is embedded in the **URL fragment** (not sent to the web server in the
page request), removed from the address bar at page boot, and sent to Django
only as `X-Service-QR-Token`. Staff issuance and rotation responses contain the
printable SVG and bearer URL once and are explicitly `no-store`; never log or
persist those raw values. Table QR submissions remain anonymous and accept no menu,
price, payment, or POS-order fields. Authorized staff can launch a linked,
server-priced POS draft from a table F&B request; this never auto-submits or
charges an order. Restaurant drafts must match an open registered table session;
bar drafts preserve the request's free-text table/pickup label.

| Method & path | Access | Description |
|---|---|---|
| `GET /context/` | Public QR bearer header | No-store room/table label and permitted request categories |
| `POST /requests/` | Public QR bearer header + idempotency key | Creates a routed guest-service request. A room link requires exactly one current in-house room occupant; a table link forces a guestless Food & Beverage request. |

QR submissions accept only `category`, `summary`, `detail`, and `idempotency_key`; the caller cannot supply guest, room, stay, team, priority, staff assignee, menu lines, or financial/POS values. Room requests derive the guest, stay, and room server-side; table requests remain anonymous. For restaurant session handoff, issue a table QR using the exact registered table code; free-text bar/pickup labels remain supported. Both reuse the normal event log, SLA and staff team queue.

---

## Staff API (`/api/admin/`)

Legacy staff endpoints retain their established `ADMIN`/`MANAGER`/`RECEPTIONIST`
role rules. New operational endpoints below use the named server-side capability
shown in the middle column, including additive operational roles.

| Method & path | | Description |
|---|---|---|
| `GET /dashboard/` | 🛎 | Aggregated dashboard (financial block only for managers/admins) |
| `GET/POST /bookings/` | 🛎 | List (filter: status, payment_status, source, room_type, date_from, date_to, check_in, check_out, search, ordering) · manual booking create |
| `GET/PATCH /bookings/<id\|ref>/` | 🛎 | Detail / modify (dates, rooms, guests, notes — re-validates availability & reprices) |
| `POST /bookings/<id\|ref>/confirm/` | 🛎 | Confirm without online payment (pay at hotel) |
| `POST /bookings/<id\|ref>/cancel/` | 🛎 | Staff cancel (audited; refund remains separate) |
| `POST /bookings/<id\|ref>/check-in/` ↪ `check-out/` ↪ `no-show/` `assign-room/` | 🛎 | Front-desk actions (checkout guards `OUTSTANDING_BALANCE` unless `allow_balance_due`) |
| `GET /guests/` · `GET/PATCH /guests/<id>/` | 🛎 | Guest CRM + stay history |
| `GET /payments/` · `GET /payments/<id\|ref>/` | 🛎 | Payment records (sanitized) |
| `GET/POST /service-requests/` · `GET /service-requests/<ref>/` | 🔐 `guest_request.manage` | Paginated team-scoped queue, request creation, full staff event timeline, and linked POS-order reference when present |
| `POST /service-requests/<ref>/assign/` | 🔐 `guest_request.assign` | Dispatcher-only assignment, re-routing, and SLA due-time updates |
| `POST /service-requests/<ref>/claim/` · `status/` · `comments/` | 🔐 `guest_request.manage` | Team-worker claim, controlled progression, and immutable internal/guest-visible comments |
| `POST /service-requests/<ref>/housekeeping-task/` · `maintenance-work-order/` | 🔐 source + task capability | Idempotently route a room-linked service request into its one operational task/work order |
| `GET/POST /service-qr-links/` | 🔐 `service_qr.manage` | List links or issue/reactivate a room/table QR. Raw token, fragment URL, and SVG are returned only in this no-store issuance response. |
| `POST /service-qr-links/<ref>/rotate/` · `revoke/` | 🔐 `service_qr.manage` | Rotation immediately invalidates prior prints; revocation retains the link and request history. |
| `GET/POST /housekeeping/` · `GET /housekeeping/<ref>/` | 🔐 `housekeeping.task.manage` | Paginated role-scoped room-readiness queue and immutable task timeline |
| `POST /housekeeping/<ref>/assign/` · `claim/` · `status/` | 🔐 dispatch / task capability | Dispatcher assignment, housekeeper claim, controlled clean/inspect lifecycle; only inspected completion marks a room clean |
| `GET/POST /maintenance/` · `GET /maintenance/<ref>/` | 🔐 `maintenance.work_order.manage` | Paginated role-scoped work-order queue and immutable timeline |
| `POST /maintenance/<ref>/assign/` · `claim/` · `status/` | 🔐 dispatch / work-order capability | Controlled triage, technician work, and manager verification |
| `POST /maintenance/<ref>/outage/start/` · `outage/clear/` | 🔐 `maintenance.work_order.assign` | Explicit availability-impacting room outage; an active outage blocks verified completion |
| `GET /finance/folios/` · `GET /finance/folios/<ref>/postings/` | 🔐 `folio.view` | Paginated folios + database-computed balances and immutable statement items |
| `GET /finance/transactions/` · `GET /finance/transactions/<ref>/` | 🔐 `reports.financial.view` | Paginated immutable ledger evidence / balanced lines |
| `GET /finance/reports/summary/?start=YYYY-MM-DD&end=YYYY-MM-DD&currency=NGN` | 🔐 `reports.financial.view` | Bounded, ledger-authoritative business-date report that keeps guest charges, recognized revenue, collections, refunds, expenses, inventory acquisitions, and cash paid-outs distinct. |
| `GET/POST /finance/control-policies/` | 🔐 `finance.controls.manage` | Effective-dated refund, discount, cash-variance, and expense approval thresholds (create successor; no historical edit) |
| `GET /finance/approvals/` · `POST /finance/approvals/<ref>/review/` | 🔐 `payment.refund.approve` | Maker-checker approval evidence |
| `GET /pos/menu/` · `GET/POST /pos/orders/` | 🔐 POS / `restaurant.order.manage` / `bar.order.manage` | Available menu; bounded department-scoped orders with server price snapshots and idempotency. Restaurant dine-in orders may join an open registered table session; the session's table code is authoritative. Optional `service_request_reference` requires both the relevant POS-mode capability and `guest_request.manage`, and links one eligible table F&B QR request to one draft. Restaurant mode verifies the QR label equals the registered table code and requires that table's open session; bar mode retains the free-text location. The request remains open and no financial charge is posted until the normal POS delivery workflow. |
| `GET/POST /pos/restaurant-tables/` · `PATCH /pos/restaurant-tables/<id>/` | 🔐 `restaurant.order.manage` read · `restaurant.table.manage` write | Bounded table register (`active=true|false|all`, `search`); tables deactivate instead of delete. Table identity/layout freezes after its first service session; activation can still change after closure. |
| `GET/POST /pos/restaurant-table-sessions/` · `GET /pos/restaurant-table-sessions/<ref>/` · `POST .../<ref>/close/` | 🔐 `restaurant.order.manage` | Open one idempotent table service session, attach multiple checks, and close only after orders are terminal and delivered orders are fully settled. A portable unique active-table key prevents two open sessions for one table. |
| `GET/POST /pos/menu/categories/` · `items/` · `modifiers/` | 🔐 matching menu capability | Controlled catalog management; restaurant and bar menu permissions are separate from general POS administration. |
| `POST /pos/orders/<ref>/submit/` · `status/` · `tenders/` | 🔐 POS/department + payment capability | Kitchen/bar ticket routing, delivered room charge, or direct tender collection; all prices, folio links, table-session state, and ledger effects are server-validated. Drafts can be cancelled with retained event evidence and no charge. |
| `GET /pos/kitchen-tickets/` · `POST /pos/kitchen-tickets/<id>/status/` | 🔐 `kitchen.queue.view` / `kitchen.ticket.manage` or matching order capability | Station-filtered kitchen/bar production queue and controlled ticket lifecycle; staff see only permitted stations. |
| `GET/POST /inventory/locations/` · `PATCH /inventory/locations/<id>/` | 🔐 `inventory.manage` | Bounded stock-location catalog |
| `GET/POST /inventory/items/` · `PATCH /inventory/items/<id>/` | 🔐 `inventory.manage` | Bounded stock-item catalog; source-of-truth stock is never edited here |
| `GET /inventory/balances/` · `GET /inventory/movements/` | 🔐 `inventory.manage` | Paginated balance projection and immutable stock ledger; movement dates use hotel-local half-open timestamp ranges |
| `POST /inventory/issues/` | 🔐 `inventory.manage` | Source-keyed controlled ISSUE or MAINTENANCE_ISSUE; rejects negative balances |
| `GET/POST /inventory/recipes/` · `POST /inventory/recipes/<ref>/retire/` | 🔐 `inventory.manage` | Versioned POS menu BOM mapping or explicit `NO_STOCK` mapping. Creating a version retires the preceding active recipe; recipes are not edited in place. |
| `GET /inventory/consumption-requests/` · `POST /inventory/consumption-requests/<ref>/process/` | 🔐 `inventory.manage` | Durable POS-to-inventory hand-off queue. Processing resolves the frozen recipe snapshot and atomically appends idempotent `POS_CONSUMPTION` movements; missing/invalid recipes remain reviewable and deduct nothing. |
| `GET/POST /inventory/stock-counts/` · `GET /inventory/stock-counts/<ref>/` · `POST .../<ref>/submit/` | 🔐 `inventory.manage` | Frozen item/location count snapshot and bounded count submission. A zero-variance count posts without adjustment; a variance waits for maker-checker approval. |
| `POST /inventory/stock-counts/<ref>/approve/` | 🔐 `inventory.adjust.approve` | Independent approver posts source-keyed `ADJUSTMENT` movements only if stock has not changed since the snapshot. Initiators cannot approve their own count. |
| `GET/POST /inventory/suppliers/` · `PATCH /inventory/suppliers/<id>/` | 🔐 `procurement.manage` | Bounded supplier register |
| `GET/POST /inventory/purchase-orders/` · `GET /inventory/purchase-orders/<ref>/` | 🔐 `procurement.manage` | Purchase-order drafts and immutable line snapshots |
| `POST /inventory/purchase-orders/<ref>/submit/` · `ordered/` · `receipts/` | 🔐 `procurement.manage` | PO lifecycle and source-keyed partial/full goods receipts; receipts append `RECEIPT` movements and reject over-receipt |
| `POST /inventory/purchase-orders/<ref>/approve/` | 🔐 `procurement.approve` | Separate maker-checker approval; requester self-approval is rejected |
| `GET/POST /users/terminals/` · `PATCH /users/terminals/<id>/` | 🔐 `terminal.manage` | Register, label, list (`active`, `department`, `search`), or deactivate optional workstation metadata; current-user presence is informational, not a session lock. |
| `GET /users/directory/` | 🔐 dispatch/profile/shift/`payroll.manage` capability | Minimal active-staff picker projection without account-management data; supports employee-code search for authorized payroll preparers. |
| `GET/POST /staff-operations/profiles/` · `PATCH /staff-operations/profiles/<id>/` | 🔐 `staff.profile.manage` | Non-sensitive operational staff profiles; user accounts and roles remain separately controlled |
| `GET/POST /staff-operations/shift-templates/` · `PATCH /staff-operations/shift-templates/<id>/` | 🔐 `shift.manage` | Reusable shift definitions, inactive rather than silently deleted |
| `GET/POST /staff-operations/shifts/` · `GET /staff-operations/shifts/<ref>/` | 🔐 `shift.manage` | Bounded scheduled-shift queue; creation locks the worker, rejects overlapping shifts and approved-leave conflicts |
| `POST /staff-operations/shifts/<ref>/cancel/` | 🔐 `shift.manage` | Audited cancellation; an active attendance record blocks cancellation |
| `GET /staff-operations/attendance/` · `GET /staff-operations/attendance/<ref>/` | 🔐 `attendance.clock` / `attendance.manage` | Workers see only their own source-of-truth attendance record/event evidence; managers can scope the workforce list |
| `POST /staff-operations/attendance/clock/` | 🔐 `attendance.clock` | Server-timestamped, source-keyed `CLOCK_IN`, break, and `CLOCK_OUT` events. Raw attendance events are append-only. |
| `GET/POST /staff-operations/leave-requests/` · `GET /staff-operations/leave-requests/<ref>/` | 🔐 `leave.request` / `leave.approve` | Workers create/read only their own leave; approvers can view the queue |
| `POST /staff-operations/leave-requests/<ref>/review/` | 🔐 `leave.approve` | Independent approve/reject control. Self-review and approved-leave/shift conflicts are rejected. |
| `POST /staff-operations/leave-requests/<ref>/cancel/` | 🔐 owner / `leave.approve` | Cancels a future pending or approved request with append-only evidence |
| `GET/POST /staff-operations/payroll/compensation/` | 🔐 `payroll.view` / `payroll.manage` | Read salary terms or append effective-dated NGN compensation, including basic, housing/transport, pension applicability and documented minimum-wage applicability; historical terms cannot be rewritten. |
| `GET/POST /staff-operations/payroll/tax-identities/` | 🔐 `payroll.manage` | Confidential TIN registration history; list responses expose only a masked ID. Statutory runs require a registered effective TIN. Changed particulars require the tax-authority report date within 30 days; no government verification provider is connected. |
| `GET/POST /staff-operations/payroll/statutory-rules/` · `GET /staff-operations/payroll/statutory-rules/<ref>/` | 🔐 payroll view/manage/approve or `payroll.rules.manage` / `payroll.rules.review` | Reviewable effective-dated Nigeria rule proposals with cited legal sources and immutable review history; rules cannot be used while pending. |
| `POST /staff-operations/payroll/statutory-rules/<ref>/review/` | 🔐 independent `payroll.rules.review` | Approve/reject with a substantive legal/compliance note; proposer self-review is prohibited. |
| `GET/POST /staff-operations/payroll/periods/` · `GET /staff-operations/payroll/periods/<ref>/` | 🔐 `payroll.view` / `payroll.manage` / `payroll.approve` | Paginated confidential runs or idempotent preparation from locked compensation and approved profiles. From 2026, the server selects the approved effective ruleset, calculates progressive annualized PAYE, employee/employer pension, evidenced NHF/NHIS/mortgage/life-insurance claims, prorated capped rent relief, and benefit-in-kind valuations (owned assets at the configured acquisition/market rate, hired assets at annual rent, accommodation subject to the configured annual-gross cap, and listed statutory exemptions). It carries verified in-system or evidence-backed opening YTD; external openings may separately supply `cash_emoluments` when gross includes non-cash benefits (otherwise it defaults to gross). Pre-2026 manual calculations remain unchanged. |
| `POST /staff-operations/payroll/periods/<ref>/submit/` · `review/` | 🔐 preparer / independent `payroll.approve` | Maker-checker submission/review; approval posts gross wages, employer pension expense, net pay and separate statutory/other deduction liabilities into the existing double-entry ledger. |
| `POST /staff-operations/payroll/periods/<ref>/pay/` | 🔐 `payroll.manage` | Records settlement to the existing ledger; cash requires an open assigned drawer. Bank settlement is manual-reference-only; no payout/reconciliation provider is integrated. Corrections use replacement runs, not edits. |
| `GET /staff-operations/payroll/payslips/<ref>/` | 🔑 own finalized line or `payroll.view` | Private, no-store PDF payslip after approval; payroll viewers can request another employee line by `staff_id`. |
| `POST /pos/cash-sessions/open/` · `close/` | 🔐 `cash_session.open` / `cash_session.close` | Controlled cashier drawer opening/count close |
| `POST /finance/cash-sessions/<ref>/variance-review/` | 🔐 `payment.refund.approve` | Completes manager review of a material drawer variance; generic approval review is intentionally blocked |
| `GET /finance/expenses/` · `GET /finance/expenses/<ref>/` | 🔐 `expense.manage` / `expense.approve` | Paginated controlled expenses; requesters see their own while financial managers and dedicated approvers can reconcile the full queue. |
| `POST /finance/expenses/` | 🔐 `expense.manage` | Creates a source-keyed controlled expense draft. |
| `POST /finance/expenses/<ref>/submit/` | 🔐 requester + `expense.manage` | Freezes a draft and creates policy-snapshotted expense approval evidence when the active expense threshold requires it. |
| `POST /finance/expenses/<ref>/review/` | 🔐 `expense.approve` | Dedicated maker-checker expense review; generic approval review is blocked so expense state and approval evidence cannot diverge. |
| `POST /finance/expenses/<ref>/post/` | 🔐 `expense.manage` | Posts an approved immutable `EXPENSE` transaction. A cash paid-out requires the assigned cashier's open drawer, appends `PAID_OUT` evidence, and reduces expected cash atomically. |
| `GET /payments/refunds/` · `GET /payments/refunds/<id>/` | 🛎 | Refund lifecycle rows reconciled from Paystack |
| `POST /payments/record/` | 🛎 | Record CASH/POS/BANK_TRANSFER payments |
| `GET/POST /rooms/` · `GET/PATCH/DELETE /rooms/<id>/` | 🛎 read · 🧰 write | DELETE = soft deactivation |
| `GET/POST /room-types/` · `GET/PATCH/DELETE /room-types/<id>/` | 🛎 read · 🧰 write | Pricing, capacity, amenities; DELETE = deactivate |
| `POST /room-types/<id>/images/` · `PATCH/DELETE /room-images/<id>/` | 🧰 | Multipart upload / reorder / primary |
| `GET/POST /amenities/` · `PATCH/DELETE /amenities/<id>/` | 🛎 read · 🧰 write | |
| `GET/POST /facilities/` · `PATCH/DELETE /facilities/<id>/` | 🛎 read · 🧰 write | |
| `GET/POST /policies/` · `PATCH/DELETE /policies/<id>/` | 🛎 read · 🧰 write | |
| `GET /settings/` · `PATCH /settings/` | 🧰 read · 👑 write | Business rules (tax, deposit %, deadlines…) — audited diffs |
| `GET/POST /offers/` · `GET/PATCH/DELETE /offers/<id>/` | 🛎 read · 🧰 write | Dates/discounts/scope validated |
| `GET/POST /gallery/` · `GET/PATCH/DELETE /gallery/<id>/` | 🛎 read · 🧰 write | |
| `GET /enquiries/` · `GET/PATCH /enquiries/<id>/` | 🛎 | General enquiry workflow plus cancellation/refund review rows |
| `POST /enquiries/<id>/review/` | 🛎 | Mark cancellation/refund request under review |
| `POST /enquiries/<id>/approve-cancellation/` | 🛎 | Staff approval cancels booking; refund remains pending review |
| `POST /enquiries/<id>/reject-cancellation/` | 🛎 | Reject request and notify guest |
| `POST /enquiries/<id>/process-refund/` | 🧰 | Manager/admin submits eligible Paystack refund backend-only |
| `POST /enquiries/<id>/close/` | 🛎 | Close request |
| `GET /reports/revenue/?start_date&end_date` | 🧰 | Legacy payment/booking compatibility totals, by-day, by-provider, refunds. Use the finance ledger summary for financially distinct reporting. |
| `GET /reports/occupancy/?start_date&end_date` | 🧰 | Per-day occupied rooms + average % |
| `GET /reports/bookings/?start_date&end_date` | 🧰 | Counts by status/payment status + room-type revenue |
| `GET/POST /users/` · `GET/PATCH /users/<id>/` | 👑 | Staff accounts, roles, activation — audited |
| `GET /audit-logs/` · `GET /audit-logs/<id>/` | 👑 | Read-only immutable trail (filter: action, object_type, actor, date range) |
| `GET /reviews/` | 👑 | Guest reviews list — **ADMIN only** (managers/receptionists get 403). Filters: `?rating=`, `?status=NEW\|REVIEWED`, `?date_from=`, `?date_to=`, `?search=` (guest name/email/phone, booking reference, review text), `?ordering=newest\|oldest\|highest\|lowest`. Paginated. |
| `GET /reviews/stats/` | 👑 | `{total, average_rating (null when empty), new_count, by_rating}` — single aggregate query |
| `GET/PATCH/DELETE /reviews/<id>/` | 👑 | Detail / status+internal notes / delete — all mutations audited |

## Rate limits (429 + `RATE_LIMITED`)

login 5/min · register 20/h · password reset 10/h · enquiry 10/h ·
booking create 30/h · availability 240/h · payment init 20/h · payment verify 60/h ·
review verify 20/h · review submit 5/h · QR context 60/min · QR submit 8/min ·
**paystack webhook 300/min** (per IP; deliberately generous so legitimate Paystack
retries/bursts are never dropped, while abuse floods are blunted)
(per user for authenticated scopes, per IP otherwise).


## Guest checkout authentication

`POST /api/auth/register/` remains available for optional accounts, but checkout never requires it. `POST /api/bookings/` is public and accepts the nested guest contact record. Its response contains a one-time raw guest access token; the booking stores only its digest. Send that token as `X-Guest-Access-Token` with booking detail, receipt, payment initialization, and payment verification requests. Booking references alone return not found. Cancellation uses the separate Contact request/status token flow.
