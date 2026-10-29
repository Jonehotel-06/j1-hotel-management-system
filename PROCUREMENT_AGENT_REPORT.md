# Procurement agent report: purchase-order drafts and supplier workflows

**Repository:** `Jonehotel-06/j1-hotel-management-system` at commit `db0c49f` ("guest portal room services").
**Scope:** Procurement page, purchase-order (PO) API and service, and the PO → Accounting hand-off.
**Ground rules followed:**
- The original project directory was never edited, and nothing was pushed.
- All work happened in a separate working copy.
- Every changed or new repository file is in this folder at its repository-relative path.
- No migrations, no new models, no new status values, no new payment system.
- The Accounting and Inventory dashboards were not touched.

---

## 1. Root-cause analysis

### 1.1 "Create draft" did nothing (verified root cause, reproduced in a browser)

**How it was reproduced.**
- The real `frontend/dashboard/procurement.html` was driven in headless Chromium (Playwright) against the real Django API, running the original code on an isolated SQLite database.
- Seeded data: an ACTIVE supplier, an active location, and two items:
  - `WATER-75CL`, with `units_per_pack = 24`
  - `RICE-50KG`, with no pack size

**Tracing the requested path:**

| Step | Finding |
|---|---|
| Click received / form bound | Yes. `formModal`'s submit handler runs and `guardSubmit` flashes the spinner. |
| Supplier / location / lines selected | Yes. All three pickers resolved records (for GM and Storekeeper). |
| formModal invokes the callback? | **No.** `formModal` sets `form.noValidate = true`, then gates on `form.checkValidity()`. It returned `false`, so `form.reportValidity()` ran and the handler `return`ed **before** `onSubmit`. |
| JS errors | None. |
| API endpoint / payload | Never reached: no request was sent. |
| Backend permission or serializer | Not involved. The same flow with `RICE-50KG` (no pack size) returned `201` with a `PO-…` reference and status `DRAFT`. |
| Frontend swallowing errors | Effectively yes: the browser could not display its message (next row). |

**The single invalid control** was the line's **hidden** "Pieces in one pack/bag" input:
- `type=number min="0.01" step="1"`, prefilled with the item's pack size `24` (original `procurement.html:94–95`).
- Per the HTML spec, a number input's step base is its `min`. So the only valid values were 0.01, 1.01, 2.01, …
- `24` is therefore a `stepMismatch`. Chromium's message: *"Please enter a valid value. The two nearest valid values are 23.01 and 24.01."*
- The control's wrapper is `hidden` in the default "Pieces" mode, so `reportValidity()` could not show the bubble.
- Result: no message, no request, no error. The button looked dead.

**Scope of the defect:**
- Any line whose item has a whole-number pack size.
- Both **Create draft** and **Edit → Save changes**, because `initialLinesFor` prefills the same value.
- In "Pack" mode the control is visible, and staff got the confusing "23.01 / 24.01" message instead.
- It was introduced in commit `587526b` ("procurment, dashboard, b2 settled", Oct 17), which added quantity types.

**Fix (procurement-local; the form was not replaced):**
- `step:"any"` on that control.
- The control defaults only from a real (>0) pack size.
- A capture-phase `invalid` listener on the PO lines now reveals any invalid line control (even a hidden one) and names it under the Lines field ("Pieces in one pack/bag for WATER-75CL …: …"). A hidden control can no longer fail silently.

### 1.2 Procurement Officer could not raise any draft
- `PROCUREMENT_OFFICER` holds only `procurement.manage`. This was checked by resolving capabilities on a freshly migrated database.
- The PO pickers searched `/api/admin/inventory/locations/` and `/items/`, which require `inventory.manage`.
- Result: 403 *"You do not have the required permission for this action."* No location or item could be picked by the page's primary role (reproduced in the browser).
- **Fix:** two read-only, procurement-scoped lookups (§3).
  - The inventory catalogs were **not** widened, because they also create records.
  - Granting `inventory.manage` would also have allowed stock movements.

