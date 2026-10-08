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

## Staff console

Sign in at `/login.html`. Roles are resolved server-side from the database —
the browser cannot grant itself privileges.

| Role | Can do |
|---|---|
| Receptionist | Bookings, check-in/out, availability, guests, enquiries, payments |
| Manager | The above, plus rooms, offers, reports, refunds and guest discounts |
| Admin | Everything, plus staff accounts, hotel settings and audit logs |

The older role labels remain compatible for reservation work. Operational
screens additionally enforce server-side capabilities and team scope; a visible
menu item is never proof that a staff member may perform its action. Managers
must assign the least-privileged operational capability rather than sharing an
administrator account.

### Operations, finance, and workforce

The staff sidebar groups the operational modules. Every list is bounded and
filterable; loading placeholders become specific empty or retryable error
states rather than invented records.

| Area | Day-to-day rule |
|---|---|
| Stays, folios, and cashier | Charges, collections, refunds/reversals, expenses, and cash movements are separate immutable evidence. Never delete or overwrite a posted financial record; correct it with the approved linked reversal/adjustment workflow. |
| POS / room service | Create orders from the live catalog, progress them through the controlled kitchen/service state, and charge a room only after the authoritative stay/folio validation succeeds. |
| Guest requests, housekeeping, maintenance | Work only in the authorized team queue. Record status/event evidence and use guest-visible comments deliberately; do not expose internal notes through the portal. |
| Inventory and procurement | Issue/receive stock through append-only movements. Use suppliers and purchase-order approvals; never silently edit a historical goods receipt or stock balance. |
| Workforce | Schedule shifts, clock attendance through source-keyed events, and review leave with a different authorized approver where required. Attendance and leave evidence are append-only/auditable. |
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
transfer, POS).

Every successful payment emails a receipt with a PDF attachment. Staff can
re-send it from the receipt screen; the system will not send a duplicate
automatic receipt for the same payment.

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
reference—including `/portal/` assets—with `?v=<version>` so a deploy can never
serve stale JavaScript beside new HTML.

Returning public/staff visitors are handled by `js/update-checker.js`: it
compares the loaded version against `version.json` (fetched `no-store`, at most
every 15 minutes, or 5 minutes after a tab regains focus) and shows a *"A new
version is available"* modal with **Refresh now** and **Not now** when safe.
It never auto-reloads, never clears booking drafts or authentication, and
defers around active booking/payment work or dirty forms.

The memory-session portal deliberately omits reload-capable PWA/update scripts.
A portal release still receives fresh stamped assets, but a guest is never
silently reloaded and stripped of an opaque in-memory session. Before release,
confirm that `/portal/**` retains `Cache-Control: no-store` and
`X-Robots-Tag: noindex, nofollow`, and test request-link, consume, portal read,
logout, and refresh/reopen-link behavior in staging.

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
