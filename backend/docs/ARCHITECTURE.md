# Architecture — J-ONE HOTEL & LODGE Backend

## 1. System overview

```
┌──────────────────────┐  HTTPS/JSON   ┌──────────────────────────────────────┐
│ Public website +     │ ────────────▶ │ Django 5.2 + DRF API (Gunicorn)      │
│ staff dashboard      │  Bearer JWT   │  config/settings/{dev,prod}          │
│ (separately hosted,  │ ◀──────────── │                                      │
│  HTML/CSS/vanilla JS)│   envelope    │  apps: core accounts hotel rooms     │
└──────────────────────┘               │        offers gallery bookings       │
                                       │        payments enquiries            │
┌──────────────────────┐   init/verify │        notifications reports audit   │
│ Paystack             │ ◀───────────▶ │                                      │
│ (card capture hosted │   webhook     │  Service layer:                      │
│  on Paystack; we     │  (HMAC signed)│   • availability engine              │
│  never see PAN/CVV)  │               │   • pricing engine                   │
└──────────────────────┘               │   • booking engine • payment service │
                                       │   • offers eligibility • notify/audit│
┌──────────────────────┐               │                                      │
│ MySQL 8.0 (prod)     │ ◀──────────── │  Celery worker + beat (scheduled     │
│ SQLite (dev/tests)   │   Django ORM  │  booking jobs, when enabled)         │
└──────────────────────┘               │                                      │
┌──────────────────────┐               │  Redis: cache + Celery broker +      │
│ Backblaze B2 (media, │ ◀──────────── │  throttle storage (production)       │
│  S3-compatible, prod)│  uploads      │  WhiteNoise: static files            │
└──────────────────────┘               └──────────────────────────────────────┘
```

## 2. Application modules

| App | Owns | Depends on |
|---|---|---|
| `core` | response envelope renderer, pagination, exceptions, permissions, throttle scopes, upload/email utils, health | — |
| `accounts` | custom `User` (email login, roles), JWT endpoints, admin user management | core |
| `hotel` | `HotelSettings` **singleton** (identity + business rules), `HotelPolicy`, `Facility` | core |
| `rooms` | `RoomType`, `RoomTypeImage`, `Amenity`, physical `Room` | core |
| `offers` | `Offer`, eligibility/discount services | rooms |
| `gallery` | `GalleryItem` | core |
| `bookings` | `Guest`, `Booking`, `BookingRoom`; availability/pricing/booking engines | rooms, offers, hotel, accounts, notifications, audit |
| `payments` | `Payment`, Paystack client, verification/webhook/orchestration services | bookings, notifications, audit |
| `enquiries` | `Enquiry` | core, notifications |
| `notifications` | `Notification`, fan-out, email log + provider delivery | accounts |
| `reports` | dashboard + revenue/occupancy/bookings reports (read-only aggregation) | bookings, payments, rooms |
| `audit` | `AuditLog` (append-only), `log_action()` | — |

Dependencies only ever point "downwards" — no circular imports.

## 3. Data model (core relationships)

```
User(accounts) 1──0..1 Guest ──< Booking >──1 RoomType ──< Room
                                  │ 1                  (Compound via)
                                  └──< BookingRoom >──1 Room
                                  │ 1
                                  └──< Payment
Offer >──< RoomType (optional restriction)
HotelSettings (singleton), HotelPolicy, Facility, Amenity, GalleryItem,
Notification, Enquiry, AuditLog
```

Key modeling decisions:

* **`BookingRoom` (assignment) is the inventory lock.** Booking 2 rooms ⇒ 2
  rows, one per physical room, created in the same transaction as the booking.
* **Room `status` ≠ booking availability.** `MAINTENANCE`/`OUT_OF_SERVICE` take
  a room out of the pool; date-availability is *computed* from assignments.
* **Pricing is snapshotted on the booking** (`price_per_night`, `subtotal`,
  `discount_amount`, `tax_amount`, `fee_amount`, `total_amount`,
  `required_payment`). Later room-type price changes never corrupt history.
* **Soft lifecycle everywhere history matters:** rooms/room-types deactivate
  (`is_active=False`) rather than delete; payments/bookings are never deleted.
