# Maintenance pass — change report

Scope: targeted audit and repair of the booking/offer flow, staff check-in,
the email pipeline and the PWA update path. No rewrites; existing working
behaviour was preserved throughout.

Baseline commit: `1b31cbc` · Head: `f939c5c` · Frontend version `1.1.0 → 1.1.1`

---

## 1. Bugs found and fixed

### 1.1 Critical — offer code blanked out the booking total

**Symptom.** On the review step, nights / subtotal / total / room count
rendered as "unavailable" and no discount was applied.

**Root cause.** Two faults compounding:

1. `booking-review.html` read the offer code from the persisted booking draft
   (`savedDraft.offer_code`). A code captured on an earlier visit was
   re-applied to a *new* set of dates it was never valid for.
2. The quote call treated any error as fatal. The backend correctly rejected
   the ineligible code with a `400 OFFER_*`, and the page discarded the whole
   quote — so an **optional** field was able to destroy the mandatory pricing.

**Fix.** The offer code is now per-visit only: the sole carry-over is the
`?offer=` URL parameter set by an offer card, and no code is ever written to
`jone.booking.draft`. `OFFER_*` rejections are caught separately — the field
shows the specific reason, the input is cleared, and the stay is immediately
re-quoted at base price. The backend stays authoritative; no validation was
weakened.

Rejection reasons are now specific, from `apps/offers/services.py`:

| Code | Message shown on the field |
|---|---|
| `OFFER_CODE_INVALID` | "NOPE" is not a valid offer code. |
| `OFFER_NOT_STARTED` | This offer starts on 20 Oct 2026. |
| `OFFER_STAY_TOO_SHORT` | This offer needs a minimum stay of 3 nights; your stay is 2. |
| `OFFER_DOES_NOT_COVER_STAY` | This offer only covers 20 Oct 2026 to 31 Oct 2026, which does not include your whole stay. |

Also covered: expired, stay-too-long and wrong-room-type.

**Multi-room maths** was verified against the live API — 4 nights × 2 rooms at
₦75,000 gives subtotal ₦600,000, discount ₦120,000, total ₦480,000, and the
result is *identical* whether the code is typed manually or auto-applied.
Survives back-navigation and a hard refresh.

### 1.2 Multi-night bookings could not be checked in after a missed first night

A guest who arrived on night 2 of a 4-night stay could not be checked in at
all. `late_arrival_bookings_queryset()` and `can_check_in_today()` in
`booking_service.py` now allow check-in on any day within the booked range
while the booking is CONFIRMED, never checked in, and before check-out.
One-night stays are excluded (those are no-shows, and the existing
no-show / reschedule / refund workflow is untouched).

A **LATE / MISSED FIRST-NIGHT ARRIVALS** table was added to the staff missed
-bookings page, showing reference, guest, rooms, original check-in, check-out,
total nights, nights missed, amount paid, outstanding, status and a working
**Check in** action.

### 1.3 Broken logo in receipt emails

**Root cause.** Templates referenced `cid:jone-logo`, but **Brevo's v3
transactional API cannot deliver inline/CID images at all** — the part is
dropped and the image arrives broken. (Confirmed by Brevo staff; Anymail has
documented it since 2018. A `data:` URI is not an escape route either — Gmail
strips those entirely.)

**Fix.** `apps/core/email_assets.py` picks per provider: transports that
support `multipart/related` (SMTP/Django, SendGrid, Mailgun, Postmark) embed
the logo inline; Brevo and Resend fall back to the absolute HTTPS
`EMAIL_LOGO_URL`. The forced `44×44` box was removed so the real 507×900
aspect ratio is preserved.

| Provider | Inline | Source |
|---|---|---|
| django / sendgrid / mailgun / postmark | yes | `cid:jone-logo` |
| brevo / resend | no | `EMAIL_LOGO_URL` |

### 1.4 Receipt status badge rendered as "• STATUS"

Traced to **seed data, not product code**: three seeded bookings carried
`payment_status='PARTIAL'`, which is not a member of `Booking.PaymentStatus`.
The rows were corrected to `PARTIALLY_PAID`. The fallback itself is correct
behaviour and is now pinned by tests so it reads as intentional.

### 1.5 Global overflow hack masking real layout faults

`css/audit-fixes.css` carried `html,body{overflow-x:hidden}`, which hid wide
children instead of fixing them. It was removed; the actual fixes are the
`.table-wrap` / `.table-scroll-x` local scroll wrappers and the stacked mobile
cards. Verified `scrollWidth == clientWidth` afterwards.

---

## 2. Other work

