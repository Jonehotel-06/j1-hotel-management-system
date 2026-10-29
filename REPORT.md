# Accounting agent — audit and repair report

Repository inspected: `https://github.com/Jonehotel-06/j1-hotel-management-system`
(HEAD `afe9af7`, "procurement agent report,"). Frontend accounting page:
`frontend/dashboard/accounting.html`. Work is isolated in
`ai_updates/accounting_agent/`; the upstream tree was **not** modified and
nothing was pushed.

Scope: the "N unresolved room or billing checks" number and its View action,
Procurement → Accounting purchase-order visibility and supplier payables, the
supplier-payment approval workflow, a form audit, and the reconciliation /
security / testing requirements. Nothing outside Accounting, reconciliation and
the directly necessary procurement/payment integration was touched: the
Procurement and Inventory dashboards, their serializers, views, services and
models are **unmodified** (see §5 for what those agents must do).

### Where each requested deliverable is

| the brief asked for | section |
|---|---|
| verified root causes | §1 |
| changed-file manifest | §3 and `MANIFEST.txt` |
| DB/API changes and migration rationale | §4 |
| test commands and actual results | §6 (including the full-suite run and the pinned UI tests) |
| Procurement/Inventory dependencies | §5 |
| exact paths + line numbers for simple manual fixes | §7 |
| safe apply/rollback | §8 and `apply.sh` / `rollback.sh` |
| remaining unverified issues | §10 |
| the financial-form audit (no field removed, and why) | §9 |
| what was changed, in prose | §2 |

---

## 1. Verified root causes

Every cause below was reproduced from the code and, where marked *(proved)*, by
a test in this change set.

### R1 — the "unresolved" number was the length of one undifferentiated list, and named the wrong subject

`backend/apps/finance/services/night_audit_service.py` (original) built a single
`discrepancies` list by appending four different conditions inside
`daily_reconciliation`:

| original line | condition | what it actually is |
|---|---|---|
| 116 | `CASH_VARIANCE` | cash-drawer check |
| 122 | `DRAWER_STILL_OPEN` | cash-drawer check |
| 144 | `OCCUPIED_NOT_IN_HOUSE` | room check |
| 178 | `IN_HOUSE_NO_FOLIO` | billing check |

`frontend/dashboard/accounting.html:1546` then rendered
`discrepancies.length + " unresolved room or billing check"`. So a night with
five drawer variances and no room or billing issue told the accountant
"5 unresolved room or billing checks". The sentence named a subject the number
did not measure, and the same number drove both the KPI
(`data-na="discrepancies"`) and the alert strip.

### R2 — the list contained conditions that were never discrepancies, and missed data that was there

* **A guest who had not arrived yet was reported as an occupied room with no
  stay.** `occupied_room_ids` came from `BookingRoom` filtered by
  `blocking_booking_q()`, and `apps/bookings/services/availability.py:25`
  defines `BLOCKING_STATUSES = (CONFIRMED, CHECKED_IN)`. A CONFIRMED booking
  arriving that same day therefore produced `OCCUPIED_NOT_IN_HOUSE` for every
  room on the arrivals list, all afternoon, until the guest reached the desk.
  *(proved: `test_a_pending_arrival_and_a_folio_on_hold_are_not_reported_as_discrepancies`)*
* **A folio on hold was reported as a missing folio.** The original query at
  lines 154 and 164 filtered `folio__status=Folio.Status.OPEN` only, while
  `Folio.Status` also has `ON_HOLD`. An in-house stay whose folio was on hold
  produced `IN_HOUSE_NO_FOLIO` *and* a folio balance of zero, so the guest's
  outstanding balance disappeared from the pack as well. *(proved, same test)*
* **A stay with several live folios silently used one of them.** `stay_folios`
  was a dict comprehension over `Folio.objects.filter(...)`, so the second open
  folio overwrote the first and the balance shown was whichever row the
  database returned last.
* **A stay with no room allocation was invisible.** `_stay_room()` returned
  `None` and the loop simply skipped that stay, so an in-house stay with no
  physical room neither appeared as a discrepancy nor in the occupancy figures.

### R3 — the count could not be inspected, reviewed, or told apart from history

The page rendered the list as `reference · detail` bullets
(`accounting.html` "Discrepancies & checks" block) and the alert strip's
**Review** action was `href="#night-audit-ops"` — a scroll, not a drill-down.
Nothing carried an id, an amount, a record status, a reason, or a next action,
and there was no way to record "I looked at this" or "this is explained".
A check reviewed last week and a check appearing for the first time were
indistinguishable, and a condition that had since cleared simply vanished with
no trace that it had ever been raised.

### R4 — Accounting could not see submitted purchase orders at all, and no payable could be linked to one

* The Accounting page said so itself:
  `accounting.html:177` — *"Full supplier register, purchase orders and goods
  receipts live in Procurement and Inventory."* There was no purchase-order
  panel on Accounting.