* Indexes match real query patterns: `booking_reference` (unique),
  `Booking(room_type, check_in, check_out)`, `BookingRoom(room, check_in,
  check_out)`, `Booking(cancelled_at)` for legacy refund reconciliation,
  status/payment_status, `Payment.reference` (unique),
  `Payment(status, paid_at)` for collection reports,
  `Notification(recipient, is_read)`, offer windows, and `created_at`s.

## 4. The availability engine (`apps/bookings/services/availability.py`)

Overlap rule (half-open ranges):

```
existing.check_in < new.check_out  AND  new.check_in < existing.check_out
```

⇒ a guest checking out on the 15th never blocks the next guest checking in on
the 15th (hotel-standard night-based semantics).

A room counts as available for [ci, co) iff:
1. `is_active` and **not** `MAINTENANCE`/`OUT_OF_SERVICE`, and
2. it has **no overlapping active assignment** — where *active* means the parent
   booking is `CONFIRMED`/`CHECKED_IN`, **or** `PENDING` with `expires_at > now`
   (a live payment hold). Expired holds never block inventory, even before the
   beat task flips them to `EXPIRED` (also flipped lazily on read).

## 5. The pricing engine (`apps/bookings/services/pricing.py`)

Computed **only** on the server, for every quote and (again) booking creation:

```
nights       = check_out − check_in
subtotal     = base_price × nights × rooms
discount     = best eligible offer (or validated promo code)
extra_guests = charged overflow beyond max_guests×rooms (if allowed)
tax          = (subtotal − discount + extra_fee) × tax_rate_percent
fees         = flat service_fee (settings)
total        = subtotal − discount + extra_fee + tax + fees
required     = total × deposit_percent      (what must be paid online to confirm)
```

Offers are *auto-applied* (best discount) unless a promo code is supplied, in
which case eligibility is validated (dates window, min/max nights, room-type
scope). All arithmetic uses `Decimal`, half-up quantized to kobo.

## 6. Booking creation — double-booking protection

```
BEGIN TRANSACTION
  lock RoomType row (SELECT ... FOR UPDATE)   ← serializes all bookings of a type
  re-validate dates/capacity
  RE-CHECK availability inside the lock        ← the early search result is never trusted
  if insufficient rooms → 409 ROOM_UNAVAILABLE
  resolve guest, compute quote (pricing engine)
  create Booking(PENDING, expires_at = now + pending_booking_minutes)
  bulk-create BookingRoom holds for the chosen rooms
COMMIT
notifications + audit + confirmation email (on_commit)
```

Because every writer of the same room type serializes on the type row and
re-checks assignments **under that lock**, two concurrent guests cannot
reserve the same room. Manual (staff) bookings follow the identical path.

## 7. Payment architecture (Paystack)

* **Initialize**: server computes the amount (remaining deposit requirement,
  then any balance), stores `Payment(PENDING, reference=J1P-…)`, calls
  commits that short transaction, and only then calls Paystack. Concurrent or
  repeated clicks reuse the same pending reference and authorization URL. The
  API returns only safe redirect fields. It never accepts an amount from the client.
* **Verify** (`GET /api/payments/verify/<ref>/`): server fetches the transaction
  from Paystack itself; checks `status=success`, **amount == expected
  (kobo)**, **currency == NGN**; then atomically marks the payment SUCCESS,
  credits the booking (`amount_paid`, derived `payment_status`), and flips
  `PENDING → CONFIRMED` once `required_payment` is covered. **Idempotent**: a
  second call short-circuits on the stored SUCCESS state — retries never
  double-credit. Webhooks reuse the exact same verification path after HMAC
  signature validation, so the two channels can never disagree.
* **No fake paths**: missing secret key ⇒ `503 PAYMENT_NOT_CONFIGURED`.
* **Refunds**: local `Refund` rows are separate from `Payment` and `Booking`.
  Staff approve cancellation requests first; manager/admin refund submission calls
  Paystack `/refund` backend-only with server-derived amount/currency. Webhooks
  (`refund.pending`, `refund.processing`, `refund.processed`, `refund.failed`,
  `refund.needs-attention`) reconcile idempotently. Payment/booking refund totals
  update only when Paystack confirms `processed`.
* **Offline payments** (front desk CASH/POS/BANK_TRANSFER) are recorded as
  SUCCESS `Payment` rows via `/api/admin/payments/record/` and drive the same
  confirmation logic — one monetary code path. Offline refunds are manual and
  are not sent to Paystack.