### 1.3 Duplicate drafts on retry
- `newOrder()` minted a new idempotency key **inside** `onSubmit`, so every retry sent a new key.
- Reproduced by letting the first POST reach the server, dropping its reply, then clicking **Create draft** again:
  - two different keys were sent
  - **two drafts** were created for one intent
- On the server side:
  - a reused key returned the stored order even if the payload differed (a silent mismatch)
  - the supplier/location checks ran before the replay lookup
- **Fix:**
  - One key per dialog (the pattern the goods-receipt dialog already used).
  - The server replays only an identical request: an identical payload returns `200 "Existing purchase order returned."`; a different payload returns `400` naming the existing PO.
  - The replay lookup now runs first.
  - Re-tested: same key twice, **+1 order**, and the toast reads "already saved as PO-… No duplicate was created."

### 1.4 Other verified defects found during the audit

| # | Defect | Evidence |
|---|---|---|
| a | **Supplier payment dialog always failed** with `formData is not defined`. `onSubmit` was declared `async function(values)` but used `formData`. No payment could be recorded from the UI. | Browser: error shown, no `/pay/` request. |
| b | Payment **amount prefill was empty**. `formModal` prefills from `opts.values` when given and then ignores each field's own `value`. | Browser: `amount = ''`. |
| c | A negative amount passed the client check. `!(x)>0` is a precedence bug. | Code plus JS test. |
| d | A server per-line error rendered as **"[object Object]"**. DRF returns `{"lines":{"0":{"quantity_ordered":[…]}}}`. | Browser. |
| e | **No rejection path** for submitted orders. `CANCELLED` existed but was unreachable, so duplicate drafts could never be withdrawn. | Code. |
| f | Submit and mark-ordered transitions left **no audit entry**. | Code and tests. |
| g | **N+1 queries** on the PO list: totals were computed per row with no prefetch. | Query-budget test fails on the original code. |
| h | An unknown supplier or location returned `404`, and an inactive one returned a non-field `400`. Neither highlighted the form field. | Tests. |
| i | Line errors did not name the item ("One or more stock items are inactive or missing."). | Tests. |
| j | `JONE.esc()` was used in text-only contexts (labels, help, errors are rendered via `textContent`). | Code. |

---

## 2. File manifest (exact)

**Modified repository files:**

| File | Change |
|---|---|
| `frontend/dashboard/procurement.html` | Root-cause fix:<br>• `piecesPerPackField` (line 199)<br>• hidden-invalid safety net `watchLineValidity` (line 412)<br>Create flow:<br>• one key per dialog `newOrder` (line 480)<br>• verified success `confirmedOrder` (line 98)<br>• actionable errors `explainOrderError` (line 132)<br>• "Creating draft…" label<br>• list refresh + highlight `showOrderInList` (line 472)<br>Totals:<br>• live order-total preview with server-exact decimals (`orderTotalText` line 165, `orderTotalField` line 441)<br>• pack-rounding disclosure<br>Lifecycle:<br>• next-step / receipt / payment-progress display (`nextStep` line 276)<br>• Reject and Cancel actions (lines 582 and 594)<br>Fixes in existing dialogs:<br>• `editOrder` (line 621)<br>• `recordPayment` (line 710; `formData` at line 739)<br>Lookups:<br>• pickers use the procurement lookups, with fallback (`resourceOr` line 83) |
| `frontend/js/config.js` | Lines 343–349, additive endpoint keys:<br>• `procurementLocations`<br>• `procurementItems`<br>• `purchaseOrderAccounting` |
| `backend/apps/inventory/services/inventory_service.py` | • `_validate_po_lines` names the SKU (line 219)<br>• replay fingerprint and check (lines 259 and 278)<br>• `create_purchase_order` checks the replay first and keys errors to fields (line 306)<br>• cancelled-edit message (line 392)<br>• `reject_purchase_order` (line 619)<br>• `cancel_purchase_order` (line 639) |
| `backend/apps/inventory/serializers.py` | • lookup serializers (lines 58 and 67)<br>• derived `receipt_progress` / `payment_progress` (lines 452 and 461)<br>• decision serializer (line 479)<br>• goods-receipt summary and Accounting hand-off serializers (lines 485 and 501) |
| `backend/apps/inventory/views.py` | • field-error resolvers (lines 67 and 75)<br>• `ACCOUNTING_VISIBLE_STATUSES` (line 84)<br>• lookups (lines 551 and 571)<br>• list prefetch (line 593)<br>• audited submit/ordered<br>• reject and cancel views (lines 663 and 680)<br>• Accounting hand-off (lines 826–868) |
| `backend/apps/inventory/urls.py` | Lines 32–42: six new routes. |
| `backend/docs/API.md` | Procurement rows for the new endpoints and the replay rule. |

