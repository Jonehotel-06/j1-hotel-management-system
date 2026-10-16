# The guest balance: why a paid room hid an unpaid laundry bill (1.1.23)

Booking `J1-20261012-D3DF9254` — accommodation **₦420,000 fully paid**, laundry **₦3,200
unpaid** — read on the check-out desk as:

| Field on the folio | Shown | Correct |
|---|---|---|
| Purchases and charges on the folio | **−₦416,800** | ₦3,200 |
| Amount to settle | **₦0** | ₦3,200 |
| Gate pass | **cleared** | blocked |

This document is the record of the defect, the fix, the migrations, the deployment steps and
the evidence. It is implemented in the repository — no part of it is a recommendation.

---

## 1. One guest, two balances

The system keeps **two different balances for one guest**, on purpose:

| Balance | Where it comes from | Where it is reported |
|---|---|---|
| **Accommodation** | `Booking.total_amount` / `amount_paid` / `amount_due`, the booking's own projection, charged into the ledger as a nightly `ACCOMMODATION_CHARGE` | "Accommodation balance", `amount_paid`, receipts, booking lists |
| **Folio charges** | everything the guest consumed on top of the stay — restaurant, bar, room service, laundry — posted as folio `CHARGE` debits, less the discounts, adjustments, reversals and payments that settle **them** | "Purchases and charges on the folio", `folio_charges`, `outstanding_balance` |

The desk collects the sum of the two (`amount_to_settle`), and the gate pass is refused while
either is outstanding.

## 2. Root cause — the same ₦420,000 was counted twice

`apps/finance/services/folio_service.py`

