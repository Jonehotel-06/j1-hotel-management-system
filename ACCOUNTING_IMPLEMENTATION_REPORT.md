# Implementation report — accounting page reorganisation and the four reported bugs

Baseline commit `2bcaa6c` · versions `1.1.15 → 1.1.16 → 1.1.17 → 1.1.18 → 1.1.19` · everything in this
report is implemented in the repo (frontend + backend), not recommended.

§1–§7 cover `1.1.16`; §8 covers `1.1.17` (supplier payment receipts); §9 covers the five
follow-up reports in `1.1.18` (settings tax controls, the cash-variance approval button,
guest-request bills on the folio, the check-out payment path, and profile-photo uploads);
§10 covers `1.1.19` (the owner/CEO profile and the staff profile's months view).

Scope: the ten accounting requirements (A), the four reported bugs (B1–B4), the
`-₦576,800.00` Guest Receivables investigation, and the tests for all of it.

---

## 1. Files changed

| File | Change |
|---|---|
| `frontend/dashboard/accounting.html` | Overview reduced to six figures; every other table moved into its own collapsible section (Posted transactions, Expenses, Supplier accounts and payables, Cash management, Financial approvals, Financial statements, Night audit & operations, Detailed financial measures, Reports and exports); night-audit drawer table replaced by a reconciliation summary that links to the one authoritative drawer table; approvals section capability-gated; section-specific 403 text; balance-sheet credit-receivable disclosure (+465/−128 lines). |
| `frontend/dashboard/reports.html` | Each report panel names the section it could not read instead of printing one page-wide "no permission to view financial reports" sentence. |
| `frontend/css/dashboard.css` | Styling for collapsible `details.panel` sections, the section chevron, three-up KPI rows, the attention strip, the help panel and the section action rows (+121 lines). |
| `frontend/dashboard/receipts.html` | Bug B2 — guest name cells are `hide-sm cell-nowrap` in the header and the rows. |
| `frontend/dashboard/settings.html` | Bug B1 — the website-image uploaders are gone; the panel says images are managed in the Gallery and links to it, because no `HotelSettings` image field exists for the API to store. |
| `frontend/js/dashboard.js` | Bug B3 — sidebar snapshot (sessionStorage `jone.dash.nav`), role check, paint-once, skeleton, capability-gated first paint. |
| `frontend/dashboard/*.html` (43 pages) | Bug B3 — the same paint runs inline during HTML parse, so the menu is on screen with the page and a limited-access account never sees links it cannot open. |
| `backend/apps/finance/management/commands/guest_receivable_check.py` | **New, read-only** diagnostic: the guest-receivable balance split by transaction type, currency and folio, with the two approved correction routes. Writes nothing. |
| `backend/tests/test_guest_receivable_check.py` | **New**, 9 tests: the credit folio mechanism, the report contents, JSON, drill-down, write-nothing proof, `--fail-on-credit`, mixed currency, clean ledger. |
| `frontend/tests-js/test-night-audit-accounting-ui.js` | Updated for the single-drawer-table rule (see §5) plus a new test that the night-audit section contains no second drawer table. |
| `frontend/tests-js/test-expense-approval-ui.js` | Extended assertion: the capability read re-decides the approvals section, not only the expense table. |

Version bump: `frontend/version.json`, `frontend/js/version.js`, `frontend/sw.js`
cache (`jone-v1.1.16`) and the asset query strings on all 72 built pages.

---

## 2. Redundant UI removed (and where each number still lives)

| Removed from the default view | Where it is now |
|---|---|
| 4 of the 9 overview KPI cards (refunds, actual payments made, supplier payments made, supplier goods received) | Detailed financial measures / Supplier accounts, kept (at zero or not) |
| "Ledger financial measures" table (27 rows) | **Detailed financial measures**, collapsed |
| Night-audit drawer expected/counted/variance table | **Cash management** — the single authoritative drawer table (linked from the night audit) |
| Night-audit tenders/discrepancies/in-house folios | **Night audit & operations**, collapsed, plus one small attention strip in the overview |
| Balance-sheet / cash-flow explanatory prose | "How these statements are built" help panel in Financial statements |
| Supplier stages as three KPI cards in the overview | **Supplier accounts and payables** (goods received, invoices recognized, payments made) |

No table or figure was deleted from the application: the six overview cards are
the only figures stated once, and every measure the ledger reports is still
reachable on the page.

### Notes on the three open design questions

1. **Tender / cash-drawer duplication** — resolved as *one* drawer table:
   Cash management owns the expected-vs-counted rows; the night audit reports
   the reconciliation outcome (drawers on the date, how many counted, how many
   still unreconciled, net variance) and links to Cash management. The
   night-audit checks themselves are untouched — the same `cash_drawers`,
   `discrepancies` and `unbilled_in_house_folios` data is still read from the
   same endpoint and still displayed (the drawer table was the duplicate, not
   the check).
2. **Which measures survive the six-card overview** — Recognized revenue,
   Collections, Operating expenses, Net operating result, Outstanding supplier
   payables, Total cash disbursed. *Net operating result* is the label the owner
   asked for, rendered from the existing `gross_operating_result` measure
   (revenue − operating expenses − accrued payroll): **no calculation changed**.
3. **Night audit collapsible or its own page** — kept as a collapsible section on
   Accounting. A separate page would move the daily pack away from the ledger it
   reconciles and would break the suite that pins the pack inside Accounting;
   collapsible gives the same clear default view without either cost.

---

## 3. Functionality preserved

Nothing in the posting, approval or reporting logic was touched.
`frontend/dashboard/accounting.html` still calls the same endpoints, through the
same `API_ENDPOINTS` map, with the same permissions; the backend was changed only
by adding a read-only management command.

- Immutable ledger, balanced journal posting, revenue recognition, collections
  and refunds, supplier invoices/payments, expenses, payroll, drawer
  reconciliation, approvals, statements, tax records, RBAC, exports and API
  contracts are unchanged: the finance/permissions suites below pass on the same
  code paths as before.
- Every export button (CSV, Excel, PDF) and both export-strip strings are
  unchanged; balance sheet and cash flow keep every line and every calculation.
- Cash Management remains the single cash-drawer table and is linked from the
  night audit and from a cash-variance approval row.

---

## 4. Bugs fixed

### B1 — "images are not uploading"
Uploads that work (proved by `backend/tests/test_image_upload_paths.py`, 7 tests):
gallery (PNG/JPEG/WEBP, with disguised-file rejection), room-type images,
physical-room images, facilities. Uploads that never worked: the
**Settings → website images** uploaders — there is no `HotelSettings` field for
`hero_image`, `intro_image`, `experience_image`, `location_image`,
`policy_image` or `contact_image`, so `PATCH /api/admin/settings/` answered
`200` and stored nothing.
Fix: `renderImageManager()` now renders an uploader only for fields the API
actually stores; when none qualify it shows "Website images are managed in the
Gallery" with a link to the Gallery instead of a control that silently no-ops.
No endpoint was added and no field was invented.

### B2 — guest names wrapping on the receipt page
`dashboard/receipts.html`: the guest header and body cells are
`class="hide-sm cell-nowrap"`, so a long name stays on one line; the mobile
guest line inside the reference cell keeps its own wrapping rule.

### B3 — sidebar reloading on every navigation / limited-access link flash
The sidebar was rendered only after the capability list arrived, so each
navigation showed an empty (or broadly-role) menu and then replaced it.
Fix: the rendered menu is stored per role in sessionStorage
(`jone.dash.nav = {role, html, at}`) and painted from an inline script during
HTML parse, before the capability read; `renderSidebar` skips `innerHTML` when
the markup is unchanged. A snapshot is painted only when its role matches the
stored session role — otherwise the menu stays empty (`navSkeleton()` bars on a
first visit), so an account that lacks a page never sees its link flash. First
paint is gated on `capsReady || !canDefer` and reconciles once on success *and*
on failure.

### B4 — "Approvals couldn't be loaded" and the page-wide permission message
Root cause: `GET /api/admin/finance/approvals/` requires
`payment.refund.approve` (admin, manager, general_manager, accounts_manager).
Every other Accounting reader — notably the **accountant** role, which holds
`reports.financial.view` but never `payment.refund.approve` — had the request
refused, and the page answered *any* 403 with
"Your account does not have permission to view financial reports", so one
section's permission gap read as a broken page.
Fix (no access granted, server-side authorization untouched):
- the approvals list is requested only when the account holds the approval
  capability; otherwise the section states that refunds, discounts, voids and
  adjustments are approved by a manager or the accounts manager;
- a 403 from that endpoint renders the same section-specific state instead of an
  error block;
- every other 403 now names its own section ("…to view cash management",
  "…to view expense records" on Reports, "…to view the financial overview"), and
  a 401 says the session expired;
- an expense approver keeps the inline Approve/Reject controls
  (`expense.approve` path unchanged), and a cash-variance row links to the
  authoritative drawer table instead of offering a decision.

---

## 5. Guest Receivables `-₦576,800.00` (requirement A.7)

**What the account is.** `ASSET.GUEST_RECEIVABLE` is debit-normal: guest charges
debit it (accommodation nights in `accommodation_service`, POS sales in
`order_service`, room-charge laundry in `laundry_service`) and money applied to a
guest's folio credits it (`apply_payment_to_folio`), as does a POS counter
tender. Folio statement items are derived from those same receivable lines
(`ledger_service` rejects any folio posting that does not come from one), so:

> the sum of every folio balance **is** the guest-receivable balance.

**Root cause of a credit (negative) balance.** Charges stop arriving while
applied payments keep standing, on folios whose stay never consumed what was
paid for: a booking cancelled or marked no-show after the payment was applied, a
guest who checks out early (only elapsed nights are ever recognized), or a rate
change/discount applied after allocation. A prepayment allocated before the
nights post is the same mechanism for one night only and reverses as the nights
accrue; a cancelled or no-show allocation never reverses, which is what leaves a
round, permanent figure like `-₦576,800.00`. The folios stay open with a credit
balance until the guest is refunded or the balance is reclassified.

**Evidence.** `tests/test_guest_receivable_check.py` reproduces the mechanism
from the staff-facing path (payment collected → `ensure_payment_collection`,
allocation → `apply_payment_to_folio`):

```
Guest receivables (ASSET.GUEST_RECEIVABLE) through 2026-10-16
  debits   guest charges posted      25000.00
  credits  applied to guest folios   55000.00
  balance                           -30000.00   <- CREDIT-NORMAL
  folio balances total              -30000.00  (1 folio(s), 1 in credit)
Receivable movements by transaction type
  ACCOMMODATION_CHARGE   DEBIT   25000.00
  PAYMENT_ALLOCATION     CREDIT  55000.00
Folios carrying a credit balance
  FOL-20261016-1C94FBA1FE4C  Ada Obi  OPEN  charges 25000.00  credits 55000.00  balance -30000.00
```

**What could not be done here.** The exact ₦576,800.00 cannot be attributed from
this repository: there is no production database in the workspace
(`backend/db.sqlite3` is empty), so per-folio attribution of the live figure is
an operational step, not a code change. Nothing was hidden, rounded or removed
to make the balance look better, and no posted transaction was edited.

**Correction, through the existing approved process only.**
1. `python manage.py guest_receivable_check` (new, read-only) — prints the
   balance, the movement split by transaction type and currency, and the folios
   in credit with their postings; `--json` for bookkeeping, `--folio REF` to
   drill into one folio, `--fail-on-credit` for monitoring. It never writes.
2. Refund the guest balance through the existing refund request/approval
   workflow (posts an immutable debit against guest receivables), **or**
3. post a balanced reclassification journal through Accounting → *Post journal
   entry*: Debit `ASSET.GUEST_RECEIVABLE`, Credit `LIABILITY.GUEST_DEPOSITS` —
   the liability account that actually describes money held for a guest. Both
   routes add evidence; neither edits history.

**On the page.** The balance sheet shows the amount exactly as the ledger
reports it and, when the receivable is credit-normal, adds a note naming the
cause and both correction routes — the figure is disclosed, never hidden.

**Second finding, reported by the same command.** The statements present one
functional currency and query receivable lines without a currency filter, so
receivable lines posted in another currency would inflate the same figure. The
command now splits the account by currency and warns loudly if more than one
appears; the correct response is a currency revaluation by the accountant, not a
journal, and the warning says so. `test_receivable_lines_in_another_currency_are_reported_not_blended`
pins it.

---

## 6. Tests

| Suite | Result |
|---|---|
| `cd frontend && node --test tests-js/` | **393 tests — 391 pass, 1 skip, 1 fail** |
| `frontend/validate.py` | **ALL OK** (70 HTML, 30 standalone JS) |
| `python manage.py test tests.test_guest_receivable_check` | **9 pass** (new) |
| `python manage.py test tests.test_finance tests.test_finance_api tests.test_report_ranges tests.test_approvals_inbox tests.test_checkout_folio_clearance tests.test_capabilities tests.test_image_upload_paths tests.test_separate_approver_control tests.test_role_permissions_matrix tests.test_guest_receivable_check` | **77 pass** |
| Page structure | tag-balance parse of `accounting.html` and `reports.html`: no unclosed or stray tags; every `id`, `[data-tbody]`, `[data-rp]`, `[data-na]`, `[data-bs]`, `[data-cf]` hook referenced by the page script exists in the markup; every inline script passes `node --check` |

Known failures, both pre-existing and unrelated to this change:
- `tests-js/test-pwa-dashboard-only.js` — root-manifest test (was failing before
  this pass, still failing, untouched).
- `tests/test_financial_summary_report.py::test_summary_uses_posted_ledger_lines_without_conflating_measures`
  — 1 of 6, unchanged before and after this pass.

Two suites were updated *because the UI contract changed*, not to make a
failure disappear:
- `test-night-audit-accounting-ui.js` no longer demands a second drawer table in
  the night-audit section (requirement A.3 forbids duplicate drawer tables); it
  now asserts the night-audit section has no `data-tbody="na-drawers"`, that Cash
  management owns `data-tbody="acct-cash"`, and that the drawer,
  discrepancy and in-house data are still read and displayed.
- `test-expense-approval-ui.js` now also asserts that the capability read
  re-decides the approvals section.

---

## 7. Remaining issues and honest limits

- **No browser was run this pass.** The sandbox has no Playwright/Chromium, so
  the collapsible sections, the skeleton and the attention strip are verified by
  markup/JS tests, `validate.py` and static analysis only — not by a rendered
  page. A quick visual pass on Accounting (dark and light, 390 px and 1280 px)
  is still worth doing before calling the layout final.
- **The `-₦576,800.00` figure itself** needs the live database to attribute: run
  `guest_receivable_check` against production data and close it with a refund or
  the reclassification journal.
- **Not changed, deliberately:** the separate `finance.controls.manage` /
  `reports.financial.view` permission model, the approvals endpoint's
  capability (widening it would grant access the owner did not ask for), the
  ledger, and any historic posting.
- **Not covered by tests yet:** the inline sidebar snapshot script is duplicated
  across 43 pages; `tests-js/test-department-page-access.js` covers the
  behaviour, but a future page added without the snippet would simply fall back
  to the skeleton — acceptable, worth remembering.

---

## 8. Follow-up (1.1.17) — supplier payment receipts

### 8.1 What was reported

"There's no option to upload a receipt transferred to suppliers, no button to record
that payment is made already."

### 8.2 What the code actually did

The supplier-payment step existed, but only for **one** of the two ways an expense can
leave the hotel:

| How the expense was recorded | Ledger at posting | Payment step offered |
|---|---|---|
| `CASH` / `BANK_TRANSFER` / `CARD` | credits cash / bank clearing / card clearing — the money is treated as **already out** | none (correct: there is no payable left), and **no way to attach the transfer slip** |
| `PAYABLE` (on account) | credits `LIABILITY.ACCOUNTS_PAYABLE` | "Record supplier payment" → posts Dr payables / Cr the tender, stamps paid, and **does** take a receipt file |

So the receipt upload existed but was unreachable for a bill recorded with the default
payment method, and nothing on the screen explained why no button appeared. On top of
that, the expense drill-down showed a dash for "Supplier payment" on an already-paid bank
transfer, and never linked an uploaded payment receipt.

### 8.3 What changed

| File | Change |
|---|---|
| `frontend/dashboard/accounting.html` | New **Payment receipt** action on every posted expense that has no pending settlement (money already out, or an already-settled payable); the transfer-receipt dialog (file / link / reference / date paid); explicit payment-method labels in Create expense ("already paid" vs "On account / not paid yet") plus a "Supplier invoice / receipt link" field that the API already accepted; the payables section now explains the two-step flow **in the backend's own words** (it renders the reporting service's `payable_settlement_posting_note`); the drill-down lists every attachment (supplier document, payment receipt file, payment receipt link) and says "Paid at posting — BANK_TRANSFER (no separate supplier payment to record)" instead of a dash |
| `backend/apps/finance/services/expense_service.py` | `attach_expense_payment_evidence(...)` — stores receipt file/link/reference/date on an expense whose money has already left, writes one append-only `PAID` event that states `ledger_effect: "none"`, and posts nothing |
| `backend/apps/finance/views.py`, `urls.py` | `POST /api/admin/finance/expenses/<reference>/payment-evidence/` (`expense.manage`, multipart) + audit-log entry `EXPENSE_PAYMENT_EVIDENCE_ADDED` |
| `backend/tests/test_expense_payment_evidence.py` | **New**, 7 tests (see 8.4) |
| `frontend/tests-js/test-supplier-payment-receipt-ui.js` | **New**, 6 tests pinning the two-button rule, the dialog fields, the create-dialog wording and the drill-down rendering (run against the page's real `expenseDetailHtml`) |
| `docs/OWNER_FINANCE.md` | Owner-facing "Guest receivables" and "Paying a supplier — the two ways money leaves" sections |

**One narrow endpoint was added, deliberately.** No existing route could store evidence
on an already-posted expense: the settlement endpoint correctly refuses a non-payable
expense (it would post a second cash-out), the detail endpoint is read-only, and posted
expenses refuse `save()`. The new route reuses the settlement serializer shape, the same
`expense.manage` capability, the same append-only event model and the same audit log,
and it never touches the ledger.

### 8.4 Evidence that no figure moves

`tests/test_expense_payment_evidence.py` asserts, for a bank-transfer expense carrying a
real uploaded PNG plus a link, a reference and a date:

- the receipt is stored and the expense reads as paid;
- the append-only event says `evidence_only: true` and `ledger_effect: "none"`;
- `FinancialTransaction`, `FinancialLine` counts and totals are **identical** before and
  after, and `financial_summary()` returns byte-identical
  `outstanding_supplier_payables`, `total_cash_disbursements`, `supplier_payments_made`
  and `operating_expenses`;
- an on-account expense that is still unpaid is **refused** here with a message pointing
  at "Record supplier payment" (the step that releases the payable);
- a settled payable can still gain the receipt it missed, and the settlement's own
  reference is never blanked out or overwritten;
- a receptionist without `expense.manage` gets 403; a future payment date is refused.

The last point found a real defect while writing the tests: the first version of the
service wrote the (empty) `payment_reference` on every call, which wiped the bank
reference a settlement had already stored. The write is now limited to the fields the
caller actually supplied.

### 8.5 Tests for this follow-up

| Suite | Result |
|---|---|
| `python manage.py test tests.test_expense_payment_evidence` | **7 pass** |
| `python manage.py test tests.test_expense_api tests.test_expenses tests.test_expense_payment_evidence tests.test_finance tests.test_finance_api tests.test_guest_receivable_check tests.test_supplier_payment_visibility tests.test_approvals_inbox tests.test_report_ranges tests.test_separate_approver_control` | **68 pass** |
| `cd frontend && node --test tests-js/` | **399 tests — 397 pass, 1 skip, 1 fail** (the same pre-existing manifest failure) |
| `frontend/validate.py` | **ALL OK** |

### 8.6 Still open from this follow-up

- **A bill recorded with the wrong method cannot be reclassified.** If a supplier bill
  was entered as "Bank transfer" while the money had not actually left, the ledger says
  it did. There is no void/reverse route for a posted expense today, so the supported
  correction is a manual journal (Accounting → Post journal entry) agreed with the
  accountant, or reversing and re-recording the expense. I did not invent a reclassify
  endpoint for it; that decision is the owner's.
- **No browser pass.** The new dialog is covered by markup/vm tests and the API by
  Django tests, not by a rendered page.


---

# 9. Follow-up reports fixed in `1.1.18`

Five reports, each verified against the shipped code before anything was changed.
No financial calculation was altered by any of them: the only posting added (9.3) is
the laundry charge the ledger already posts when an order is billed — the same
transaction type, the same accounts, the same amount, posted at the moment the guest
receives the service instead of at the desk's convenience. 9.1, 9.2, 9.4 and 9.5 post
nothing at all.

## 9.1 Government taxes removed from Settings

**Report.** "Remove government taxes from settings."

**What was actually there.** `dashboard/settings.html` exposed the hotel's *own*
statutory-tax accrual: accrual on/off, rate, and basis (three inputs, plus their keys in
the page's editable-field list). Nothing on that page has ever taxed a guest — since the
2026-10-11 owner policy no customer transaction carries billed tax — so the controls only
offered a change that could not affect a single guest bill.

**Fix.** The three controls and the three keys in the field list are gone, so the
dashboard can no longer send a tax setting at all. The page still states the policy in
plain words ("no tax is added to guest bills") and now says where the hotel's own
statutory figure is reported. Deliberately **not** removed: the `HotelSettings`
`statutory_tax_*` columns, `apps/finance/services/tax_policy.py`, the statutory accrual in
the ledger, the *Statutory tax accrued (not billed to guests)* statement measure, and every
historical line. No migration, no deleted data, no changed figure.

**Evidence.** `frontend/tests-js/test-settings-tax-removal.js` (6 tests): no tax control
and no tax key remain; every remaining field in the list still has an input; the policy
sentence is still published; the Accounting statement measure still exists; and no other
dashboard page sends a tax key. `backend/tests/test_customer_tax_removal.py` (tax-free
customer billing, statutory accrual, historical tax kept) still passes.

## 9.2 A button to approve a cash drawer variance ("approve cash collected")

**Report.** "No button to approve cash collected in accounting."

**Root cause.** When a cashier records a physical count that does not match the expected
cash, the drawer session stops at `PENDING_REVIEW` and cannot close until a manager
decides. The API for that decision already existed
(`POST /api/admin/finance/cash-sessions/<reference>/variance-review/`, capability
`payment.refund.approve`), but the only reference to it anywhere in the UI was the
Financial approvals row linking to `#cash-management` — and the Cash management table
rendered no row actions at all. A dead end: the money stayed unreconciled and the cashier
could not open a new controlled drawer.

**Fix.** Cash management (the one authoritative drawer table) gained a **Decision** column.
A session in `PENDING_REVIEW` shows **Review & approve** for anyone with
`payment.refund.approve`; everyone else reads "Manager review required" instead of a
control they cannot use. The dialog takes the decision and a reason (a rejection must state
one), calls the existing variance-review endpoint — no new route, and the browser never
posts an adjustment — and then re-reads the page so the closed drawer and the approvals
list agree. The approvals-row link is now "Review in Cash management".

**Evidence.** `frontend/tests-js/test-cash-variance-review-ui.js` (7): the button appears
only for `PENDING_REVIEW` + the capability, the decision goes to
`resourceAction("financeCashSessions", reference, "variance-review", …)`, no journal or
adjustment is posted from the page, a rejection demands a reason, and both outcomes are
stated plainly. `test-expense-approval-ui.js` updated for the new wiring (12 tests).
`backend/tests/test_finance_api.py::test_manager_uses_controlled_endpoint_to_finish_cash_variance_review`
still passes.

## 9.3 The folio records a guest's request bill when the guest receives it

**Report.** "Guest folio is not recording guest request bills, but on print receipt it
appears."

**Root cause.** Restaurant, bar and room-service orders post their folio charge the moment
they are marked delivered (`apps/pos/services/order_service.py::_post_order_charge`, with
the `FolioPosting` that keeps the folio and its receivable line in step). An in-house
**laundry** order did not: it posted only when somebody pressed *Bill to folio* or when the
desk settled the bill at check-out. In between, the guest had the laundry back, the
check-out bill stated the money under "Requests not yet paid for", the printed receipt
showed it — and the folio, the guest's own statement, and every folio figure the desk read
showed nothing.

**Fix.** `advance_laundry_order` bills an in-house order to the guest's folio at the moment
it is marked `DELIVERED`, reusing the existing idempotent `bill_laundry_order` (so a second
delivery or an explicit bill press can never double-charge). Walk-in orders have no folio
and are never charged to one; a zero-priced order stays deliverable without inventing a
charge; a cancelled order is never billed. Revenue recognition timing therefore matches the
restaurant/bar rule — the guest receives the service, the charge posts — with the same
accounts and amounts as the manual bill it replaces.

**Evidence.** `backend/tests/test_laundry_folio_billing.py` (9): delivery posts exactly one
folio charge for the order total with a matching receivable; the folio balance, the folio
charge map and the check-out bill agree; re-billing is a no-op; walk-ins, zero-priced and
cancelled orders post nothing; settling at the desk still clears it. **Negative control:**
with the new call disabled, 5 of the 9 fail (no folio posting, blocked gate pass, wrong
check-out figures) and all 9 pass with it. The 62-test laundry / guest-service / check-out /
stay batch is green.

**Limit that remains, stated openly.** A request that has been *placed* but not yet
delivered is not a bill yet — the guest can still cancel it — so it is not on the folio. It
is still stated at check-out as an unpaid request, and the desk bills and collects it in
one step (9.4).

## 9.4 Check-out offers "record the payment" as a first-class choice

**Report.** "No button to mark it as paid during checkout for guests that requested
something and then makes payment."

**Before.** With money outstanding, pressing **Check out** asked one question — "Check out
with an outstanding balance?" — and the way to take the money sat behind a different button
(and, for a request that had never been billed, behind a dialog the desk had to know to
open).

**Fix.** The check-out action now opens a dialog that states the amount owed and offers the
payment as the primary choice:

* **Record payment & clear the bill** — opens the folio clearance bill, which bills any
  request that is still not on the folio and applies the money in the same step through the
  existing `settle-balance` endpoint (nothing is billed twice); or
* **Check out anyway (balance stays owed)** — the deliberate override, unchanged; and
* **Cancel**.

The gate-pass rule is untouched: it stays server-decided and blocked until the folio is
clear, whichever choice the desk makes.

**Evidence.** `frontend/tests-js/test-checkout-clearance-ui.js` (12, +2 new): the dialog,
both choices, `allow_balance_due: true` on the override only, the settlement going through
`settleBookingBalance(..., include_unbilled: true)`, and no invented payment route.

## 9.5 Profile photos: stored-and-verified, or a message saying why not

**Report.** "Profile images are still not uploading and it doesn't display any error message
why it fails."

**Root causes addressed.**

1. `PATCH /api/auth/profile/` — and the administrator's staff edit — answered `200` and
   nothing more. Neither the API nor the page said whether the photo had reached media
   storage, and the page toasted "Profile updated." regardless. A photo written to storage
   nothing can serve therefore looked exactly like a success.
2. A storage backend that refuses the write surfaced as an unhandled failure instead of a
   message a staff member can act on.

**Fix.** A new `apps.core.storage.verify_stored_upload()` confirms the saved object is
present in its storage and has a public address, and both write paths now store through
`save_upload()` — a refused write becomes `502 STORAGE_UPLOAD_FAILED` with a safe message
and the record is left untouched. When a photo was submitted, the response carries
`profile_image_stored` plus `profile_image_warning` naming the storage backend in use. The
dashboard uses both: the profile page probes the published URL with a real `Image()` load
(cache-busted, 8-second timeout) and, when the API or the browser says the photo is not
showing, raises an alert-styled inline message with the reason and an error toast — it only
claims "your photo is showing" when the browser actually loaded it. The staff editor
reports the same warning to the administrator instead of a plain success toast.

**Evidence.** `backend/tests/test_profile_image_uploads.py` (18): both surfaces store the
photo and publish a URL that storage really serves; a non-image is refused with a field
error the UI can render; a storage that does not hold the object answers
`profile_image_stored: false` with a reason naming the backend; a storage that cannot be
checked reports the exception *type* only (no internal detail leaks); a refused write
answers 502 and records nothing; a replacement photo is stored; a save with no photo says
nothing about photos. `frontend/tests-js/test-profile-photo-ui.js` (6). The existing
image-upload suite (`test_image_upload_paths.py`, 7) still passes.

**Honest limit.** The code paths are verified here; whether a photo *displays* in
production depends on the deployment's media storage being writable and publicly readable.
That is exactly what the new message reports, with the storage backend class named, so the
administrator has something concrete to fix instead of a silent no-op.

# 10. Follow-up (1.1.19) — the owner's own profile, and the staff profile's months view

## 10.1 What was reported

Two things, in the owner's words: the Administrator's own profile carried staff
material it should never have — *"my salary, attendance, shift, leave etc …
Admin should have its own profile as a CEO and not as staff"* — and the staff
profile's styling *"is not professional yet; the months section should use the
occupancy page style of design"*.

## 10.2 What the code actually did

* `GET /api/admin/staff-operations/my-profile/` resolves the caller from the
  session and served **any** role other than GUEST — `User.is_staff_member` is
  true for every non-guest role, the Administrator included. It then called
  `ensure_staff_profile()`, which **created an employee record (an `STF-…`
  employee code and a department) for the owner account** the first time the
  owner opened their own profile.
* The page had no role branch at all: `profile.html` called `loadSelfService()`
  unconditionally, so the CEO saw salary structure, payslips, attendance,
  shifts, leave, disciplinary, reviews and a lateness-deduction ledger.
* `OperationalStaffDirectoryView` (`/api/admin/users/directory/`) — the picker
  behind shift assignment, dispatch, attendance, leave and disciplinary forms —
  excluded only GUEST, so the owner was offered as an assignable employee.
* The Staff page's roster query (`role__in`) began with `ADMIN` and the role
  filter carried an "Administrators" option: the CEO appeared as a staff row.
* The months block was a six-column table whose "Worked *n* / *n* scheduled ·
  Missed *n* / *n* scheduled" chips were competing with the numbers beside
  them, and the record tabs were a loose row of buttons with no container.

## 10.3 What changed

| File | Change |
|---|---|
| `backend/apps/staff_operations/views_self_profile.py` | The Administrator is refused **before** the profile lookup, with a plain reason ("The Administrator account is the hotel owner …"). Refusing first is also what stops an employee record being manufactured for the owner. |
| `backend/apps/accounts/views_admin.py` | The operational staff directory excludes `User.Role.ADMIN` together with GUEST, so the owner cannot be assigned a shift, task, attendance record, leave request or disciplinary case. |
| `frontend/dashboard/profile.html` | Role-aware page: `isOwner` comes from the signed-in session; the staff panel is hidden **and never fetched** for the owner; a new owner console (`#pf-owner`) shows the owner's account and sign-in facts plus tiles for Staff & access, Settings, Accounting and Reports, each filtered through `JONE.dashboard.canOpenPage()`. For staff, the months table became an occupancy-style month calendar (year navigation, chip legend, `No record` cells, a detail panel holding the month's payroll periods), the record tabs became a segmented strip, panes gained real section headings, and the attendance range became a filter bar. |
| `frontend/dashboard/staff.html` | The roster query and the role filter no longer include ADMIN; a caption states the rule; a labelled **Owner account** button keeps the account reachable through the existing edit form. |
| `frontend/css/dashboard.css` | `.ss-tabs`, `.ss-pane h3`, `.owner-tiles`/`.owner-tile`, and the `.mo-*` month calendar, mirroring the occupancy calendar's cells, chips, legend and phone behaviour (one row per cell below 560px). |
| `docs/USAGE.md` | "Profiles: the owner's profile and a staff member's profile". |

## 10.4 No figure and no record changes

Nothing is deleted and nothing is recalculated. Attendance, roster, payroll,
document or audit rows that already exist — including any created for the owner
account before this change — are untouched, and payroll generation (which
iterates staff profiles) is not modified. What changed is **who is offered**
(the pickers and the roster) and **what is shown on the owner's own profile**.

## 10.5 Evidence

| Check | Result |
|---|---|
| `tests.test_staff_self_profile` + `tests.test_operational_directory_api` | **16 OK** — new: the owner is refused *before* any staff profile is created; the refusal is specific to the owner (a general manager still answers 200); the owner is never offered by the directory, and asking for `roles=ADMIN` returns nobody. |
| Regression batch — `tests.test_staff`, `tests.test_staff_operations`, `tests.test_staff_operations_api`, `tests.test_query_efficiency`, `tests.test_lateness_deductions` + the two above | **Ran 75, OK** |
| `frontend/tests-js/test-owner-ceo-profile-ui.js` | **9 tests** — 6 on the shipped page, 3 that *run* the page's own code in the stub DOM (tiles rendered, no staff figure in the rendered console, the empty state when no screen is permitted). |
| `frontend/tests-js/test-profile-month-calendar-ui.js` | **5 tests** — runs the page's calendar code: 12 cells, `No record` for months the API did not return, chips only for non-zero counts, the current month marked, year navigation clamped to the current year, and a month's payroll periods in its detail panel. |
| `test-profile-monthly-salary-ip-ui.js`, `test-workforce-ui.js` | reconciled and green (the roster no longer filters for the owner; the months block is pinned to the calendar) |
| `test-profile-photo-ui.js`, `test-lateness-ui.js`, `test-barcode-scanner.js`, `test-sidebar-profile-link.js`, `test-department-page-access.js` | green |
| `python3 validate.py` | 70 HTML / 30 JS **ALL OK** |
| `node --test tests-js/` | **434 tests: 432 pass, 1 skipped, 1 pre-existing failure** (root web-app manifest scope, `test-pwa-dashboard-only.js`) |

## 10.6 Honest limits

* The month calendar shows the API's own 24-month history and navigates by
  year; months older than that are not queryable because the endpoint does not
  return them.
* The Administrator remains the hotel's administrator: this change does not
  remove the role, its capabilities or its authority — only the owner's staff
  record and the owner's presence in staff lists.
* Whether the live production owner account had already had a staff profile
  created for it cannot be verified from this repository (its database is not
  part of it). If one exists it is deliberately left in place; it is no longer
  surfaced as a staff record and the owner is no longer offered as staff.