* A read-only hand-off **did** exist (`/api/admin/inventory/accounting/purchase-orders/`,
  `apps/inventory/views.py:842`), but only the Procurement page consumed it.
  Accounting's own "Outstanding supplier payables" table
  (`accounting.html:1086-1100`) was built purely from `latest.expenses`.
* `apps/finance/models.py` `Expense` had **no link to a purchase order**
  (original lines 527-625: `supplier_reference` is a free-text supplier
  document number, not an order reference). Consequences:
  * a payable raised from an order was an unrelated record — order, receipt,
    payable and payment could not be traced to each other;
  * nothing prevented the same delivery being billed twice;
  * nothing capped a payable at the value actually received, so the ordered
    total could be billed before any goods arrived.

### R5 — a supplier payment needed no approval and had no state of its own

`apps/finance/services/expense_service.py` (original) `settle_expense_payment`
(lines 330-350) required only `expense.manage` and took **no amount**. That
capability is granted to `CASHIER` (`apps/accounts/capabilities.py:113`) and is
inherited by `WAITER` (`:152-160`), so the person who could record a supplier
bill could also discharge it, on any amount, with no second signature and no
record distinguishing "payment proposed", "payment approved" and "payment
executed". `Expense.settlement_transaction` is a `OneToOneField`, so one
settlement is all the ledger design supports — but nothing said so to a caller.

### R6 — procurement-recorded supplier payments never reach the ledger (documented, not "fixed")

`apps/inventory/services/inventory_service.py:469-551`
(`record_purchase_order_payment`) appends a `PurchaseOrderPayment` row and moves
the order to `PAID` when the payments cover the **ordered** total. It posts no
`FinancialTransaction`, and the amount is not tied to the value received. It
requires `procurement.approve`, so it is not an unapproved path — but a payment
recorded there is invisible to Accounting's payables, cash-flow and supplier
disbursement figures. Fixing that is a Procurement/owner decision (§5, §8);
this agent did not invent a posting rule or silently re-point those payments.

---

## 2. What was changed

### 2.1 Unresolved checks: one record per condition, with a review trail

New `backend/apps/finance/services/reconciliation_service.py` is now the single
place reconciliation conditions are computed. It returns one dict per
condition with:

`check_id` (stable hash of business date + type + object), `category`
(`ROOM` / `BILLING` / `CASH_DRAWER` / `PROCUREMENT`), `type` + human `type_label`,
`severity` (`ERROR` / `WARNING` / `INFO`), `object_reference` plus the specific
`room` / `folio` / `stay` / `booking` references, `amount`, `currency`,
`record_status` (the status of the underlying record), `why_flagged`,
`recommended_action`, and the derived `status` (`OPEN` / `ACKNOWLEDGED` /
`DISMISSED` / `RESOLVED`) with `reviewed_by`, `reviewed_at`, `review_note`.

Conditions now computed, and the fixes they encode:

| check type | category | severity | fix |
|---|---|---|---|
| `CASH_VARIANCE` | CASH_DRAWER | ERROR | unchanged logic, now labelled as a drawer check and counted separately |
| `DRAWER_STILL_OPEN` | CASH_DRAWER | ERROR | unchanged, counted separately |
| `ROOM_OCCUPIED_NOT_IN_HOUSE` | ROOM | ERROR | **only** when the booking's own status is `CHECKED_IN` |
| `ROOM_ARRIVAL_NOT_CHECKED_IN` | ROOM | INFO | a `CONFIRMED` arrival is informational, never "unresolved" |
| `STAY_WITHOUT_ROOM` | ROOM | ERROR | in-house stay with no live room allocation is now visible |
| `IN_HOUSE_NO_FOLIO` | BILLING | ERROR | a folio in `ON_HOLD` counts as a folio (no false "missing folio") |
| `FOLIO_ON_HOLD` | BILLING | WARNING | the real condition, stated once |
| `FOLIO_MULTIPLE_OPEN` | BILLING | WARNING | several live folios are reported instead of one silently winning |
| `FOLIO_BALANCE_DUE` | BILLING | WARNING | balance per folio, with the amount and the guest |
| `PO_RECEIVED_NOT_BILLED` | PROCUREMENT | WARNING | goods received with nothing (or not enough) billed |
| `PO_PAYABLE_EXCEEDS_RECEIVED` | PROCUREMENT | ERROR | a payable larger than the value received |

`summary` reports `unresolved`, `unresolved_room_and_billing`,
`unresolved_cash_drawer`, `unresolved_procurement`, the warning/informational
counts, `dismissed` and `resolved_since_review`. Only `ERROR` conditions count
as unresolved; warnings and informational rows are counted beside them so the
one number keeps one meaning.