* `booking_folio_charge_map()` excluded the nightly `ACCOMMODATION_CHARGE` debits (they are
  the booking's own total split by night, so including them would double the room) but it
  **included the accommodation payment credit** — the `PAYMENT` posting that
  `apply_unallocated_payments_for_booking()` creates when the guest's room money is released
  from guest deposits onto the folio. That ₦420,000 is already counted in
  `booking.amount_paid`, so it was counted a second time as a folio credit:

  ```
  ₦3,200 laundry charge − ₦420,000 room payment = −₦416,800
  ```

* `booking_outstanding_balance()` = `booking.amount_due + folio_charge_balance`, floored at
  zero → `0 + (−416,800) → 0`, so `cleared` was true and `gate_pass_allowed` was true: the
  gate was told a guest who still owed ₦3,200 had "all bills cleared".

The same wrong figure fed the desk's collection maths
(`apps/finance/services/payment_collection_service.py`, `settle_outstanding_balance`):
`folio_due = max(folio_charge_balance, 0) = 0`, so `bill_total = stay_due (0) + 0 = 0`, and
the payment was refused with **409 `PAYMENT_ALREADY_COMPLETED`** — the desk could not even
take the laundry money.

## 3. The fix — classify money by the balance it settles

Money is classified **when the posting is created**, not by re-reading it later, and a
posting is only ever netted against the balance it actually settles:

| Posting | `settles` | Effect on the folio-charge balance |
|---|---|---|
| Room money released onto the folio (booking/stay payment) | `ACCOMMODATION` | **excluded** — already in `amount_paid` |
| Money the desk took for the folio charges (`settle_outstanding_balance`) | `FOLIO_CHARGES` | reduces the charges |
| Refund | inherits the classification of the allocation it reverses | refunding folio money re-opens those charges; refunding room money never invents a charge |
| Discounts, adjustments, reversals | `""` (counted with the charges) | unchanged — still reduce the charges |
| Guest charges (restaurant, bar, room service, laundry) | `""` | unchanged — increase the charges |
| Historical rows (pre-1.1.23) | `""` | keep the historical reading until the deployment pass labels them |

Consequences:

* the folio-charge balance can no longer be dragged negative by room money, so an unpaid
  laundry bill stays visible and `cleared` / `gate_pass_allowed` tell the truth;
* the desk can collect that bill, and the money it takes is classified `FOLIO_CHARGES`;
* nothing is "ignored": every discount, adjustment, reversal and folio payment still reduces
  the guest's charges, which is why the reported booking now reads **Purchases and charges
  ₦3,200 · amount to settle ₦3,200 · gate pass blocked** (and, once paid, ₦0 / allowed).

No posting row, payment, allocation, booking figure or stock record was edited, deleted or
re-posted. The folio ledger still holds the ₦420,000 credit and the ₦3,200 charge exactly as
posted; the change is a classification plus a read model.

## 4. Files changed

| File | Change |
|---|---|
| `backend/apps/finance/models.py` | `FolioPosting.Settles` (`ACCOMMODATION` / `FOLIO_CHARGES`; `""` = historical) and the `settles` field (`max_length=16`, indexed, blank default). Documents the two balances on the field. |
| `backend/apps/finance/services/ledger_service.py` | `_validate_folio_postings` reads `settles` from a posting spec, refuses an unknown value with `LedgerIntegrityError`, and persists it on the immutable posting. |
| `backend/apps/finance/services/payment_collection_service.py` | `apply_payment_to_folio(..., settles="")`; `apply_unallocated_payments_for_booking()` passes `ACCOMMODATION` (room money); `settle_outstanding_balance()` passes `FOLIO_CHARGES` (desk money); `ensure_processed_refund()` stamps a refund with the classification of the allocation it reverses. |
| `backend/apps/finance/services/folio_service.py` | `booking_folio_charge_map()` excludes `settles=ACCOMMODATION`, with the defect recorded in the module comment. |
| `backend/apps/bookings/views_admin.py` | `_folio_clearance_payload()` states accommodation money separately: `accommodation_settlements[]` and `accommodation_settlements_total`, and the docstring explains why it is not netted. All existing keys keep their names and meanings. |
| `backend/apps/finance/migrations/0013_folioposting_settles.py` | **New** — adds the field (additive, blank default, no data migration needed). |
| `backend/apps/finance/migrations/0014_classify_existing_folio_money.py` | **New** — the deployment pass: labels existing `PAYMENT` postings `ACCOMMODATION` (a payment allocation that released the money from guest deposits) or `FOLIO_CHARGES` (`payment.metadata["settled_via"] == "checkout_folio"`), and `REFUND` postings by the allocation they reverse. Idempotent; its reverse clears only the labels it computed. Amounts are never touched. |
| `backend/tests/test_folio_balance_classification.py` | **New** — 11 tests (see §6). |
| `frontend/dashboard/check-out.html` | The bill states "Purchases … less credits … already applied" when credits exist, and lists "Accommodation payments on the folio (for reference)" with the reason they are not offset — the desk can see where the room money went. |
| `frontend/tests-js/test-checkout-clearance-ui.js` | New test: room money is shown as room money, and no client-side arithmetic can re-derive the balance. |
| `frontend/js/api.js` | The folio-clearance contract documents the money fields and that the balance and the gate decision are the server's. |
| `ACCOUNTING_IMPLEMENTATION_REPORT.md`, `docs/FOLIO_BALANCE_CLASSIFICATION.md` | Report §14 and this document. Version files bump to 1.1.23 (`frontend/version.json`, `frontend/js/version.js`, `frontend/sw.js`, built pages). |

No endpoint was added or removed, no new dependency, no POS/receipt change, no stock or
recipe change.

## 5. Deployment

```bash
cd backend
python3 manage.py migrate          # applies finance.0013 → finance.0014
python3 manage.py check
python3 manage.py makemigrations --check --dry-run    # must print "No changes detected"
```

* **0013** only adds a column with a blank default, so it is fast and safe on a live
  database; existing rows keep the historical reading until 0014 runs.
* **0014** is a bounded, idempotent pass over `PaymentAllocation` and `FolioPosting` rows. It
  writes **only** the new `settles` column — no amount, no direction, no date, no description,
  and it never creates or deletes a row. Running it twice changes nothing.
* **Rollback**: `python3 manage.py migrate finance 0012` runs the reverse pass (clearing only
  the labels 0014 computed) and drops the column; the ledger itself is untouched either way.
* No manual step is required for existing guests. After deployment, re-open the folio / gate
  read for the reported booking: "Purchases and charges on the folio" must show the guest's
  consumption (₦3,200 in the reported case) rather than a negative figure, and the gate pass
  must be blocked until it is settled.
* `python3 manage.py guest_receivable_check` remains the read-only way to see the receivable
  split by transaction type before and after the deployment pass.

## 6. Tests and evidence

| Check | Result |
|---|---|
| `python3 manage.py test tests.test_folio_balance_classification --parallel 1` | **Ran 11 tests … OK** |
| `python3 manage.py test tests.test_folio_balance_classification tests.test_checkout_folio_clearance tests.test_laundry_folio_billing --parallel 1` | **Ran 32 tests … OK** |
| `python3 manage.py test tests.test_folio_balance_classification tests.test_checkout_folio_clearance tests.test_finance tests.test_laundry_folio_billing tests.test_guest_receivable_check tests.test_expense_payment_evidence tests.test_auto_checkout --parallel 1` | **Ran 70 tests … OK** |
| `python3 manage.py test tests.test_payments tests.test_payment_rbac tests.test_payments_statements_exports_laundry tests.test_booking_balances tests.test_accommodation_reconciliation tests.test_night_audit_journals tests.test_guest_discounts tests.test_cancellation_refunds tests.test_stays tests.test_guest_checkout --parallel 1` | **Ran 108 tests … OK** |
| `python3 manage.py test tests.test_pos tests.test_pos_api tests.test_pos_department_api tests.test_pos_restaurant_tables tests.test_accounting_inventory_reconciliation tests.test_expense_payment_evidence tests.test_expenses tests.test_finance_api --parallel 1` | **Ran 37 tests … OK** |
| `python3 manage.py test tests.test_financial_summary_report tests.test_report_ranges tests.test_service_request_pos_handoff tests.test_manual_receipts tests.test_refund_cancellation_fee tests.test_guest_services tests.test_housekeeping tests.test_supplier_payment_visibility tests.test_enterprise_upgrade_suite --parallel 1` | **Ran 56 tests … 1 pre-existing failure** (`test_financial_summary_report.py::test_summary_uses_posted_ledger_lines_without_conflating_measures`, a query-count assertion that failed before this release as well) |
| `frontend/tests-js/test-checkout-clearance-ui.js` | **13 tests, 13 pass** |
| `cd frontend && python3 validate.py` | 70 HTML / 30 JS **ALL OK** |
| `python3 /tmp/checkinline.py dashboard/check-out.html` | 3 inline blocks OK |

The 11 new tests, in the order they fight the defect:

1. **the reported booking exactly** — ₦420,000 paid room + ₦3,200 laundry ⇒ folio ₦3,200,
   amount to settle ₦3,200, not cleared, and the room money listed as
   `accommodation_settlements` (₦420,000) rather than as a folio credit;
2. **the desk can collect it** — `settle-balance` returns 201 (it used to be 409
   "already paid"), the money is classified `FOLIO_CHARGES`, `bill_total_before` is ₦3,200,
   the booking's `amount_paid` does not move, the gate pass is then allowed, and a second
   attempt is refused with 409 without inventing a second payment;
3. **a fully paid room with no extras** ⇒ ₦0, cleared, gate allowed, and the desk is told
   there is nothing to collect;
4. **the gate pass is blocked while a real balance remains** (`OUTSTANDING_BALANCE`), and an
   unbilled request adds its own `UNPAID_REQUESTS` blocker;
5. **genuine credits still count** — a ₦1,000 discount reduces the charges to ₦2,200, and a
   ₦5,000 refund of *room* money leaves the laundry bill untouched;
6. **every sales channel still counts** — a real room-service sale (recipe-configured stock,
   depleted by the sale, charged to the folio) plus a laundry bill = ₦5,200 to settle, with
   exactly two folio charge postings and stock down by the four bottles sold;
7. **every surface agrees** — the folio-clearance read, the bookings list, the booking detail
   and the raw folio ledger;
8. **nothing existing is edited or deleted** — a full snapshot of transactions, lines,
   postings, payments, allocations and the booking survives a read and a settlement, and the
   settlement only adds rows;
9. **the deployment pass** — with labels rewound to a pre-release state the balance reads
   **−₦416,800** (the reported defect) and after `classify_folio_money()` it reads ₦3,200 with
   the amounts byte-for-byte unchanged; running it twice is a no-op and the reverse clears
   only its own labels;
10. **a desk settlement and its refund** — the pass labels the desk money `FOLIO_CHARGES` and
    the refund with it, while the room payment keeps `ACCOMMODATION`;
11. **the read-only check command** — `guest_receivable_check` changes no row.

## 7. What deliberately did not change

* `booking.amount_due` / `amount_paid` / `total_amount`, the nightly `ACCOMMODATION_CHARGE`
  postings, `Payment` / `PaymentAllocation` rows and `RefundApplication` evidence — no figure
  was recomputed, edited, blanked or deleted.
* `auto_checkout_due_bookings()` still checks out with `allow_balance_due=True`; the balance
  it leaves behind is now the correct one.
* POS orders, receipts, barcode scanning, laundry walk-in money handling and the accounting
  statements are untouched; the folio-clearance response only gained keys.
* `tests.test_checkout_folio_clearance` (which posts its desk payment through
  `create_posted_transaction`) keeps working: a posting with no classification keeps the
  historical reading, exactly like every pre-1.1.23 row.

## 8. Honest limits

* The sandbox database is an empty development SQLite file (`backend/db.sqlite3`, ~4 KB), so
  the production rows of `J1-20261012-D3DF9254` could not be inspected. The defect, its
  arithmetic and the fix are proved instead by the fixture in
  `tests/test_folio_balance_classification.py`, which reproduces the reported booking through
  the real production paths (`Payment` → `_ensure_main_folio` →
  `apply_unallocated_payments_for_booking` → laundry create/advance/bill → `folio-clearance` →
  `settle-balance`).
* On a historical database, a folio `PAYMENT` posting with no `PaymentAllocation` and no
  `settled_via="checkout_folio"` marker keeps the blank classification and the old reading;
  `guest_receivable_check` remains the read-only way to see such rows.
* There is no headless browser in this environment, so the check-out rendering is proved by
  the shipped source contracts and the JS suite rather than by screenshots.
