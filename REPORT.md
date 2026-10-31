# Batch 01 — Accounting & Reporting

**Repository:** `Jonehotel-06/j1-hotel-management-system` · base `main` @ `c17a84f`
**Branch:** `batch-01-accounting`
**Date:** 2026-10-31

Every changed and new file is in this folder at its repository-relative path.
Copy the tree over a clean checkout and the batch is applied — nothing else
in the repository was touched.

---

## 1. What was wrong, and what it now does

### Task 1 — "Restaurant & bar revenue" was three different businesses added together

`FOOD_BEVERAGE_REVENUE` is credited by the POS for every mode of sale. The
dashboard summed the account and called the result restaurant and bar
revenue, so **room-service food charged to a guest folio was counted as bar
takings**, and a figure that was mostly uncollected credit was presented
beside cash measures.

The split is made on evidence, not on a guess: each revenue line carries a
`source_reference` that matches `PosOrder.reference`, so the order's own
`mode` decides where the money belongs.

| New measure | Contents |
|---|---|
| `restaurant_bar_revenue` | `RESTAURANT` + `BAR` + `TAKEAWAY` orders |
| `room_service_revenue` | `ROOM_SERVICE` orders — reported separately, never merged |
| `unclassified_food_beverage_revenue` | F&B revenue whose order could not be matched |
| `restaurant_bar_collections` | Counter sales **actually tendered** — the cash figure |

The residual bucket is deliberate. Silently folding unmatched revenue into
the restaurant line is how the original defect looked correct; it is now
disclosed as its own line on the profit and loss so it can be investigated
rather than absorbed.

`recognized_revenue` is unchanged in name and meaning, because the owner
dashboard (`index.html`) reads it.

### Task 2 — there was no total expense figure anywhere

`operating_expenses` covered one account prefix. Payroll, inventory
purchases and supplier settlements were each reported somewhere else or not
at all, so no screen answered "what did the hotel spend".

`total_expenses` is now a net of debits minus credits across **every**
`EXPENSE.*` account, which means a reversal or a credit note reduces it
instead of being ignored. Two things are deliberately excluded:

* **Inventory purchases** (`ASSET.INVENTORY`) — buying stock converts cash
  into an asset; it is not a cost until the stock is consumed. It is
  reported as `inventory_acquisitions` so the money is still visible.
* **Supplier settlements** — paying an invoice debits accounts payable. The
  expense was recognised when the invoice was posted; counting the payment
  too would double it.

`expense_breakdown` carries `by_account` and `by_department`, and the
printed pack now prints both, so the headline total can always be traced to
its parts.

> **Note on cost of sales.** The system posts no COGS entry anywhere — a
> consumed inventory item never becomes an expense. `total_expenses` is
> therefore complete with respect to what the ledger records, but the hotel's
> true cost of sales is understated by the value of stock consumed. Fixing
> that needs a consumption posting at the point of sale and is a change to
> how the business records inventory, not a reporting change, so it is out of
> this batch's scope. It is the single largest remaining gap in the accounts.

### Task 3 — a figure was hidden from the screen while remaining inside the totals

`accounting.html` carried a base64-obfuscated regular expression:

```js
var HIDE = new RegExp(atob("dGF4fHZhdHxsZXZ5fHN0YXR1dG9yeQ=="), "i");   // tax|vat|levy|statutory
function hidden(label){ return HIDE.test(label); }
```

It ran over every balance-sheet and expense row label and dropped the
matches **after** the totals had been computed. The statutory tax balance
was therefore invisible as a line but still inside the total above it, so
the sheet appeared not to add up and no one could see why. Obfuscating the
pattern meant a reader could not tell what was being removed.

Both call sites are deleted. Suppression now happens **once, on the server**,
in `statements_service.py`:

* `TAX_PAYABLE` and `STATUTORY_TAX_PAYABLE` fold into **"Other liabilities"**
* `STATUTORY_TAX_ABSORBED` folds into **"Other expenses"**

The balances are carried into a neutral line rather than removed, so **the
statement still foots**. Payroll tax and pension payables are deliberately
*not* suppressed — they are genuine obligations to third parties and the
hotel needs to see them.

### Task 4 — the profit and loss mixed income with money received

Booking payments and refunds were listed as income lines. A deposit for a
stay six weeks away inflated this month's profit; a guest who stayed on
credit contributed nothing. The statement measured cash and called it
earnings.

The statement is now three separated blocks:

```
Income            revenue earned in the period
  less            discounts allowed
Expenses          total expenses (task 2)
  =               Net result
---------------------------------------------------------
Memorandum        money received in the period — collections, not income
```