`night_audit_service.daily_reconciliation()` keeps its historic response shape
(`discrepancies`, `cash_drawers`, `unbilled_in_house_folios`, `daily_flash`,
`tenders`, `in_house_count`) and now derives it from the checks, adding
`checks`, `check_summary` and `resolved_reviews`. No existing caller breaks
(proved by `tests/test_night_audit_journals.py`, unchanged and passing).

Review workflow: `GET /api/admin/finance/reconciliation/checks/` and
`POST /api/admin/finance/reconciliation/checks/<check_id>/review/` with
`decision` ∈ `ACKNOWLEDGED | ESCALATED | DISMISSED` and a mandatory note.
Decisions are appended to `ReconciliationCheckReview` (append-only model; `save()`
refuses an update, `delete()` refused by the shared immutable base), mirrored
into the audit log as `RECONCILIATION_CHECK_REVIEWED`, and **never** edit the
drawer, folio, room, order or payment the check is about (proved:
`test_a_review_never_edits_the_records_it_is_about`). Dismissing is not
suppression — it is a dated, attributed, reversible statement; a later
acknowledgement re-opens the same check and both rows stay.

### 2.2 Purchase orders and supplier payables

* `Expense.purchase_order` (nullable `PROTECT` FK to `inventory.PurchaseOrder`)
  with `UniqueConstraint(["purchase_order"], name="one_supplier_payable_per_purchase_order")`.
  The link makes order → receipt → payable → payment traceable; the constraint
  makes double-billing impossible at the database level, not just in the UI.
* `services/procurement_payable_service.py`:
  `raise_payable_from_purchase_order()` raises the payable as an ordinary
  on-account `Expense`, so it travels the **existing** submit → approve → post
  controls and lands in the same liabilities the reports already read. It
  refuses `DRAFT`/`SUBMITTED`/`CANCELLED` orders, refuses orders with nothing
  received ("a submitted purchase order is not proof of delivery"), caps the
  amount at the value actually received
  (`GoodsReceiptLine.quantity_received × unit_cost`), refuses a second payable,
  and locks the order row so two accountants cannot race the check.
* `purchase_order_payable_summary()` / `purchase_order_payables_index()` give
  the read-only payable position per order: ordered total, received value,
  payable raised, payable outstanding, unbilled received value, the individual
  payables with their status, and `can_raise_payable`.
* API: `GET /api/admin/finance/procurement/payables/` and
  `POST /api/admin/finance/procurement/purchase-orders/<reference>/payable/`
  (`expense.manage` **and** `reports.financial.view`, so cashiers/waiters cannot
  raise payables).

### 2.3 Supplier payment approval workflow

New `SupplierPaymentRequest` model with explicit states —
`PROPOSED → SUBMITTED → APPROVED/REJECTED → EXECUTED`, plus `CANCELLED` — and
`services/supplier_payment_service.py` enforcing every transition:

| action | endpoint | capability | what it refuses |
|---|---|---|---|
| propose | `POST /supplier-payments/` | `expense.manage` | unposted/settled non-payable expense, amount > outstanding, amount ≠ full balance, missing bank/card reference, future date, a second open request for the same payable |
| submit | `POST /supplier-payments/<ref>/submit/` | `expense.manage` | any state but `PROPOSED`; a payable whose balance moved |
| review | `POST /supplier-payments/<ref>/review/` | `expense.approve` | self-approval (central `require_independent_approver`), expired requests, non-`SUBMITTED` states |
| execute | `POST /supplier-payments/<ref>/execute/` | `expense.manage` | anything not `APPROVED`; re-execution (idempotent, no second posting) |
| cancel | `POST /supplier-payments/<ref>/cancel/` | requester or `expense.approve` | cancelled/executed requests; requires a reason |

`settle_expense_payment()` now requires **either** an `expense.approve` holder
**or** an `APPROVED SupplierPaymentRequest` for that exact payable, re-validated
inside the locked transaction; it accepts an optional `amount`, refuses any
amount above the outstanding balance and any amount that is not the full
balance (one settlement posting per payable is what the ledger supports), and
records the authority (`APPROVER_DIRECT` / `APPROVED_PAYMENT_REQUEST`) and the
request reference on the append-only expense event. Execution and the `EXECUTED`
stamp share one `transaction.atomic()` block, so a failed posting can never be
reported as a successful payment.

The accounts manager's existing direct route is unchanged (regression-proved):
the fix removes the capability gap without changing a posted figure.

### 2.4 The Accounting screen

