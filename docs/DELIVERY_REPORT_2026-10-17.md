# J-ONE Hotel & Lodge — Delivery report, 17 October 2026

Batch: nine items raised against the staff console, procurement, finance and
storage. Every item below is implemented, migrated and covered by tests.

| # | Item | Status |
|---|---|---|
| 1 | Apps missing from django-admin | Done — 7 apps registered, 82 models, all changelists return 200 |
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