Payments, refunds and collections appear **only** in the memorandum, below
the net result and labelled as not income.

### Task 5 — the balance sheet reported equity it could not support

The equity section mixed `OWNER_CAPITAL`, `OWNER_DRAWINGS` and
`OPENING_BALANCE_EQUITY` with no retained-earnings roll-forward, so
"Total equity" was arithmetic on accounts that had never been reconciled.

`balance_sheet()` no longer returns `equity`, `total_equity` or
`total_liabilities_and_equity`. It returns **`net_assets`** — total assets
minus total liabilities — which is identical in value to total equity but
makes no claim the ledger cannot support. `earnings_lines` and a
`presentation` block (`equity_section: "withdrawn"` plus an explanatory
note) tell the reader why the section is absent instead of leaving a hole.

### Task 6 — the cash flow statement was removed from the dashboard

It was derived from period movements on cash accounts with no reconciliation
to opening and closing bank balances, so it could not be relied on. The tab,
its table, its caption, its print-pack section and the fetch that fed it are
gone from `accounting.html`.

`CashFlowView` and `statements_service.cash_flow()` are **kept** — an
existing backend test covers them and removing a working API endpoint is a
larger decision than removing a misleading screen. `APP_CONFIG.API_ENDPOINTS.financeCashFlow`
remains defined in `js/config.js` and is now referenced by no page.

### Task 7 — guest receivables were netted into a single misleading number

Debits and credits were netted **across guests**, so one guest's credit
balance cancelled another guest's debt and the hotel's true exposure was
understated. Negative balances were clamped to zero, so money owed back to
guests disappeared entirely.

A new service, `guest_receivables_service.py`, derives every figure from one
queryset and proves itself on every call:

```
control_account_balance
    == gross_receivables − guest_credit_balances + unassigned_receivables
```

If that identity fails the report says so rather than printing a number.
What changed in practice:

* **Outstanding and credit are never netted.** Each folio reports both.
* **Negatives are never clamped.** A credit is money owed back and is shown.
* **Unapplied guest deposits are reported separately** — they are a
  liability, not a reduction of receivables.
* **Other currencies are kept apart**, in `other_currency_balances`.
* **`stay_value_not_yet_charged`** exposes stays in progress whose nights
  have not yet been posted, with the identity
  `gross_receivables + stay_value_not_yet_charged = total_stay_exposure`.
* Aging runs from `booking.check_in`, falling back to `folio.opened_at`.

Three further defects in `folio_service.py` were fixed:

| Defect | Effect | Fix |
|---|---|---|
| Postings summed whatever the transaction status | A `DRAFT` charge moved the guest's balance; a `VOIDED` one was counted **twice** (original + reversal) | Only `POSTED` transactions contribute |
| `VOIDED` folios counted toward the booking | A cancelled folio's charges were billed to the booking | Excluded |
| Currencies summed as interchangeable | A $150 charge added 150 to a naira balance | Only the booking's own currency contributes; the rest is reported by the receivables service |

`folio_balance_queryset` now also annotates `charges_total` and
`credits_total`, because a net zero cannot distinguish a quiet folio from
one holding a large charge against an equally large credit.

**`booking_outstanding_balance` is deliberately unchanged.** It clamps at
zero, which is correct for the front desk — the figure answers "how much do
I collect now", and you do not collect a negative. Eight call sites across
bookings and payment collection depend on that. Reporting callers now use
`booking_balance_detail()`, which returns the same clamped figure *plus* the
credit it suppressed. The checkout response carries both.

### Task 8 — the report export buttons produced unusable files

Two separate faults.

**The root cause was in the frontend**, in `js/api.js`:

```js
// before — params is overwritten by the caller's own opts.params
{ params: { ...(opts.params || {}), start, end, export: extension }, ...opts }
```

Spreading `...opts` *after* `params` meant any caller passing its own
`opts.params` replaced the merged object, so the request went out with no
date range and no `export` flag. The server answered with ordinary JSON, the
blob guard rejected it, and the user got a broken download. `params` is now
destructured out of `opts` and applied last. This alone fixed an existing
failing test (`tests-js` 548).

**Second**, three report endpoints had no export at all. `RevenueReportView`
and `BookingsReportView` ignored `?export=`, returning the JSON envelope
under a `.xlsx` filename — Excel then refused to open it. Both now return
real files through one shared `_export_response` helper.

Export content was also improved:

* The financial summary workbook gained **"Profit and loss"** and
  **"Expenses"** sheets; the PDF gained P&L, collections, expense and
  reconciliation tables.
* Money carries its currency — as a real Excel number format so cells still
  sum, and as `NGN 1,234.56` text in PDFs (reportlab's built-in Helvetica has
  no ₦ glyph and would print a blank box).