* `accounting.html` — the night-audit section now renders the checks as a table
  (Subject · Check · Record · Amount · Status · Why it is flagged · Recommended
  next action · decision), with a summary line that names each subject and a
  **Review** action per row. The alert strip no longer says "unresolved room or
  billing checks" over a mixed number: it shows `N unresolved room or billing
  checks`, `N unresolved cash drawer checks` and `N purchase orders received but
  not billed` separately, and its action opens the full record list.
* New section **Purchase orders submitted to Accounting** — supplier, order
  reference, order status, receipt progress, ordered total, received value,
  payable raised, still-unpaid, plus *Lines* (order lines, goods receipts,
  payables) and *Raise payable* (capped at the value received, server-validated).
* New section **Supplier payment requests** — request, payable, supplier,
  amount, method, status, raiser, with Submit / Approve / Reject / Execute /
  Cancel offered per state and per capability.
* The payables table keeps *Record supplier payment* for an approver and offers
  *Propose payment* to a user who cannot approve, so the UI never offers a
  control the server would refuse.
* `frontend/js/config.js` — five endpoint keys added
  (`financeReconciliationChecks`, `financeProcurementPayables`,
  `financePurchaseOrderPayable`, `financeSupplierPayments`,
  `procurementAccountingOrders`).
* Both new sections fail closed: they render only what the server returns, and
  an unreadable endpoint shows a section-specific message, not a page-wide
  permission sentence.

---

## 3. Changed-file manifest

`MANIFEST.txt` is the machine-readable list used by `apply.sh`. Summary:

| file | kind | change |
|---|---|---|
| `backend/apps/finance/models.py` | M | `Expense.purchase_order` + unique constraint + index; `ApprovalRequest.Type.SUPPLIER_PAYMENT`; new `SupplierPaymentRequest`; new `ReconciliationCheckReview` |
| `backend/apps/finance/migrations/0017_accounting_payables_checks_and_payment_requests.py` | A | the additive migration for the above |
| `backend/apps/finance/services/reconciliation_service.py` | A | the reconciliation checks, summaries and review recording |
| `backend/apps/finance/services/night_audit_service.py` | M | `daily_reconciliation` derived from the checks; historic keys kept |
| `backend/apps/finance/services/procurement_payable_service.py` | A | payable position per order; raise a payable from received value |
| `backend/apps/finance/services/supplier_payment_service.py` | A | propose/submit/review/execute/cancel |
| `backend/apps/finance/services/expense_service.py` | M | settlement authority + amount validation + `purchase_order`/`metadata` on create |
| `backend/apps/finance/services/approval_service.py` | M | `SUPPLIER_PAYMENT` threshold mapping, capability mapping, routing |
| `backend/apps/finance/serializers.py` | M | `purchase_order` + `payment_requests` on expenses; the new request/raise/review serializers |
| `backend/apps/finance/views.py` | M | 9 new endpoints; `ValidationError` import; supplier-payment routing on the generic approvals endpoint |
| `backend/apps/finance/urls.py` | M | routes for the above |
| `backend/tests/test_accounting_agent_reconciliation.py` | A | 9 tests |
| `backend/tests/test_accounting_agent_procurement_payables.py` | A | 11 tests |
| `backend/tests/test_accounting_agent_supplier_payments.py` | A | 12 tests |
| `frontend/dashboard/accounting.html` | M | checks table + drill-down + review, purchase-order section, supplier-payment section, split alert strip, payables action |
| `frontend/js/config.js` | M | 5 endpoint keys |
| `frontend/tests-js/test-accounting-agent-ui.js` | A | 7 tests |

No Procurement or Inventory file is modified.

---

## 4. Database and API changes, and why the migration is safe

Migration `finance/0017_accounting_payables_checks_and_payment_requests`
(depends on `finance.0016_ledger_reporting_indexes` and
`inventory.0013_alter_stockmovement_department`) does exactly five things:

1. **`CreateModel ReconciliationCheckReview`** — a new table.
2. **`CreateModel SupplierPaymentRequest`** — a new table.
3. **`AddField Expense.purchase_order`** — nullable, `ON DELETE PROTECT`,
   default `NULL`; every existing expense keeps `NULL` and therefore keeps its
   current meaning ("not raised from a purchase order"). No row is rewritten.
4. **`AddConstraint one_supplier_payable_per_purchase_order`** —
   `UNIQUE(purchase_order)`. Because SQL unique indexes ignore `NULL`, the
   existing expenses (all `NULL`) cannot collide. It is created **after** the
   column exists and no data exists that could violate it, so the migration
   cannot fail on a populated database.
5. **`AlterField ApprovalRequest.type`** — choices only; `SUPPLIER_PAYMENT` is a
   new allowed value. No column type, length or data changes.

Rationale: the payable link and its uniqueness must be enforced where two
requests cannot race (the database), not only in a service check; the review
table must be append-only evidence owned by the finance app; the payment
request needs its own states rather than overloading `Expense`. Nothing is
dropped, renamed or back-filled, no existing financial history is touched, and
`python manage.py makemigrations --check` reports **no pending changes** after
the migration is applied.

Reversal: `python manage.py migrate finance 0016_ledger_reporting_indexes`
drops the two tables, the column and the constraint. Rows created after the
change (reviews, payment requests) are dropped with them — export first if they
must be kept (`rollback.sh` prints the `dumpdata` command).

New API surface (all under `/api/admin/finance/`):

| method + path | capability |
|---|---|
| `GET reconciliation/checks/` | `reports.financial.view` **or** `expense.approve` |
| `POST reconciliation/checks/<check_id>/review/` | `expense.manage` **or** `expense.approve` |
| `GET procurement/payables/` | `reports.financial.view` / `expense.approve` / `procurement.approve` |
| `POST procurement/purchase-orders/<reference>/payable/` | `expense.manage` **and** `reports.financial.view` |
| `GET,POST supplier-payments/` | `expense.manage` **or** `expense.approve` |
| `GET supplier-payments/<reference>/` | same, object-scoped |
| `POST supplier-payments/<reference>/submit/` | `expense.manage` |
| `POST supplier-payments/<reference>/review/` | `expense.approve` |
| `POST supplier-payments/<reference>/execute/` | `expense.manage` |
| `POST supplier-payments/<reference>/cancel/` | `expense.manage` / `expense.approve` |

Existing endpoints and response shapes are unchanged; the night-audit payload
only gained keys. Amounts, currencies and references are validated on the
server; the client-supplied amount is never trusted (the payable balance is the
ceiling) and exceptions are surfaced through the project's existing error
envelope (no stack traces, no internal identifiers).

---

## 5. Dependencies on Procurement and Inventory (for those agents)

These are the cross-dashboard changes this agent deliberately did **not** make:

1. **Purchase-order payments are not ledger payments.** `record_purchase_order_payment`
   (`apps/inventory/services/inventory_service.py:469-551`) records money out
   against the *ordered* total and posts no ledger entry, so Accounting's
   payables, cash-flow and supplier-disbursement figures never see it, and an
   order can read `PAID` while Accounting still shows it unpaid. The two
   dashboards therefore disagree. Either (a) PO payments should create/execute a
   `SupplierPaymentRequest` (the model and endpoints now exist), or (b) the
   hotel decides PO payments are deposits outside the supplier-payables ledger
   and Procurement labels them as such. **Owner decision required.**
2. **Receipts are the accounting basis.** The payable is capped at
   `GoodsReceiptLine.quantity_received × unit_cost`. If Storekeeping under-
   receipts a delivery, the payable is capped too; the fix belongs in
   Procurement/receiving, not in Accounting.
3. **Second deliveries after the payable is raised.** `UNIQUE(purchase_order)`
   means one payable document per order. A later delivery is reported every day
   by `PO_RECEIVED_NOT_BILLED` (with the unbilled amount) but cannot be added to
   that payable, because posted expenses are immutable. The hotel must decide
   between a supplementary payable (needs a payable-adjustment model) and a
   separately keyed supplier invoice. Not invented here.
4. **Drafts stay private.** `ACCOUNTING_VISIBLE_STATUSES` excludes `DRAFT`, and
   Accounting sees nothing until Procurement submits. Keep it that way.
5. **Supplier identity.** The payable copies the supplier *name* and
   `PurchaseOrder.supplier_reference` into the expense (there is no
   `Expense.supplier` FK). If Procurement/Inventory want supplier-level ageing,
   that needs a nullable supplier FK on `Expense` — a shared decision, so it was
   not made unilaterally.
6. **Procurement UI.** The Procurement page should show, per order, whether a
   payable has been raised and whether it is paid (read
   `GET /api/admin/finance/procurement/payables/`), so the two screens stop
   disagreeing. No Procurement file was edited by this agent.

---

## 6. Test commands and actual results

Environment used (Linux, Python 3.13, SQLite test database):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "Django>=5.2,<5.3" "djangorestframework>=3.15,<4" djangorestframework-simplejwt \
            django-cors-headers drf-spectacular python-decouple python-dotenv dj-database-url \
            Pillow qrcode reportlab openpyxl boto3 "django-storages[s3]" requests whitenoise celery redis
```

