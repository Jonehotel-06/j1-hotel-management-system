# Usage

How the system is operated day to day. For installation and configuration see
[SETUP.md](SETUP.md).

## Guest booking flow

Six steps, spread over three pages. The backend prices and validates every one
of them.

| Step | Page | What happens |
|---|---|---|
| 1 Stay | `booking.html` | Dates and party size. The calendar shows live availability; sold-out dates cannot be picked. |
| 2 Room | `booking.html` | Room type and **how many rooms**. Each card shows a backend-quoted total for that quantity. |
| 3 Guest | `booking-review.html` | Name, phone, email and an optional offer code. |
| 4 Review | `booking-review.html` | The authoritative quote: nights, rooms, rate, subtotal, discount, total. |
| 5 Payment | `booking-confirmation.html` | The booking is created, then Paystack takes over. |
| 6 Confirmation | `booking-confirmation.html` | Verified payment, receipt emailed with a PDF attached. |

Going back, refreshing, or reopening the tab preserves the stay, room, guest
details and room count. It never restores a stale offer code.

### Offer codes

A code reaches the flow from an offer's *Book with this offer* button or the
`?offer=` URL parameter. It applies to the current visit only.

If the code does not fit the chosen stay, the guest sees the specific reason on
the offer field — expired, not started yet, minimum or maximum nights, wrong
room type, or not covering the whole stay — and the stay is immediately
re-priced at the standard rate. An optional code can never blank out the
nights, subtotal or total.

The public offers page shows each offer's validity window, discount, minimum
and maximum stay, and which room types it applies to, so eligibility is visible
before the quote step.

### Multi-room bookings

Choosing N rooms creates one booking with N room assignments. The total is the
nightly rate × nights × rooms, discounted by the backend. Receipts list every
room, and cancellation or refund applies to the booking as a whole.

## Verified-email guest portal

The public footer's **Guest portal** link opens `/portal/login.html`. It is
purposefully separate from **Your stays** (`/my-bookings.html`): the latter
uses a booking-specific reference/access model, while the portal uses a
verified-email, short-lived server session.

1. The guest enters the email used for their stay. The response is generic, so
   it never reveals whether an address has access.
2. If eligible, the guest receives a one-use link. Opening it consumes the
   short-lived challenge and opens the portal.
3. The guest can see only reservations tied to the verified email, lazy,
   paginated folio statements, and their own guest-visible service requests.
   New assistance requests require an active in-house stay and can be assigned
   only to an authoritative current room shown by the portal.
4. **Sign out** revokes the session. A refresh or reopening the portal without
   the secure link intentionally starts over; staff must not advise a guest to
   save a link token or browser session value.

Portal pages and portal data must never be copied into a staff workflow,
emailed as a statement attachment without the hotel's approved process, or used
to identify another guest. A guest may comment on or cancel only a request the
backend says remains eligible.

## Room and table service QR codes

Authorized managers/admins open `/dashboard/service-qr.html` to issue, rotate,
and revoke links, then print the downloaded SVG at the matching room or table.
Rotation invalidates the old print immediately; take it out of service and
replace it. A revoked link is retained for audit/history and can be reissued
with a fresh token.

- A room QR creates a normal guest-service request for the room's **single
  current in-house occupant**. The backend derives guest, stay, and room; an
  empty or ambiguous occupancy is rejected. The guest may choose a supported
  service category, and the request routes into the same department queue as
  staff/portal requests.
- A table QR never invents a guest and is restricted to Food & Beverage. It
  creates an anonymous service request, not a public order or payment. When the
  guest is ready to order, an authorized waiter/bartender can choose **Create POS
  draft** from that request, select the actual menu items, and review the
  server-priced draft. Restaurant QR labels should be issued using the exact
  registered table code so staff can attach the draft to the right open session;
  bar/pickup QR labels stay free text.
- This handoff does not import guest-entered request text as menu lines, submit
  the order, or post a charge. The ordinary POS submit/delivery workflow remains
  the controlled point at which production and financial effects can occur. The
  request and linked order remain associated in their respective timelines.
- The public page keeps the bearer in the URL fragment only long enough to
  submit a request, removes it from the address bar, and sends it to the API in
  a header. Never copy the raw bearer into logs, screenshots, tickets, or
  general chat. Revoke a code immediately if a print is lost or exposed.
- QR submissions are idempotent during retries. The request queue, service
  history, SLA, team permissions, and guest-visible/internal comment rules are
  the existing service-request workflow.

## Staff console

Sign in at `/login.html`. Roles are resolved server-side from the database —
the browser cannot grant itself privileges.

