# DEPENDENCIES — Batch 01 (Inventory Management, Stock Control & Reordering)

This batch is deliberately confined to the **inventory** application and the **two staff dashboard
pages that own it**. Nothing in another module's logic was modified. This document records:

1. the shared files that had to be touched (all **additive**, with the reason), and
2. the modules that were inspected but intentionally left alone, with the reason and the exact
   file/line where a future authorised change would be needed if the hotel wants more.

---

## 1. Shared (cross-cutting) files touched — additive only

| File | Change | Why it is safe | Impact on other modules |
|---|---|---|---|
| `frontend/js/config.js` | one new endpoint constant `inventoryAdjustments` (+ a comment block) | It is only a URL table; no existing value changed | None — no other page reads the new key |
| `frontend/js/api.js` | new `API.inventoryReorderContext(itemId)` helper, added to the exported object | New function only; no existing function was edited | None — nothing else calls it |
| `frontend/js/operations.js` | `searchPicker()` gained an optional `onSelect(row)` callback (invoked on selection and on “Change”, with `null`) | Optional callback; when omitted behaviour is byte-for-byte the previous behaviour | None — the 40+ existing `searchPicker` call sites never pass it |
| `backend/apps/inventory/services/valuation_service.py` | projection rows add `base_unit_label` (plus one import) | Additive read-only field; every existing figure/formula is unchanged | None — the only consumer is `ProductProfitabilityRowSerializer`, updated in the same batch |

**Dependency direction check:** `inventory.services.inventory_service` does not import
`valuation_service`; `valuation_service` now imports `unit_label` from `inventory_service`
(one-way, no cycle). `serializers` imports both models and the conversion helpers; no POS, finance,
booking or kitchen module imports anything new.

**Backend dependencies:** no new Python package, no new JS library, no framework. The migration uses
only `django.db.migrations`. The frontend uses the already-loaded `JONE.dashboard.formModal`,
`JONE.ui.confirm`, `JONE.operations.searchPicker` and `window.API`.

---

## 2. Inspected but NOT modified (cross-batch restrictions respected)

| Module | What Batch 01 relies on | Why it was not changed |
|---|---|---|
| **Procurement approval logic** — `submit_purchase_order`, `approve_purchase_order`, `reject_purchase_order`, `cancel_purchase_order`, `mark_purchase_order_ordered`, `record_purchase_order_payment`, `receive_goods` (`apps/inventory/services/inventory_service.py`) | The reorder dialog creates an ordinary **DRAFT** through the existing `POST purchase-orders/` endpoint, so approval, independent-approver rules (including `ADMIN_OVERRIDE`), payments and goods receipt keep working exactly as before | Out of batch scope. Reuse, not modification |
| **Accounting** — `apps/finance/*`, `apps/accounts/*` accounting hand-off, `PurchaseOrderAccountingListView` | Untouched. The new adjustment posts a stock `ADJUSTMENT` movement only: **no journal entry, no supplier payment, no expense** | Restricted by the batch rules |
| **Bookings / stays / rooms** | Untouched | Restricted |
| **Staff approval permissions & capability catalogue** — `apps/accounts/capabilities.py`, `ROLE_CAPABILITY_CODES`, `RoleCapability` migrations | Batch 01 **reuses the existing** capabilities `inventory.manage` (read/create movements), `inventory.adjust.approve` (administrator correction — already the capability that authorises stock-count variance approval) and `procurement.manage` (reorder). **No new capability was introduced**, so no permission migration is required | Adding a new capability would touch the accounts module and every role matrix — unnecessary for this batch |
| **Kitchen / production** (`apps/pos`, kitchen tickets) | Untouched; POS stock consumption paths were only exercised by tests | Restricted |
| **POS order/depletion logic** | Untouched (`pos_stock_summaries`, `process_pos_stock_consumption` unchanged) | Not required by Batch 01 |

---

## 3. Behavioural contracts preserved (documented because they are security-relevant)

1. `POST /api/admin/inventory/items/` keeps its exact request/response contract. The only new
   optional request field is `barcode`; the only new response fields are `barcode`,
   `base_unit_label`, `pack_conversion_available`.
2. `PATCH /api/admin/inventory/items/<pk>/` now **refuses** a `base_unit` change once stock history
   exists. This is a deliberate, documented tightening of an unsafe behaviour (it would have silently
   re-interpreted every posted quantity); the error names the item, the unit and the alternative
   (post an authorised adjustment, or create a new item).
3. `POST /api/admin/inventory/issues/` still accepts a base-unit `quantity` with no `unit`; the new
   `unit`/`conversion_factor` fields are optional additions.
4. Every new endpoint is capability-gated with `HasCapability` **on the server**. The frontend
   filters are cosmetic. Verified by tests (`403` for a storekeeper on the adjustment endpoint, `403`
   for a cashier on balances/context, `401/403` unauthenticated).
5. No stock movement, balance, purchase order, payment or audit row is deleted or updated by this
   batch. `StockMovement`, `StockBalanceAdjustment`, `PurchaseOrderPayment` remain append-only.

---

## 4. Known gaps that would require an authorised change elsewhere

These are **not** implemented, by design, and are recorded here instead of being modified:

| Gap | File that would need changing | Reason it was left alone |
|---|---|---|
| Stock **returns** and **transfers** still take quantities in the item's base unit only (issues now accept a unit) | `apps/inventory/serializers.py` (`ReturnStockSerializer`, `TransferStockSerializer`), `apps/inventory/views.py`, `inventory.html` dialogs | Extending them is additive but widens the surface of two controlled movement endpoints; the batch could not test every caller. Recommend a follow-up batch item |
| Stock-count counted quantities are entered in the base unit only | `StockCountSubmitLineSerializer`, `apps/inventory/views.py` | Stock counts are an approval-controlled path (`inventory.adjust.approve`); changing its input contract should be its own reviewed change |
| No server-side "reorder level suggestion" (e.g. suggested quantity) is generated | `apps/inventory/services/*` | Task 5 asks for staff-entered reorder quantity; inventing a suggestion would be a fabricated business rule |
| Barcode is a plain text field; no EAN/UPC checksum validation and no label printing integration | `apps/inventory/models.py` / `barcode-labels.html` (POS labels) | Out of Batch 01 scope; the POS barcode system is a separate, working subsystem |
| `search` uses `icontains` (no trigram/full-text index) | `apps/inventory/views.py` | Correct for the hotel's catalogue size; an index would need a MySQL-specific migration |
| The per-row **Reorder** link navigates to `procurement.html` (a page navigation) rather than opening the dialog in place | `frontend/dashboard/inventory.html` | The inventory page is gated on `inventory.manage` while reordering needs `procurement.manage`; opening the dialog inside a page the role may not be able to use would leak a permission boundary |