Applied to a scratch copy of the repository (`/tmp/verify`), then:

```bash
cd backend
python manage.py check                      # System check identified no issues (0 silenced).
python manage.py makemigrations --check     # No changes detected
python manage.py test tests.test_accounting_agent_reconciliation --parallel 1
python manage.py test tests.test_accounting_agent_procurement_payables --parallel 1
python manage.py test tests.test_accounting_agent_supplier_payments --parallel 1
python manage.py test tests.test_finance_api tests.test_finance tests.test_night_audit_journals \
    tests.test_expenses tests.test_expense_api tests.test_expense_payment_evidence \
    tests.test_payments_statements_exports_laundry tests.test_accounting_inventory_reconciliation \
    tests.test_audit_procurement_regressions tests.test_admin_approval_override \
    tests.test_approvals_inbox --parallel 1
```

| suite | result |
|---|---|
| `tests.test_accounting_agent_reconciliation` (new) | **9 tests — OK** |
| `tests.test_accounting_agent_procurement_payables` (new) | **11 tests — OK** |
| `tests.test_accounting_agent_supplier_payments` (new) | **12 tests — OK** |
| 11 pre-existing finance/expense/procurement suites listed above (81 tests) | **OK** (after the `ValidationError` import fix described in §8) |
| the three new modules together (final re-run) | `Ran 32 tests in 20.392s — OK` |
| full backend suite (`python manage.py test tests`) | **Ran 2022 — 1999 passed, 22 skipped, 1 pre-existing environmental failure (no `mysqlclient`)** (see §6.1) |