| Role family | Day-to-day surface |
|---|---|
| Receptionist / front-desk supervisor | Reservation and stay workflows, guest folios, payments, enquiries, dispatch, and permitted team queues. |
| Cashier | Permitted POS orders, tender collection, drawer sessions, and controlled expenses; cashier scope is not general financial administration. |
| Restaurant manager / waiter | Restaurant managers maintain the registered dining-table list; restaurant staff open service sessions and create authorized checks. Server-made charges still use existing POS/ledger rules. |
| Bar manager / bartender | Bar-category orders, bar menu (manager), and assigned bar tickets. Bartenders can create a server-priced bar draft from an eligible table QR request while preserving its free-text location. |
| Kitchen manager / chef | Assigned kitchen production tickets and controlled status progression, not financial order editing. |
| Housekeeping manager / housekeeper | Role-scoped task assignment/inspection or own task execution. |
| Maintenance technician | Assigned work orders and authorized maintenance actions; outage approval remains a separate permission. |
| Accounts manager / accountant | Folios, ledger/financial reporting, expenses, payroll surfaces, and maker-checker duties according to the seeded capability matrix. |
| HR manager | Staff operational profiles, shifts, attendance, leave review, and payroll operations. |
| Procurement officer / storekeeper | Purchase workflow or inventory/receipt movement scope, with independent procurement/count approvers. |
| Security | Limited guest-request and audit visibility; no general account, booking, or money authority. |
| Manager / general manager | Broad operational oversight; the seeded matrix still applies and dangerous actions keep separate approval controls. |
| Admin | Staff accounts, hotel settings, audit and full administrative operations. |

Legacy role labels remain compatible for reservation work. Each staff member
uses an individual account; the database-backed capability matrix and object/team
scope are enforced by the API. A visible menu item, browser-side role, or
workstation label is never permission. Create only the role needed for the job;
do not share administrator credentials. Current role grants are seeded by
migration and reviewed server-side—changing a frontend menu does not change
access. The **Workstations** registry lets an authorized manager label a shared
desktop and optionally select it in that browser. The reference is sent only as
audit metadata with the authenticated user's API request; it is not a sign-in,
permission, presence lock, or verified proof of who is at the device. Clear it
when the browser is being repurposed.

### Operations, finance, and workforce

The staff sidebar groups the operational modules. Every list is bounded and
filterable; loading placeholders become specific empty or retryable error
states rather than invented records.

| Area | Day-to-day rule |
|---|---|
| Stays, folios, and cashier | Charges, collections, refunds/reversals, expenses, and cash movements are separate immutable evidence. Never delete or overwrite a posted financial record; correct it with the approved linked reversal/adjustment workflow. |
| POS / room service | Create orders from the live catalog and progress them through the controlled kitchen/service state. Restaurant managers register tables; waiters open a table session before dine-in orders and can attach split checks to it. Eligible table QR requests can launch a linked POS draft; staff still select menu items and submit through the existing workflow. Close is rejected while an order is unfinished or a delivered check is unpaid. Room-service charges still require the authoritative in-house stay/folio validation. |
| Guest requests, housekeeping, maintenance | Work only in the authorized team queue. Record status/event evidence and use guest-visible comments deliberately; do not expose internal notes through the portal. |
| Inventory and procurement | Issue/receive stock through append-only movements. Use suppliers and purchase-order approvals; never silently edit a historical goods receipt or stock balance. |
| Workforce and payroll | Schedule shifts, clock attendance through source-keyed events, and review leave independently. Compensation terms include effective-dated basic/housing/transport amounts and recorded pension/minimum-wage applicability. For 2026 Nigeria payroll, record the employee Tax ID and retained evidence first; TIN changes must be reported within 30 days, and raw IDs stay out of general staff profiles/list responses. Propose statutory bands/rates with official-source citations; a different authorized legal/compliance reviewer must approve the immutable ruleset before use. Supply actual NHF/NHIS, owner-occupied mortgage interest, preceding-year life-insurance premium, rent and benefit-in-kind inputs with retained evidence. Owned assets, hired assets, accommodation caps and statutory exemptions are calculated from the approved ruleset. If earlier current-year payroll was processed elsewhere, provide verified opening YTD totals and pay-period count. When opening gross includes non-cash benefits, specify `cash_emoluments` separately; if omitted, it defaults to opening gross for backward compatibility. The server otherwise requires approved in-system history. PAYE uses progressive bands and cumulative calendar-year snapshots; rent relief is prorated across covered calendar days and capped. Approval posts cash wages, employer pension expense and separate payables to the ledger. Pre-2026 manual runs retain prior calculation behavior; bank payout/reconciliation/provider integration is deferred. Corrections are replacement runs, never edits to historical snapshots. |
| Reports | Read charges/revenue, collections, refunds/reversals, expenses, and cash variance as distinct measures. Legacy payment totals are compatibility information, not a substitute for the financial ledger. |