- **Offer display.** Discount, title, description, valid-from/to, min/max
  nights, applicable room types, eligibility and CTA are exposed by the API and
  rendered on the offers page and the homepage banner ("Valid 20 Oct 2026 –
  31 Oct 2026", "Minimum stay: 2 nights").
- **Homepage featured offer.** Restrained overlay, contained text column,
  image fully visible, and the **Book this offer** button is legible in dark
  mode. Offer section only — nothing else on the homepage was touched.
- **Provider-neutral email.** `apps/notifications/providers.py` introduces a
  thin adapter boundary (Django/SMTP + Brevo, SendGrid, Mailgun, Postmark,
  Resend) selected by `EMAIL_PROVIDER`. Callers are unchanged and delivery is
  still synchronous — no Celery, Redis, queue or retry was added.
- **EmailLog** states are `PENDING → SENDING → SENT | FAILED` only. `SENT` is
  written only on provider acceptance. No `QUEUED`/`RETRYING`, no task IDs, no
  secrets.
- **Email templates** audited across receipt, confirmation, pending/reminder,
  cancellation, refund, enquiry, review invite, password reset and staff
  notices: table-based, `max-width`, inline styles, media queries; no grid,
  flex, JS or external CSS. Content and font sizes preserved. No overflow at
  320 / 375 / 430 / 600 / 1280; robust with images blocked and in dark mode.
  Receipt HTML is 13.3 KB, well under Gmail's ~102 KB clipping threshold.
- **Check-in date restriction** is honoured in the UI *and* enforced in the
  service layer, behind the new "Restrict check-in to booked check-in date"
  setting (migration `hotel/0005`). Only this safe toggle was added; riskier
  ones were skipped.
- **Booking details table** got a local scroll wrapper — no page-level
  horizontal scroll.
- **Mobile booking steps** use the full viewport with comfortable spacing at
  ≤560px. Step structure and functionality are unchanged.
- **PWA notification badge** syncs the unread count via the Badging API with
  feature detection; every failure is swallowed.
- **Storage.** The booking draft keeps room, dates, guest and room count — and
  never an offer code. The service worker caches no offer, quote, booking,
  payment, auth or notification response.
- **Comments.** Every source file carries an app-relative filename header
  (`# apps/offers/services.py`, `/* js/utils.js */`). AI-report prose was
  removed; security, business-rule, API-contract and browser-compat notes were
  kept, concisely. No TODO/FIXME/placeholder/debug/commented-out code remains.
- **Docs.** Six report markdowns deleted; `docs/SETUP.md` and `docs/USAGE.md`
  written; the root README replaced (it was a stale copy of the backend one);
  OpenAPI artifacts kept.

---

## 3. Files

**Created (9)**

| File | Why |
|---|---|
| `backend/apps/notifications/providers.py` | Provider adapter boundary |
| `backend/apps/core/email_assets.py` | Per-provider logo source + real aspect ratio |
| `backend/apps/notifications/management/commands/email_check.py` | Provider-neutral diagnostic |
| `backend/apps/hotel/migrations/0005_hotelsettings_restrict_check_in_to_booked_date.py` | Check-in restriction setting |
| `backend/tests/test_offer_eligibility.py` | 17 offer regression tests |
| `backend/tests/test_late_arrival_check_in.py` | 18 late check-in tests |
| `backend/tests/test_email_providers.py` | 30 provider/adapter tests |
| `docs/SETUP.md`, `docs/USAGE.md` | Setup and operation guides |

**Deleted (9)** — `FINAL_REPORT.md`, `RELEASE_1.1.1_REPORT.md`,
`IMPLEMENTATION_REPORT.md`, `FIX_REPORT_multi_room.md`,
`CHANGES-multiform-update.md`, `UPDATE_PLAN.md` (AI-report clutter);
`brevo_check.py`, `email_pipeline_check.py`, `requeue_emails.py`
(Brevo-specific / queue-era commands with no queue left to manage).

**Modified — substantive (24)**

| File | Change |
|---|---|
| `backend/apps/notifications/tasks.py` | Synchronous send through the adapter; log states |
| `backend/apps/bookings/services/booking_service.py` | Late-arrival queryset, `can_check_in_today` |
| `backend/apps/offers/services.py` | Specific eligibility rejection codes/messages |
| `backend/apps/bookings/serializers_admin.py` | Late-arrival fields, cached check-in decision |
| `backend/apps/bookings/views_admin.py` | Late-arrivals endpoint |
| `backend/apps/bookings/services/receipt_email.py` | Status presentation, logo source |
| `backend/apps/core/exceptions.py` | Offer error codes surfaced to the client |
| `backend/apps/core/email_design.py` | Logo sizing |
| `backend/apps/hotel/{models,serializers}.py`, `apps/offers/views.py` | Setting + offer display fields |
| `backend/apps/reviews/services.py` | Adapter call site |
| `backend/config/settings/{base,production}.py` | `EMAIL_PROVIDER` + provider config, validation |
| `backend/{.env.example,render.yaml,README.md}` | Provider-neutral documentation |
| `frontend/booking-review.html` | **The critical offer fix** |
| `frontend/booking{,-confirmation}.html`, `index.html`, `offers.html` | Offer display and carry-over |
| `frontend/dashboard/{missed-bookings,booking-details,settings}.html` | Late arrivals, scroll wrapper, toggle |
| `frontend/css/{dashboard,public,booking,audit-fixes}.css` | Mobile layout; overflow hack removed |
| `frontend/js/{dashboard,utils,api}.js` | Badge, late-arrival calls, helpers |
| `backend/tests/test_{receipt_email_content,email_pipeline,multi_room_booking,receipt_email,synchronous_email}.py` | Repointed at the adapter; obsolete queue assertions dropped |

**Modified — mechanical (~270)** Filename header added, and/or the `1.1.0 →
1.1.1` asset-version stamp rewritten by `build.py` across 54 pages.

**Migrations** — one: `hotel/0005_hotelsettings_restrict_check_in_to_booked_date`.
`makemigrations --check` reports no further changes.

---

## 4. Validation

| Command | Result |
|---|---|
| `python manage.py check` | No issues (0 silenced) |
| `python manage.py makemigrations --check --dry-run` | No changes detected |
| `python manage.py test` | **622 tests OK** (skipped=1) |
| `python validate.py` (frontend) | ALL OK — 54 HTML files, 22 JS files |
| `node --test tests-js/` | **43 pass, 0 fail** |
| `audit/overflow_pub.py` | `[]` — 13 public pages × 8 widths |
| `audit/overflow_dash.py` | `[]` — 25 dashboard pages |
| `audit/sticky_check.py` | Header pinned at `top: 0` on every page |
| `tests-pwa/` (9 modules) | **381 checks pass** — see below |

Frontend PWA suite, run against a static server on `:8080`:

| Module | Result |
|---|---|
| `test_update.py` | 6/6 — new SW waits, old caches purged, activation only on request |
| `test_pwa.py` | 153/153 — offline behaviour, install controls, theming, no JS errors |
| `test_regression.py` | 85/85 — no secrets exposed, booking page marked unsafe for auto-update |
| `test_tables_and_update.py` | 62/62 — local table scroll at 320px, update modal, **deferred on a dirty form** |
| `test_multi_room_ui.py` | 46/46 — 390–1440px, overflow not merely hidden |
| `test_ios.py` | 16/16 — install prompt behaviour, standalone detection |
| `test_a11y.py` | 14/14 — no serious/critical violations |
| `test_paystack.py` | 5/5 — **no API or Paystack response is ever served or cached by the SW** |
| `test_installability.py` | No critical manifest errors |

Manual checks, 320 → 1440px in Chromium:

- Ineligible offer at 390px keeps nights/subtotal/total/rooms populated, clears
  the input and shows the reason; an eligible stay shows
  "OCT20 — ₦120,000 off", total ₦480,000.
- `localStorage['jone.booking.draft']` holds no offer code after visiting an
  offer, and the code never reappears on a later visit. The quote survives a
  reload.
- Clicking **Check in** on a late arrival toasts "Guest checked in." and drops
  the row from the table.
- Missed-bookings page has no horizontal overflow at 320/390/1280 with the
  global hack gone; the stacked card shows no stray dividers.
- Receipt renders correctly at 375 and 600px with the logo at its true
  aspect ratio.
- **Receipt PDF** regenerated for a 2-room, 5-night booking: both rooms
  (401, 402) listed, logo at true aspect ratio, and the payment summary is
  internally consistent (₦750,000 total − ₦150,000 paid = ₦600,000
  outstanding). The PDF is built from the same `ReceiptSerializer` as the
  emailed HTML, so the attachment and the message body cannot disagree.

Run the backend suite with the pinned **Django 5.2.17**; an unpinned install
produces spurious failures.

---

## 5. Not done, deliberately

- **Badging a closed app.** `navigator.setAppBadge` only updates while the app
  is running. Updating the badge in the background needs a Push subscription,
  a push service and a `push` handler in the service worker — server
  infrastructure well outside a maintenance pass. The in-app path is
  implemented and the limitation is documented at the call site.
- **Inline logo for Brevo/Resend.** Impossible at the provider level, not a
  code defect. The hosted-URL fallback is the only Gmail-safe option.
- **The riskier settings toggles** were skipped as instructed; only the
  check-in restriction was added.
- **No queue/retry layer** was introduced for email. Delivery stays
  synchronous, as required.