Frontend:

```bash
cd frontend
python3 validate.py                          # Checked 73 HTML files, syntax-checked 32 standalone JS files. ALL OK
node --check ../js/config.js                 # (also: every inline <script> block in accounting.html)
node --test tests-js/                        # 688 tests: 680 pass, 8 fail
node --test tests-js/test-accounting-agent-ui.js   # 7 tests — OK
```

The **8 failing JS tests are pre-existing**: the same 8 fail on the untouched
`source_repo` checkout (`# pass 673, # fail 8` before adding the new test file),
so this change introduces **no** new frontend test failure. They are:
`116` (72-page version.json stamp), `120` (supplier goods-received measure),
`148` (production rewrite config), `293` (night-audit UI assertions),
`493` (manifest scope), `571` (statutory figure), `595` (balance-sheet UI),
`609` (table scroll wrapper). They are reported in §8 rather than papered over.

### What the new tests prove (mapping to the brief)

| requirement | test |
|---|---|
| unresolved-count and record retrieval | `test_the_count_is_split_by_subject_and_cash_is_not_called_room_or_billing`, `test_night_audit_keeps_its_shape_and_gains_the_drilldown` |
| View action and permissions | `test_a_cashier_cannot_read_the_pack`, `test_a_reader_without_financial_visibility_cannot_record_a_review`, `openReconciliationChecks`/`reviewCheck` in `test-accounting-agent-ui.js` |
| stale/resolved discrepancy handling | `test_the_count_follows_the_underlying_status`, `test_acknowledging_keeps_the_check_open_and_records_who_looked`, `test_dismissing_requires_a_reason_and_is_reversible` |
| procurement order visibility | `test_accounting_sees_submitted_orders_with_the_payable_position`, `test_a_draft_order_is_never_handed_to_accounting` |
| payable creation and duplicate prevention | `test_a_partial_receipt_raises_a_payable_for_what_actually_arrived`, `test_the_same_order_cannot_be_billed_twice` (service **and** DB constraint) |
| approval and rejection | `test_the_lifecycle_moves_through_distinct_states_and_posts_once`, `test_a_rejected_request_leaves_the_payable_untouched`, `test_a_requester_cannot_approve_their_own_payment_request` |
| partial payment and remaining balance | `test_a_partial_receipt_...`, `test_a_second_delivery_after_the_payable_shows_as_unbilled_remainder`, `test_over_payment_and_partial_amounts_are_refused` |
| invalid and excessive payment amounts | `test_over_payment_and_partial_amounts_are_refused`, `test_an_already_settled_payable_cannot_be_proposed_again` |
| reconciliation after a payment | `test_received_goods_without_a_payable_are_reported_and_then_cleared`, `test_a_second_delivery_after_the_payable_shows_as_unbilled_remainder` |
| historical financial record preservation | `test_history_is_preserved_and_audited`, `test_a_review_never_edits_the_records_it_is_about`, `test_the_lifecycle_..._posts_once` (one transaction, no double posting) |

### 6.1 Full backend suite — actually run

```bash
cd backend && python manage.py test tests --parallel 1
# Ran 2022 tests in 752.539s
# FAILED (failures=1, skipped=22)
```

The single failure is **pre-existing and environmental**, not caused by this
change:

```
FAIL: test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls
      (tests.test_deployment_readiness.ProductionSettingsBootAndFailFastTests)
django.core.exceptions.ImproperlyConfigured: Error loading MySQLdb module.
Did you install mysqlclient?
```

That test boots the production settings with a `mysql://` URL; this environment
has no `mysqlclient` and no MySQL server. Running the same module on the
**untouched** `source_repo` checkout gives the identical result
(`Ran 14 tests … FAILED (failures=1)`, same test), so it is a missing optional
dependency in the audit sandbox, not a regression. Everything else — 1999
passed, 22 skipped — including the 32 new tests in this change set.

### 6.2 The pinned Accounting UI tests, individually

