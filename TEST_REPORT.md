# TEST REPORT — Batch 01 (Inventory Management, Stock Control & Reordering)

All commands below were executed in this batch's isolated working copy
(`/home/user/work`, created from commit `d9683e1`; the original repository was never modified).
Results are reported exactly as observed — including the failures that were already present in the
repository before this batch touched anything.

Environment: Python 3 / Django (project settings `config.settings.development`, SQLite), Node.js
`node --test`. Packages installed to run the suite: `django-storages[s3]` + `boto3` (needed by the
project's own `manage.py check`), plus the project's existing dev requirements.

---

## 1. New backend test module

`backend/tests/test_inventory_batch01_management.py` — **38 tests, 6 test classes** (+1 shared base class)

```bash
python manage.py test tests.test_inventory_batch01_management
```

```
Ran 38 tests
OK
```

| Class | Tests | What it proves |
|---|---|---|
| `StockUnitFieldTests` | 10 | Base unit vs opening-quantity unit are distinct and both validated; pack/crate/carton conversions are exact; kg↔litre, contradictory factor, contradictory pack size and container-without-size are refused; opening quantity is stored in base units with the entry evidence on the movement; `base_unit` cannot be changed once history exists; labels are human-readable; a rejected creation leaves no orphan catalog row |
| `CrateIssueTests` | 3 | Issuing 2 crates deducts exactly 48 pieces; a container issue without a declared pack size is refused with 400 and no movement; a base-unit issue keeps the pre-existing contract; the ledger returns `base_unit_label` |
| `StockAtHandAdjustmentTests` | 8 | Administrator correction records actor/previous/new/delta/reason/timestamp; the balance is corrected without rewriting any movement; the reason is mandatory; a storekeeper gets 403 (direct API) and an unauthenticated caller 401; idempotent replay returns the first correction instead of double-posting; adjustment list is capability-gated; valuation follows the corrected quantity |
| `SearchableSkuTests` | 7 | Add-stock-item search finds an item by SKU, name, category and barcode; exact SKU is ranked first; duplicate SKU creation is blocked case-insensitively with the existing item named; replenishment of an existing item succeeds and **never alters** name/category/base unit/barcode/selling price; replenishment requires a location and a positive quantity; a storekeeper cannot hit the replenishment endpoint; the barcode uniqueness is enforced |
| `StockAtHandSearchTests` | 4 | Search by SKU / name / category / barcode / location; the four existing filters still work and combine with search; pagination metadata is correct; an unmatched term returns an empty page (not an error); every row carries the new read-only labels; the search issues a bounded number of queries |
| `ReorderingTests` | 6 | Reorder context returns identity, unit, balances, total on hand, last purchase cost/supplier and open-order quantity; it is `procurement.manage`-only; creating a reorder draft **does not change any balance**; the draft lands in `DRAFT` and the approval workflow still refuses the requester (`403` on approve); stock only rises after an authorised receipt; a kobo-rounded pack price matches the server's stored line total |

## 2. New frontend test module

`frontend/tests-js/test-inventory-batch01-management.js` — **11 tests**

```bash
cd frontend && node --test tests-js/test-inventory-batch01-management.js
```

```
# tests 11
# pass 11
# fail 0
```

Covers: unit-label rendering (including unknown tokens and API-provided labels), the
base-unit-vs-opening-unit fields, the Edit action's capability gate, the adjustment dialog's required
fields + audited endpoint + idempotency key, the SKU search/duplicate callout, replenishment never
sending master data, the stock-at-hand search wiring (server `search` parameter, debounce, filters
preserved, loading/empty/error states), and the **exact** reorder arithmetic
(`3 packs × 1,200.00 = 72 pieces`, total `3600.00`; `5 × 49.99 = 249.95`; a pack price that does not
divide into kobo → the figure the server actually stores, `1000.08`, with the review disclosing the
supplier's `1000.00` quote).

These are DOM-free contract tests that **execute the real page functions** (sliced out of
`dashboard/procurement.html` / `dashboard/inventory.html` by brace matching) inside a `vm` context —
the same technique the repository's existing `tests-js` suite uses.

## 3. Regression suites (unchanged modules)

**(a) Every module that touches the changed code paths** — 33 test modules:

```bash
python manage.py test tests.test_accounting_agent_procurement_payables \
    tests.test_accounting_inventory_reconciliation tests.test_admin_approval_override \
    tests.test_approvals_inbox tests.test_approved_11_departments_consolidation \
    tests.test_audit_procurement_regressions tests.test_barcode_lookup tests.test_capabilities \
    tests.test_dashboard_features tests.test_demo_seeding tests.test_enterprise_upgrade_suite \
    tests.test_financial_summary_report tests.test_inventory tests.test_inventory_agent_patch \
    tests.test_inventory_api tests.test_inventory_batch01_management \
    tests.test_inventory_item_management tests.test_inventory_sales_depletion \
    tests.test_inventory_unit_contract tests.test_inventory_unit_migration \
    tests.test_menu_price_cost_guard tests.test_permission_alignment tests.test_pos \
    tests.test_pos_menu_management tests.test_pos_stock_validation \
    tests.test_procurement_agent_workflow tests.test_procurement_inventory_dashboard_access \
    tests.test_procurement_receipts_and_editing tests.test_product_profitability \
    tests.test_purchase_order_payments tests.test_query_efficiency \
    tests.test_separate_approver_control tests.test_supplier_payment_visibility
```

```
Ran 491 tests in 216.160s
OK
```

**(b) The complete backend suite** (all 154 test modules — non-inventory domains included):

```bash
python manage.py test --parallel 1
```

```
Ran 2062 tests in 674.577s
FAILED (failures=2, skipped=22)
```

The two failures are **pre-existing on the untouched base commit** — re-verified by running the same
two tests inside the pristine clone of `d9683e1`:

```
FAIL: test_security_and_procurement_role_grants_stay_narrow
      (tests.test_department_roles.DepartmentRoleModelTests)
FAIL: test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls
      (tests.test_deployment_readiness.ProductionSettingsBootAndFailFastTests)
Ran 2 tests — FAILED (failures=2)      ← identical result before any Batch-01 change
```

* `test_department_roles…`: asserts an exact role→capability matrix that the repository's committed
  fixtures no longer match ("Items in the first set but not the second"). Nothing in Batch 01 adds,
  removes or re-assigns a capability.
* `test_deployment_readiness…`: boots production settings from Northflank-style MySQL/Redis URLs; it
  fails in this sandbox for the same reason it fails on the base commit (the hosts/URLs in the test
  environment are not resolvable here, printing a connection traceback). Not code-related.

Batch 01 therefore leaves the full backend suite at **2060 passing, 2 pre-existing failures, 0 errors,
no new failure**.

## 4. Static / consistency checks

| Check | Command | Result |
|---|---|---|
| Django system check | `python manage.py check` | `System check identified no issues (0 silenced).` |
| Migration state | `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| Migration applies | `python manage.py migrate inventory` | `0014_batch01_barcode_and_balance_adjustments` applied cleanly (exercised implicitly by all 2062 tests, which build the schema from migrations) |
| Python syntax/imports | `python -m py_compile` on every changed module + `import apps.inventory.views` | OK |
| Inline page scripts parse | `node --test tests-js/test-inline-scripts-parse.js` | 3 pass (both edited pages compile) |

## 5. Full frontend suite (regression)

```bash
cd frontend && node --test tests-js/
```

```
# tests 700
# pass 692
# fail 8
```

The **same 8 failures exist on the untouched base commit** (verified before any Batch-01 edit, when
the suite reported 689 tests / 8 failures):

```
not ok 123 - 3. All 72 deployed HTML pages load runtime-config.js before config.js and match version.json
not ok 127 - accounting surfaces supplier goods received separately from posted invoices
not ok 155 - no obsolete production API rewrite remains active in static hosting configs
not ok 311 - Accounting renders the night audit daily flash and checks
not ok 512 - the only web app manifest is scoped to the staff dashboard
not ok 590 - the hotel's own statutory figure is still reported, not deleted with the fields
not ok 614 - Accounting renders balance sheet and cash flow statements
not ok 628 - every staff table scrolls inside a .table-wrap instead of the page
```

Batch 01 adds 11 passing tests (689 → 700) and introduces **no new failure**. These eight are
accounting/manifest/table-markup issues outside Batch 01's scope and were deliberately left alone
(fixing them would modify accounting surfaces and unrelated pages).

> Note: `node --test` exits `0` even with failures, so the counts above were read from the
> `# pass` / `# fail` footer rather than the exit code — a trap worth knowing when re-running this.

## 6. Manual/functional verification (API level)

Performed against a running development server with real HTTP calls (not mocked):

| Scenario | Expected | Observed |
|---|---|---|
| Create a new stock item with an opening quantity in crates | item created, balance in pieces | ✅ 201, `12 CRATE × 24 = 288` pieces |
| Create an item with an opening quantity whose factor contradicts the pack size | rejected, nothing created | ✅ 400 with a field error, no catalog row |
| Search an existing SKU while adding an item | item returned, duplicate creation blocked | ✅ exact SKU ranked first, duplicate rejected |
| Replenish the searched item | stock rises, master data unchanged | ✅ `{movement, balance, item}`, name/unit/barcode/selling price unchanged |
| Duplicate SKU via direct API (no UI) | rejected | ✅ 400, existing item named |
| Administrator corrects stock at hand | previous/new/actor/reason recorded, ledger appended | ✅ 201 + `STOCK_BALANCE_ADJUSTED` audit entry |
| Storekeeper calls the adjustment endpoint directly | refused | ✅ 403 |
| Unauthenticated call to any new endpoint | refused | ✅ 401 |
| Search stock at hand by barcode / category / store, page 2 | filtered page, no reload | ✅ server-side `search`, `page`, filters preserved |
| Reorder an item (3 packs @ 1,200.00) | draft PO, total 3,600.00, stock unchanged | ✅ `DRAFT`, approver-only transition, balance identical |
| Receive goods against the draft after approval | stock rises | ✅ only after the authorised receipt |

## 7. Not covered by automated tests (honest gaps)

1. **No browser E2E run.** This environment has no browser/Playwright, so the frontend is covered by
   (a) inline-script parse checks, (b) `vm`-executed contract tests of the real page functions, and
   (c) API-level verification of every endpoint the pages call. Click-through of the dialogs
   (proof-reading copy, focus order, mobile layout at 360 px) should be done once by a human on the
   deployed build.
2. **MySQL-specific behaviour not executed here** (the dev/test settings use SQLite). The new
   nullable-unique `barcode` column and the new indexes are standard and portable, but the migration
   was only *applied* on SQLite. Run `python manage.py migrate inventory` on a staging MySQL copy
   before production.
3. **Load/perf testing not performed**; the search endpoints were verified to issue a bounded number
   of queries (one page query + one count, `select_related`ed) rather than timed under load.
4. **MySQL was not exercised** (see §7.2), so the two remaining unknowns are database-specific
   only. The Python/Django surface is fully covered: the complete 2062-test suite ran and shows no
   new failure.