* We never store card data; gateway metadata is whitelisted (id and channel; payer IP is not retained).

## 8. Booking state machine

```
                                        (payment verified / staff confirm)
      ┌────────┐  deposit window lapses ┌───────────┐  ┌────────────┐
      │PENDING │ ─────────────────────▶ │ CONFIRMED │─▶│ CHECKED_IN │─▶ CHECKED_OUT
      └───┬────┘                        └─────┬─────┘  └────────────┘
          │ expires (beat/lazy)               ├── staff-approved cancellation ─▶ CANCELLED
          ▼                                   └── check-in date passes ─▶ NO_SHOW
      EXPIRED  (hold released)
```

Every transition validates the current state (`INVALID_BOOKING_STATE` 409),
is idempotent against retries, and writes an audit entry.

## 9. Security model

* JWT bearer auth; refresh rotation + blacklist; login/register/reset
  throttling; email-based accounts; role checks **server-side only**.
* `IsOwnerOrStaff` object-level permission prevents IDOR on bookings/payments.
* Webhook: HMAC-SHA512 signature (`x-paystack-signature`) validated from the
  raw body before parsing.
* Uploads: extension + size + Pillow content verification; B2 creds sever-side only.
* Production: HTTPS redirect+HSTS, `X-Content-Type-Options`, frame `DENY`,
  referrer policy, secure cookies, restricted CORS allowlist, MySQL-only guard.
* Errors: one handler, no tracebacks to clients, full traceback in logs.
* Audit log is append-only (no API/admin delete) for: bookings, payments,
  check-in/out, room changes, user/role changes, settings changes, offer changes.

## 10. Performance practices

* Dashboard = a handful of aggregate queries (`Count`/`Sum` + conditional
  aggregation), never collection loops; report ranges bounded (≤ 366 days).
  Datetime report filters use hotel-local half-open bounds (`>= start midnight`,
  `< next-day midnight`) rather than `field__date`, so MySQL can use range
  indexes. Validate representative `EXPLAIN ANALYZE` plans on the production
  MySQL 8+ staging dataset before each high-volume report rollout.
* Lists paginated (20 default, 100 max) with a single pagination envelope.
* Hot paths use `select_related`/`prefetch_related` (bookings table carries
  guest/type/rooms in one page-load query; room-types prefetch amenities+images).
* Singleton settings cached 5 min; public hotel payload cached; caches
  invalidated on writes. Volatile data (availability/payments) is **never** cached.
* Money serialized as strings; list serializers are lightweight vs detail ones.

## 11. Deployment (provider-independent)

```
Web process:             gunicorn config.wsgi:application --bind 0.0.0.0:$PORT
Worker (scheduled jobs): celery -A config worker
Scheduler (scheduled jobs): celery -A config beat
Authoritative database:  MySQL 8+ (DATABASE_URL or DB_* variables)
Cache / task broker:     Redis (REDIS_URL in the current production settings)
Private user media:      configured object storage
```

`build.sh`: `pip install -r requirements.txt && python manage.py collectstatic
--noinput && python manage.py migrate` · liveness `/api/health/live/` ·
readiness `/api/health/ready/` (legacy `/api/health/` remains a readiness alias).

The application reads hosts, CORS/CSRF origins, database, media, email, and
payment settings from environment variables. Render, Railway, Fly.io, and a
generic Ubuntu process manager can run the same web command; only their
platform-specific process and environment setup differs.

## 12. Versioning strategy

The contract ships as `/api/` today (single consistent version). Any future
breaking change will be introduced as `/api/v2/` while `/api/` keeps working,
so the independently hosted frontend can migrate deliberately.

## Guest checkout and secure booking access

Hotel customers are not website users. `User` accounts are for authenticated
staff (ADMIN, MANAGER, and RECEPTIONIST); the legacy `GUEST` choice is retained
only so existing rows can be migrated safely and cannot sign in. Public
checkout creates or reuses a standalone `Guest` and links the reservation
through `Booking.guest`.

A booking response and its email contain a cryptographically random,
90-day guest access token. Only a SHA-256 digest is stored on the booking.
Guest detail, receipt, payment initialization, and payment verification require
the token in `X-Guest-Access-Token`; a booking reference alone is never
sufficient. Cancellation/refund status uses a separate request reference and
access token issued by the enquiries app. Staff continue to use JWT and staff permissions.