| test file | result |
|---|---|
| `tests-js/test-night-audit-accounting-ui.js` | 7 pass / **1 fail** — pre-existing baseline #293, `"Accounting renders the night audit daily flash and checks"`. It asserts `/data-na="adr"/`; that tile does not exist in the pristine `source_repo` file either (`grep -c 'data-na="adr"'` → `0` in both trees), so the test is asserting a KPI nobody ever added. Not touched here: adding an ADR tile is a report decision, not an accounting repair. |
| `tests-js/test-expense-approval-ui.js` | **12 pass / 0 fail** |
| `tests-js/test-cash-variance-review-ui.js` | **7 pass / 0 fail** |
| `tests-js/test-supplier-payment-receipt-ui.js` | **6 pass / 0 fail** |
| `tests-js/test-accounting-agent-ui.js` (new) | **7 pass / 0 fail** |

---

## 7. Exact file paths and line numbers for simple manual fixes

These are small, optional follow-ups that stay inside Accounting/Procurement.
Line numbers refer to the **patched** files in this folder.

1. `frontend/dashboard/accounting.html:292` — the KPI card still reads
   *"Checks needing attention"* with the sub-label *"Drawer, room and billing
   checks"* while `data-na="discrepancies"` now holds only the **error** checks.
   If the owner wants the split on the card itself, split this one `<div>` into
   three and fill them from `check_summary.unresolved_room_and_billing`,
   `.unresolved_cash_drawer` and `.unresolved_procurement` in `renderChecks()`
   (`accounting.html`, function `renderChecks`). Copy/markup only — the numbers
   already exist; the table below the card already separates them.
2. `frontend/dashboard/accounting.html:1356` (`receiptLabel`) — turns
   `RECEIVED_IN_FULL` into "received in full"; a status such as
   `PARTIALLY_RECEIVED` reads acceptably but `NOT_RECEIVED` reads "not received"
   only by luck of word order. If the wording matters, replace the generic
   underscore-replace with an explicit map; it is one function, no logic.
3. `backend/apps/finance/services/reconciliation_service.py:441`
   (`_procurement_checks`) — procurement checks are computed for **today only**
   (`if business_date != hotel_today(): return []`, line 451). If Accounting
   wants payable conditions on historical dates, that guard is the single line
   to revisit; it exists so no history is invented.
4. `backend/apps/finance/services/supplier_payment_service.py:66`
   (`ACTIVE_STATUSES`) — this tuple decides which requests block a new one for
   the same payable. Removing `APPROVED` from it would allow a second proposal
   alongside an approved one; keep it unless the hotel wants parallel proposals.
5. `apps/inventory/services/inventory_service.py:469-551` (Procurement's file,
   **not** changed here) — the PO payment that posts no ledger entry; see §5.1.

---

## 8. Safe application and rollback

```bash
# 1. apply (from the repository root, on a clean branch)
bash ai_updates/accounting_agent/apply.sh

# 2. migrate (additive)
cd backend && python manage.py migrate finance

# 3. verify
python manage.py check
python manage.py makemigrations --check --dry-run   # expect "No changes detected"
python manage.py test tests.test_accounting_agent_reconciliation \
    tests.test_accounting_agent_procurement_payables \
    tests.test_accounting_agent_supplier_payments
cd ../frontend && python3 validate.py && node --test tests-js/test-accounting-agent-ui.js
```

`apply.sh` backs up every file it overwrites (and records which files did not
exist) under `ai_updates/accounting_agent/.rollback/`, so
`bash ai_updates/accounting_agent/rollback.sh` restores the tree byte-for-byte
and prints the exact reverse-migration command
(`python manage.py migrate finance 0016_ledger_reporting_indexes`).

The scripts themselves were exercised, not just written:

```text
$ bash ai_updates/accounting_agent/apply.sh
Applied 17 file(s) to /tmp/applytest
$ diff -rq ai_updates/accounting_agent/backend/apps/finance /tmp/applytest/backend/apps/finance   # no differences
$ bash ai_updates/accounting_agent/rollback.sh
Restored 17 file(s) in /tmp/applytest
$ diff -q backend/apps/finance/models.py <pristine source_repo>/backend/apps/finance/models.py
# identical ("models.py pristine"); the new 0017 migration file is gone
```

and the migration was forward/reverse/re-forward tested on a fresh SQLite
database:

```text
$ DATABASE_URL=sqlite:////tmp/agenttest.sqlite3 python manage.py migrate
  ... Applying finance.0017_accounting_payables_checks_and_payment_requests... OK
$ ... python manage.py migrate finance 0016_ledger_reporting_indexes
  Unapplying finance.0017_accounting_payables_checks_and_payment_requests... OK
$ ... python manage.py migrate finance
  Applying finance.0017_accounting_payables_checks_and_payment_requests... OK
```

Operational notes for go-live:

* The two new sections read endpoints that exist in this change set; deploying
  the frontend without the backend would show section-specific "couldn't be
  loaded" messages rather than broken markup, but deploy **backend first**.