**New repository files:**

| File | Purpose |
|---|---|
| `backend/tests/test_procurement_agent_workflow.py` | 43 API/service tests (§4). |
| `frontend/tests-js/test-procurement-draft-create-ui.js` | 16 Node tests that **execute** the shipped page functions (§4). |

**Documentation in this folder (not repository files):**
- `PROCUREMENT_AGENT_REPORT.md` (this report)
- `procurement_agent.patch`: a unified diff of all of the above against `db0c49f`

**Deliberately not modified:**
- `frontend/js/dashboard.js` (`formModal`), `operations.js` (`linePicker`) and `api.js`: shared by every dashboard and by the Inventory BOM/recipe pages.
- `accounting.html`, `inventory.html`, `approvals.html`.
- The `finance` app.
- All models and migrations.

---

## 3. Database and API changes, with justification

**Database:** none. `python manage.py makemigrations --check --dry-run` reports `No changes detected`.
- No model, field, status value or migration was added.
- `receipt_progress` and `payment_progress` are computed per response from existing lines and payments. They are never stored and never used for control decisions. `status` remains the one workflow field.

**New endpoints** (all under `/api/admin/inventory/`):

| Endpoint | Gate | Behaviour | Why |
|---|---|---|---|
| `GET procurement/locations/` | `procurement.manage` | Active locations only: `id, code, name, is_active`. | The officer needs to pick a delivery location (§1.2) without being able to create locations or move stock. |
| `GET procurement/items/` | `procurement.manage` | Active items with purchase-side fields: SKU, name, unit, `units_per_pack`, `pack_price`, `standard_cost`, preferred supplier. **No** POS or selling-price fields. | Same as above. It also keeps purchase cost separate from selling price. |
| `POST purchase-orders/<ref>/reject/` | `procurement.approve` | Takes a `reason` (required). Moves SUBMITTED to **DRAFT**. The reason is appended to the notes and audited as `PURCHASE_ORDER_REJECTED`. | "Approval or rejection by the authorised role" had no rejection path. Returning to DRAFT reuses existing statuses; the requester corrects and resubmits, or cancels. |
| `POST purchase-orders/<ref>/cancel/` | `procurement.manage` | Takes a `reason` (required). Moves DRAFT or SUBMITTED to the existing **CANCELLED** status. Never deletes. A replay is a no-op. APPROVED or later is refused. | Withdraws unwanted or duplicate drafts (including those created by §1.3) without deleting history. |
| `GET accounting/purchase-orders/`, `…/<ref>/` | Any of `reports.financial.view`, `expense.approve`, `procurement.approve` | Read-only. Statuses other than DRAFT only. Returns lines, totals, paid/outstanding, progress, goods receipts with `received_value`, payments and evidence URLs. Filters: `status`, `supplier_id`, `search`. | The minimum integration needed so Accounting can see submitted orders (§5). `expense.manage` alone is *not* accepted, because the current matrix also gives it to cashiers and waiters. |

**Changed behaviour (backward compatible for well-formed clients):**

`POST purchase-orders/`:
- An unknown supplier or location returns `400 {errors:{supplier_id|delivery_location_id}}`; it was a `404`.
- An inactive supplier or location is keyed to its field.
- The idempotency replay is evaluated first and must match the payload (§1.3).

`PATCH purchase-orders/<ref>/`:
- The same field errors apply.
- A cancelled order is refused with an explicit message.

List and detail:
- Responses add `receipt_progress` and `payment_progress`.
- The list uses a constant number of queries.
- The detail prefetches `lines__item` and `payments__evidence_uploaded_by`.