* An empty period now states *"No activity was recorded in this period."*
  instead of rendering a bare header row that reads as a failed export.

The Bookings export is wired to a button on `reports.html`. The Revenue
export is **not**, on purpose: that page takes its money figures from the
ledger summary, and a second "revenue" export fed by the payments projection
would re-create exactly the collections-versus-income conflation tasks 1 and
4 set out to remove. The endpoint is public API, so it was fixed; it is just
not re-promoted onto the dashboard.

---

## 2. Verification

### Backend

| Suite | Result |
|---|---|
| `tests/test_accounting_batch01.py` (new, 13) | OK |
| `tests/test_guest_receivables_batch01.py` (new, 48) | OK |
| `tests/test_exports_batch01.py` (new, 12) | OK |
| `tests/test_reports_exports_batch01.py` (new, 12) | OK |
| Full suite, sequential — 2209 tests | **31 failures / 1 error, all pre-existing** |

The full suite had the **same 31 failures and 1 error** on the untouched
checkout before any change was made (email templating, receipt rendering,
inventory unit normalisation, synchronous-email delivery — none in finance).
They are listed in `baseline-known-failures.txt` next to this report, so the
batch can be judged against the real baseline rather than against zero.

**Net effect on the backend suite: no regressions, 85 new tests.**

Six existing tests were edited rather than left failing. Each had encoded a
defect this batch removes, so passing them unchanged would have meant
keeping the defect:

| Test | Why it changed |
|---|---|
| `test_financial_summary_report.py:129` | asserted `tax_accrued` was present → now `assertNotIn` |
| `test_accommodation_reconciliation.py:135,141` | same measure, same change |
| `test_customer_tax_removal.py:278` | same measure, same change |
| `test_night_audit_journals.py` (×2) | asserted the netted receivable figure task 7 corrects |
| `test_payments_statements_exports_laundry.py:118-131` | asserted the equity section task 5 withdraws |
| `test_accounting_inventory_reconciliation.py` · `test_supplier_payment_visibility.py:301` | query budget 7 → 9 (see below) |

The query-budget change is the only one that is not about a defect. The
summary grew from roughly a dozen measures to twenty-two, and two fixed-cost
statements were added: one grouped read of `PosOrder.reference → mode` (how
task 1 tells bar sales from room service on evidence rather than by guessing
from the revenue account) and one grouped read of the expense breakdown
(which backs the task 2 total). Neither scales with measures, rows or days,
so the N+1 property the test protects is intact.

### Frontend

`node --test tests-js/` — **719 tests, 712 pass, 7 fail.**

Baseline was 711 / 701 / **10**. The batch fixes 3 and adds 8 new tests:

* **548** fixed by the `api.js` root-cause repair.
* **601** rewritten — it asserted the statutory figure was still rendered.
  It now asserts the opposite, and that no row-hiding mechanism remains.
* **625** rewritten — it asserted a cash-flow table and an equity section
  that task 5 and task 6 removed. It now pins the balance sheet, the absence
  of the cash-flow statement, and `net_assets`.

The remaining 7 failures are all pre-existing and unrelated to accounting
(HTML page/version manifest, static-hosting rewrite rules, web app manifest
scope, night-audit flash, a `.table-wrap` on one print-pack table, From/To
field state, and a goods-received KPI). They were not touched.

New file `tests-js/test-accounting-batch01-ui.js` (8 tests) pins the
frontend contracts of tasks 1, 2, 3, 4, 7 and 8.

---

## 3. Decisions worth your attention

1. **No cost of sales exists.** The largest accuracy gap in the accounts, and
   not fixable inside a reporting batch. See the note under task 2.
2. **`booking_outstanding_balance` left clamped.** Changing it would alter
   what the front desk collects at eight call sites. The credit-aware figure
   is a new, additive API.
3. **The cash flow API was kept** while the screen was removed. Deleting a
   covered endpoint is a bigger decision than hiding an unreliable screen.
4. **`index.html` was not changed.** It is the owner dashboard, not the
   accounting dashboard, and it reads `d.ledger.recognized_revenue` — a key
   this batch preserves.
5. **`CORS_EXPOSE_HEADERS` was left untouched.** `Content-Disposition` is not
   exposed, so a cross-origin deployment cannot read the server's filename
   and falls back to the client-supplied one. Downloads work today because
   the dashboard is same-origin. Worth adding before any split-origin
   deployment — but it is a settings change with deployment implications, so
   it is flagged here rather than made silently.
6. **Statement views have no export branch.** No screen offers one, so adding
   it would be unreachable code. The helper is in place if you want it.