* No back-fill is required. Existing payables have `purchase_order = NULL` and
  behave exactly as before; existing posted expenses, ledger transactions,
  audit logs and payment records are untouched.
* `SupplierPaymentRequest` starts empty. Nothing becomes unpayable: an
  `expense.approve` holder can still settle a posted payable directly.

---

## 9. Form audit (§6 of the brief) — what was checked and what was not removed

No field was removed. Each field that looks like a duplicate was traced to its
uses; every one of them is load-bearing, and removing it would either break a
financial calculation, a report/export, an API contract, or historical data.

| field | other field it could be confused with | why both stay (evidence) |
|---|---|---|
| `Expense.supplier_reference` | new `Expense.purchase_order` | `supplier_reference` is the *supplier's* document number and is written to the ledger as `external_reference` (`expense_service.post_expense`), shown in the expense detail and used by exports. `purchase_order` is the *internal* order link. Different facts. |
| `Expense.receipt_url` / `receipt_file` | `payment_receipt_url` / `payment_receipt_file` | the first pair is the document the expense was recorded *from*; the second is proof the money actually *left*. `tests/test_expense_payment_evidence.py` pins the distinction (a bank expense already paid at posting can gain the second without posting anything). |
| `Expense.payment_reference` | `SupplierPaymentRequest.payment_reference` | the expense field is the settled payment's bank reference (read by the payables view and the summary). The request field is the *intended* reference before execution; execution copies it onto the expense. Keeping both is what lets a proposal be corrected without editing a posted expense. |
| `Expense.amount` | `PurchaseOrder.total_amount` / `received_value` | ordered value, received value and payable value are three different amounts; the audit found them collapsed (payable could equal the ordered total). They are now separate and the reconciliation reports the difference. |
| `payment_method` on the expense form | `SupplierPaymentRequest.method` | the expense's method decides the *posting* (which tender account is credited at posting) and is immutable; the request's method is how the payment will be made later. For a `PAYABLE` expense they are different events. |
| expense form `supplier_id` select vs free-text `payee` | — | the select resolves to `payee` (name) plus `supplier_reference`; the free-text field is the fallback for a one-off payee that is not in Procurement. Both are used by `create_expense` and covered by `tests/test_expense_api.py`. |

Left as-is **by decision, documented rather than changed**: the manual "Create
expense" form still lets an accountant hand-key a purchase that Procurement has
already ordered. It is not wrong (it covers costs with no purchase order), but
the new *Raise payable* action should be preferred for anything with an order,
and the form's help text was not reworded to avoid changing a tested copy
string. A follow-up could add a "linked purchase order" selector to the manual
form; that is a UX change, not a defect.

---

## 10. Remaining unverified issues

1. **`tests.test_deployment_readiness` cannot pass in this sandbox** — it needs
   `mysqlclient` and a MySQL server to boot production settings. It fails
   identically on the pristine checkout. Run it in CI, where MySQL is available,
   before deploying.
2. **Procurement payments and the ledger (§5.1)** — verified by reading
   `record_purchase_order_payment` (no `FinancialTransaction` is created), not by
   an end-to-end reconciliation against real data. Resolving it needs the owner
   decision described there.
3. **Concurrency under production MySQL/MariaDB** — the new paths take row locks
   (`select_for_update`) and rely on a database unique constraint, and the
   tests run on SQLite, where `select_for_update` is a no-op. The duplicate
   payable guarantee is enforced by the constraint, which behaves the same on
   MySQL; the "two accountants raise at the same instant" race was reasoned
   about, not load-tested.
4. **`FOLIO_MULTIPLE_OPEN` / `FOLIO_BALANCE_DUE` do not attempt to allocate a
   payment between several folios.** They flag the condition; consolidation
   remains a manual, approved step.
5. **Second deliveries after a payable is raised** are reported, not billed
   (§5.3). Not fixable without a new payable-adjustment decision.
6. **Supplier-level ageing** — the payable has no supplier FK (§5.5), so
   "how much do we owe supplier X" is still derived by name matching. Unchanged
   behaviour, unchanged limitation.
7. **The 8 pre-existing JS test failures** listed in §6 are untouched by this
   change (they fail identically on the pristine checkout) and belong to
   whichever agents own those pages.
8. **No browser/UI automation was run** for the new panels. They are verified by
   static assertions (`frontend/tests-js/test-accounting-agent-ui.js`), by
   `frontend/validate.py`, and by `node --check` on every inline script block;
   `renderChecks`/`renderPurchaseOrders`/`renderSupplierPayments` were not
   exercised in a real browser.
9. **Permission matrix breadth** — the new endpoints use existing capabilities
   (`reports.financial.view`, `expense.manage`, `expense.approve`,
   `procurement.approve`). Whether `STOREKEEPER`/`PROCUREMENT_OFFICER` should see
   the payable position was not re-decided; they can reach it only through
   `procurement.approve`.