Audit: new `PURCHASE_ORDER_SUBMITTED`, `_ORDERED`, `_REJECTED` and `_CANCELLED` entries, written only when the status actually changes.

---

## 4. Test commands and actual results

**Environment:**
- Python 3.13 venv from `backend/requirements.txt`. `mysqlclient` was skipped; the default SQLite database was used.
- Node 20.
- Chromium (Playwright) for the browser checks.
- Commands were run from `backend/` or `frontend/` of the working copy.

### Baseline (original code, before any change)

| Command | Result |
|---|---|
| `python manage.py test tests.test_purchase_order_payments tests.test_procurement_receipts_and_editing tests.test_audit_procurement_regressions tests.test_supplier_payment_visibility` | 53 tests, OK |
| `python manage.py test tests.test_inventory_api tests.test_inventory tests.test_admin_approval_override tests.test_separate_approver_control tests.test_folio_balance_classification tests.test_inventory_item_management tests.test_inventory_sales_depletion tests.test_product_profitability tests.test_approvals_inbox tests.test_role_permissions_matrix` | 122 tests, OK |
| `python manage.py test tests.test_query_efficiency tests.test_permission_alignment` | 32 tests, OK |
| `node --test tests-js/` | 665 tests: 657 pass, **8 fail (pre-existing, not procurement)** |

The 8 pre-existing JS failures:
- runtime-config/version.json page check
- Accounting balance sheet / cash flow
- Accounting night audit
- Accounting goods-received display
- `.table-wrap` in `accounting.html`
- obsolete API rewrite
- statutory figure
- PWA manifest scope

### After the changes

| Command | Result |
|---|---|
| `python manage.py test tests.test_procurement_agent_workflow` | **43 tests, OK** |
| Same new file run against the *original* code | **34 of 43 fail** (25 failures, 9 errors). The 9 that pass guard existing behaviour: maker-checker, required fields, invalid quantities, unchanged inventory permissions. |
| `python manage.py test tests.test_purchase_order_payments tests.test_procurement_receipts_and_editing tests.test_audit_procurement_regressions tests.test_supplier_payment_visibility tests.test_inventory_api tests.test_admin_approval_override tests.test_separate_approver_control tests.test_approvals_inbox` | 84 tests, OK. All 207 baseline tests also pass inside the full run below. |
| `python manage.py test tests` (entire backend suite, 144 files) | `Ran 1990 tests in 722.417s`, `FAILED (failures=1, skipped=22)`. The single failure is environmental and pre-existing (details below). |
| `python manage.py check` / `makemigrations --check --dry-run` | No issues / No changes detected |
| `node --test tests-js/test-procurement-draft-create-ui.js` | **16 tests, 16 pass**. Against the original page: 15 of 16 fail. Test 1 deliberately documents the original constraint and passes on both. |
| `node --test tests-js/` | **681 tests: 673 pass, 8 fail.** The failure set is byte-identical to the baseline. |
| Procurement-related JS files (`test-procurement-*`, `test-audit-procurement-receipts`, `test-supplier-payment-receipt-ui`, `test-inline-scripts-parse`, `test-inventory-ui`) | 55 tests, 55 pass |

The one full-suite failure is `tests.test_deployment_readiness.ProductionSettingsBootAndFailFastTests.test_production_settings_boot_cleanly_with_northflank_mysqls_and_rediss_urls`.
- Cause: `ModuleNotFoundError: No module named 'MySQLdb'`. `mysqlclient` needs system MySQL libraries and was not installed in this sandbox.
- It fails identically on the original code. It is unrelated to procurement.

**Coverage of the requested test areas:**