### Arrivals and check-in

**Check-in** lists today's expected arrivals and everyone currently in-house.

A guest can be checked in on any day of their booked stay, including after
missing the first night. The rules the backend enforces:

- the booking must be CONFIRMED (checking in an already-checked-in booking is a
  no-op, not an error);
- today must not be past the check-out date;
- today must not be before the check-in date, unless the hotel has turned off
  **Restrict check-in to the booked check-in date** in Settings.

When the action is unavailable, the button is disabled and carries the reason
rather than failing after the click.

### Late arrivals and missed bookings

**Missed bookings** has two tables.

*Late / missed first-night arrivals* — guests who missed their first night but
whose stay is still running. Each row shows the reference, guest, rooms, booked
dates, nights and nights missed, amount paid, outstanding balance and status,
with a **Check in** action. Missed nights are not refunded automatically.

*Guests who never arrived* — bookings whose arrival day passed with no
check-in. The actions are **No-show**, **Reschedule** (keeps the same booking
and all its payments, re-checks availability and re-prices the stay) and
**Refund**, which hands over to the cancellation workflow so the cancellation
fee and the Paystack refund record stay in one place.

### Payments and receipts

Online payments are confirmed by Paystack verification or a signed webhook —
never by the browser redirect. Staff can also record manual payments (cash,
transfer, POS) when their account has both `booking.manage` and
`payment.capture`; POS collection authority alone does not permit room-booking
payments. Staff-initiated Paystack initialization and verification use the same
combined capability scope; guest owner/token checkout behavior is unchanged.

The Payments and Receipts screens require `payment.read` (front desk,
management and accounting roles). Accounting users can read the staff receipt
projection without receiving full booking-management access. Receipt sending
is separately gated by `payment.receipt.send`; the guest-owned public receipt
route remains owner/token-scoped. Every successful payment emails a receipt
with a PDF attachment. Authorized staff can re-send it; the system will not
send a duplicate automatic receipt for the same payment.

### Settings

Admin-only, under Settings. Hotel identity, contact details, check-in/out
times, stay limits, currency, tax, fees, deposit percentage, the pending
booking window, cancellation deadline and fee, and the published policy text.

**Restrict check-in to the booked check-in date** controls early arrivals only.
Late arrivals are always permitted either way.

## Releasing a frontend update

`version.json` is the single source of truth for the deployed version.

```bash
cd frontend
python3 build.py --bump patch      # or --version 2.0.0
python3 validate.py
```

`build.py` updates `version.json`, regenerates `js/version.js`, syncs the
service worker's `CACHE_VERSION`, and stamps every local `js/` and `css/`
reference—including `/portal/` and `/qr-service.html` assets—with
`?v=<version>` so a deploy can never serve stale JavaScript beside new HTML.

Returning public/staff visitors are handled by `js/update-checker.js`: it
compares the loaded version against `version.json` (fetched `no-store`, at most
every 15 minutes, or 5 minutes after a tab regains focus) and shows a *"A new
version is available"* modal with **Refresh now** and **Not now** when safe.
It never auto-reloads, never clears booking drafts or authentication, and
defers around active booking/payment work or dirty forms.

The memory-session portal and QR guest form deliberately omit reload-capable
PWA/update scripts. Their releases still receive fresh stamped assets, but a
guest is never silently reloaded and stripped of an opaque session or bearer.
Before release, confirm `/portal/**` and `/qr-service.html` retain
`Cache-Control: no-store` and `X-Robots-Tag: noindex, nofollow` (the QR page
also uses `Referrer-Policy: no-referrer`), then test the portal lifecycle and
issue/scan/revoke/reissue QR behavior in staging.

## Notifications

Unread staff notifications appear as a badge in the sidebar, bottom navigation
and topbar. When the site is installed as an app on a supporting browser
(Chromium on desktop and Android), the count is also mirrored onto the app icon
via the Badging API.

That mirror only updates while the app is open. Badging a closed app requires
Web Push infrastructure, which this project deliberately does not run.

## Diagnostics

```bash
cd backend
python manage.py email_check                    # active provider config
python manage.py email_check --send you@x.com   # + one real delivery
python manage.py smtp_check you@example.com     # full SMTP conversation
python manage.py check_media_storage            # upload/storage round trip
```

None of these ever print a credential.

## Tests

```bash
cd backend
python manage.py check
python manage.py makemigrations --check
python manage.py test

cd ../frontend
python3 validate.py          # HTML validation + node --check on all JS
node --test tests-js/        # frontend unit tests

# browser-driven PWA checks (needs the dev server running)
python3 tests-pwa/test_pwa.py
python3 tests-pwa/test_update.py
```

See [`frontend/tests-pwa/README.md`](../frontend/tests-pwa/README.md) for the
full PWA suite.