| Area | Tests |
|---|---|
| Draft creation | Backend: `ProcurementLookupTests`. Browser: E2E. |
| Required-field validation | Backend: `DraftValidationTests`. |
| API failure handling | JS: actionable errors and verified success. Browser: 500, proxy HTML 200, lost reply. |
| Duplicate submissions | Backend: `DuplicateSubmissionTests`. JS: key reuse test. |
| Draft editing | Backend: `DraftEditingTests`. Browser: E2E. |
| Submission and approval permissions | Backend: `ApprovalAndRejectionTests`. |
| Cost calculations | Backend: `CostAndUnitTests`. JS: `orderTotalText`. |
| Unit conversions | JS: `linesToBaseUnits` and pack hint. Backend: pack contract. |
| Receipt and payable linkage, partial receipts and payments | Backend: `ReceiptAndPayableLinkageTests`. |
| Accounting hand-off | Backend: `AccountingHandoffTests`. |
| Historical preservation | Backend: snapshot test, receipt-price test, cancel keeps the record. |
| Query budget | Backend: `ListQueryBudgetTests`. |

### Browser end-to-end checks (real page, real API, Chromium)

**Harness.** It is kept outside this folder because it is not a repository file. It lives in the workspace at `/home/user/work/repro/`:
- `seed.py`: isolated SQLite database, users per role, freshly minted JWTs
- `e2e_procurement.py`: the scenarios
- `before_original_code.txt` and `after_patched_code.txt`: the raw outputs

To run it:
1. `DB_NAME=…/repro.sqlite3 python manage.py migrate && python manage.py shell < seed.py`, writing the printed `CREDS=` JSON to `creds.json`.
2. `python manage.py runserver 127.0.0.1:8000`.
3. `python3 -m http.server 5500` in `frontend/`.
4. `python e2e_procurement.py <scenario…>`.

| Scenario | Original code | Patched code |
|---|---|---|
| GM: item with pack size 24 → Create draft | No request, modal stays open, no message, no JS error; hidden `pieces_per_pack` is invalid | `201`, `PO-… DRAFT`, toast "Draft PO-… created. Next: open it and submit it for approval.", row appears at top with "Next: submit for approval" |
| Storekeeper: same | Same dead button | `201 DRAFT` |
| Edit draft containing a pack item → Save changes | Dead button | `PATCH 200` |
| Procurement Officer | Location and item searches `403` | Searches work, `201 DRAFT` |
| Reply lost, then retry | 2 different keys, **+2 orders** | Same key twice, **+1 order**, "already saved as PO-… No duplicate was created." |
| Server per-line error | "[object Object]" | "Line 1 (RICE-50KG) — Quantity (in pieces): Ensure that there are no more than 10 digits before the decimal point." |
| HTTP 500 | n/a | Error and retry hint; modal stays open; no success |
| HTTP 200 HTML page from a proxy | (would show success) | "The server did not confirm this purchase order…"; no success |
| Invalid value forced into the hidden control | (silent) | Control revealed and named; no request |
| 2 packs × ₦1,000 (24 per pack) | n/a | Hint: "Recorded line total ₦2,000.16 (quoted ₦2,000.00)…". Preview ₦2,000.16 equals the server total of 2000.16. |
| Record supplier payment | `formData is not defined`, amount empty | Amount prefilled 130000, `/pay/` `200` |
| Reject, then Cancel (GM on a storekeeper's order) | No such actions | DRAFT with reason note, then CANCELLED with reason note |
| Accounting hand-off (HTTP) | Accountant cannot see POs | Accountant `200` (drafts excluded); accountant on `/purchase-orders/` `403`; officer on the hand-off `403` |

---

## 5. Dependencies on the Accounting and Inventory agents

### What exists today (traced, not assumed)

**There is no PO ↔ payable relationship.**
- Accounting's supplier payables are `finance.Expense` rows: `accounting_class=INVENTORY`, `payment_method=PAYABLE`.
- Their workflow is create → submit → independent approval (`ApprovalRequest`) → post (Dr Inventory / Cr Accounts payable) → `settle-payment` (Dr AP / Cr tender).
- `Expense` has no PO field, and `PurchaseOrder` has no expense field.

**Procurement's own `PurchaseOrderPayment` is a separate path.**
- It records "Record supplier payment" by `procurement.approve` holders and moves the order to PAID.
- It **does not post to the ledger**. This is documented as a deliberate boundary in `docs/DELIVERY_REPORT_2026-10-17.md`.
- The payment dialog now says this plainly.

### What Procurement now provides
- The read-only hand-off endpoint (§3).
- Derived receipt and payment progress.
- The goods-received value per receipt.
- A config key, `purchaseOrderAccounting`.

### Accounting agent (needed to complete the flow; not implemented here by instruction)
- **A1. Hand-off queue in Accounting.** List `purchaseOrderAccounting` (SUBMITTED onwards) in `accounting.html` so the accountant can inspect and verify an order.
- **A2. Link payable ↔ PO (minimum integration).** When raising the Expense (INVENTORY / PAYABLE) for an order, carry the PO reference. Two options:
  - store it in `Expense.metadata["purchase_order_reference"]` (no migration), or
  - add a nullable FK `Expense.purchase_order` (with a migration).
  
  The link should be validated as follows:
  - the PO exists and is APPROVED or later (never DRAFT, SUBMITTED or CANCELLED);
  - the sum of non-rejected, non-voided expenses for the PO is ≤ the PO total (or ≤ `received_value`, if the hotel pays only for goods received).
  
  This is what prevents duplicate payables. Today nothing stops two expenses for the same order.
- **A3. Choose one system of record for supplier payments.** The candidates are procurement's `PurchaseOrderPayment` (no ledger) and Accounting's `settle-payment` (ledger).
  - The ledger never double-counts, because `PurchaseOrderPayment` never posts, but the two records can disagree.
  - Recommendation: after A2, retire the procurement "Record supplier payment" action, or make it reference the settled expense.
- **A4. Partial payments.** Expense settlement is all-or-nothing per expense. Paying a PO in instalments through Accounting needs one expense per invoice or instalment.

### Inventory agent
- **I1.** The PO pickers now use the procurement lookups. `/locations/` and `/items/` are unchanged (still `inventory.manage`). If `StockItem` or `StockLocation` fields are renamed, update `ProcurementItemLookupSerializer` / `ProcurementLocationLookupSerializer`.
- **I2.** In `operations.js`, `linePicker` hides controls via `when`, but hidden controls still take part in native validation. Suggest also setting `control.disabled = wrap.hidden` in `refreshHint`. It is shared with the BOM/recipe pages, so it is left to the owner; procurement no longer depends on it.
- **I3.** Cost precision: PO line, goods-receipt and stock-movement `unit_cost` are stored to 2 decimals per base unit. A pack price that does not divide evenly drifts: ₦2,000.16 instead of ₦2,000.00. The page now discloses this. An exact fix needs 4-decimal unit costs across those three models (a schema change) and is an Inventory decision.

### Shared frontend (owner of `dashboard.js`)
- `formModal` prints nested backend errors as "[object Object]".
- It never clears `errBox.textContent` between attempts, so the "Please review the highlighted fields." fallback can be skipped.
- Procurement now normalises its own errors; a shared fix would help every dashboard.

### Release
- Ship with the normal version bump (`frontend/build.py --bump`) so `config.js` and the service-worker cache refresh.
- If a stale cached `config.js` lacks the new keys, `procurement.html` falls back to the old inventory directories. Those still serve GM/Storekeeper/Admin; the Officer needs the refreshed config.

---

## 6. Simple manual fixes (exact lines in the original files at `db0c49f`)

If the full patch cannot be taken, these one-line edits fix the most visible defects:

1. **`frontend/dashboard/procurement.html:94`**: change `step:"1"` to `step:"any"`.
   - This alone fixes "Create draft" and "Save changes" for GM, Storekeeper and Admin.
2. **`procurement.html:95`**: use the pack size as the default only when it is > 0, e.g. `return Number(record&&record.units_per_pack)>0?JONE.trimQuantityValue(String(record.units_per_pack)):"";`.
3. **`procurement.html:272–277`**: make one idempotency key per dialog.
   - Add `var draftKey=OPS.newIdempotencyKey("purchase-order");` before line 273.
   - Change line 277 to `values.idempotency_key=draftKey;`.
4. **`procurement.html:461`**: change `onSubmit:async function(values){` (in `recordPayment`) to `onSubmit:async function(values,formData){`.
5. **`procurement.html:460`**: change to `values:{paid_on:new Date().toISOString().slice(0,10),amount:String(outstanding||"")},`.
6. **`procurement.html:464`**: change `if(!(Number(values.amount)||0)>0)` to `if(!((Number(values.amount)||0)>0))`.
7. **`backend/apps/inventory/views.py:530`**: add `.prefetch_related("lines", "payments")` to the list queryset (fixes the N+1).

The Procurement Officer fix (§1.2) needs the new lookup endpoints; it is not a one-liner.

---

## 7. Applying the patch and rolling it back

**Apply** (from the repository root, at `db0c49f` or a descendant without conflicting edits to these files):

```bash
git apply --check ai_updates/procurement_agent/procurement_agent.patch
git apply ai_updates/procurement_agent/procurement_agent.patch
# or copy the files:
cp -r ai_updates/procurement_agent/backend ai_updates/procurement_agent/frontend .
```

Then:
1. No migration is required. Run `python manage.py check`.
2. Restart the API.
3. Release the frontend with the usual version bump.
4. Run the tests:
   ```bash
   (cd backend && python manage.py test tests.test_procurement_agent_workflow tests.test_purchase_order_payments tests.test_procurement_receipts_and_editing tests.test_audit_procurement_regressions)
   (cd frontend && node --test tests-js/)
   ```

**Verified:**
- The patch applies cleanly to a fresh clone of `db0c49f`.
- The result is byte-identical to the files in this folder.

**Rollback:**

```bash
git apply -R ai_updates/procurement_agent/procurement_agent.patch
# or:
git checkout db0c49f -- backend/apps/inventory/services/inventory_service.py backend/apps/inventory/serializers.py \
  backend/apps/inventory/views.py backend/apps/inventory/urls.py backend/docs/API.md \
  frontend/dashboard/procurement.html frontend/js/config.js
rm backend/tests/test_procurement_agent_workflow.py frontend/tests-js/test-procurement-draft-create-ui.js
```

- Verified: the reverse patch leaves the tree identical to `db0c49f`.
- There is no data to migrate back. Records written through the new endpoints stay valid under the old code:
  - CANCELLED status (it already existed)
  - appended note lines
  - audit entries

---

## 8. Remaining unverified issues and risks

1. **Existing production data.**
   - Duplicate drafts created by the old retry bug may already exist. They can now be withdrawn with **Cancel order**.
   - I had no production access and changed no data.
   - `REMAINING_BLOCKERS.md` lists one submitted PO in production.
2. **Database engine.** Tested on SQLite only, not MySQL 8 (production). No engine-specific query was added: there are no JSON lookups and all arithmetic is Decimal.
3. **Browsers.** Verified in headless Chromium only.
   - The total preview uses `BigInt` (Safari 14+). Without it the preview is simply not shown; nothing else depends on it.
4. **The Accounting flow (A1–A4) is not complete end to end.** The steps "accounting verifies → payment request → approval → payment against the payable" exist only in the Accounting module, with no link to the PO until A2 is built.
5. **Editing a SUBMITTED order** is still allowed. This is existing, tested behaviour ("corrected before the approver saw it").
   - An approver could approve a version that changed after they opened it.
   - Recommendation: optimistic concurrency on approve, e.g. require the `updated_at` the approver saw.
6. **No cancellation after approval.** An approved order whose supplier never delivers stays APPROVED or ORDERED. It needs a controlled-correction design with approver rights and no receipts or payments.
7. **Payment evidence lives in two places.** `payment_reference` / `approval_receipt` captured at approval, and per-payment evidence on `PurchaseOrderPayment`. This is existing behaviour and unchanged.
8. **Retired roles.** MANAGER and INVENTORY_CLERK hold procurement capabilities but are not in the page's boot list (commit `602ced8`). They are retired titles; this is unchanged.
9. **The approvals inbox** still lists SUBMITTED POs. A rejected order (now DRAFT) leaves the inbox. This was verified by code reading only.
10. **The 8 pre-existing frontend test failures** are outside procurement and were left untouched.
