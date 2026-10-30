# CHANGES — Batch 01 (Inventory Management, Stock Control & Reordering)

Every entry below names the file, what changed, and why. Only Batch-01 functionality is touched;
see `DEPENDENCIES.md` for the two shared files that were extended additively and for the modules
this batch deliberately did **not** modify.

---

## A. Backend — database

### NEW `backend/apps/inventory/migrations/0014_batch01_barcode_and_balance_adjustments.py`

* Adds `StockItem.barcode` — `CharField(max_length=64, null=True, blank=True, unique=True,
  db_index=True)`. `NULL` means “no barcode assigned”; because `NULL`s are distinct in every
  supported backend, any number of items may leave it unset. It exists so stock at hand can be
  searched by the number printed on the carton (Task 4) without letting two items claim the same
  code.
* Creates `StockBalanceAdjustment` (append-only audit trail of administrator corrections, Task 2).
* **No data migration, no backfill, no recalculation.** Every existing row and ledger entry is
  untouched; the new column is nullable, so the migration is safe on a live database.

### MODIFIED `backend/apps/inventory/models.py`

* `StockItem.barcode` field + `StockItem.save()` normalising a blank barcode to `NULL` (an empty
  string would collide with the unique index and make the second item unsaveable).
* New `StockBalanceAdjustment` model: `balance`, `item`, `location`, `previous_quantity`,
  `new_quantity`, `quantity_delta`, `base_unit`, `reason`, `notes`, `movement` (one-to-one with the
  immutable `StockMovement`), `actor`, `created_at`; append-only (`save()` refuses updates,
  `delete()` refuses deletes), indexed by `(item, location, created_at)` and `(actor, created_at)`.

---

## B. Backend — services

### MODIFIED `backend/apps/inventory/services/inventory_service.py`

1. **`UNIT_LABELS`, `unit_label()`, `is_container_unit()`, `MAX_LEDGER_QUANTITY`** — one place that
   turns a ledger token (`KG`, `PIECE`, `CRATE`, `GALLON` …) into a human label
   (`Kilograms (kg)`, `Pieces`, `Crates`, `Gallons`). Unknown tokens are shown upper-cased, never
   blanked.
2. **`resolve_entered_quantity()` (new)** — the single conversion contract used by opening stock,
   replenishment and issues. It returns the exact base-unit quantity plus the evidence
   (`entered_quantity`, `entered_unit`, `base_unit`, `conversion_factor`, `converted_quantity`) that
   is stored on the movement. It refuses:
   * a factor when the quantity is already in the base unit;
   * cross-family conversions (kg ↔ litre, kg ↔ piece);
   * a factor that contradicts an exact same-family ratio (e.g. `2 g` with factor `500`);
   * a factor that contradicts the item's declared `units_per_pack` (24-piece crate with `12`);
   * a container unit with no declared size and no explicit factor (unless the caller permits the
     item's own declared pack size);
   * a “shot” conversion without an explicit factor (its size is never assumed);
   * quantities ≤ 0 or ≥ 10¹⁰ (i.e. that cannot fit `numeric(14,4)`).
3. **`replenish_stock_item()` (new)** — adds stock to an existing item as an ordinary, idempotent
   `RECEIPT` movement. Master data is structurally absent from the signature; inactive
   items/locations are refused; the converted quantity and unit evidence are stored on the movement.
4. **`adjust_stock_balance()` (new)** — administrator correction: locks the balance, resolves the
   idempotency key **before** the “nothing changed” rule (so a retry returns the correction it
   already made), posts an immutable `ADJUSTMENT` movement for the delta, then writes the
   `StockBalanceAdjustment` row in the same transaction. Refuses a negative or unchanged target,
   refuses a missing/short reason, and never rewrites a historical movement.
5. Docstring/behaviour note: `convert_stock_quantity()` is unchanged in contract and still used by
   recipes and every legacy caller.

### MODIFIED `backend/apps/inventory/services/valuation_service.py`

* The profitability projection row now carries `base_unit_label` (additive; the stored token and
  every figure are unchanged). One import added.

---

## C. Backend — serializers

### MODIFIED `backend/apps/inventory/serializers.py`

* **`StockItemSerializer`**
  * new writable `barcode` field; new read-only `base_unit_label`, `pack_conversion_available`.
  * `validate_base_unit()` — required, ≤ 30 chars, upper-cased (unchanged behaviour, clearer error).
  * `validate_sku()` (**new**) — trimmed, case-insensitive duplicate check naming the existing item
    (“SKU X already belongs to Y. … replenish the existing item instead of creating a duplicate.”).
  * `validate_barcode()` (**new**) — trimmed, blank → `NULL`, case-insensitive duplicate check.
  * `_validate_unit_history()` (**new**) — refuses a `base_unit` change when the item already has
    movements or corrections, because every posted quantity was written in the old unit.
* **`StockBalanceSerializer`** — adds `item_category`, `item_barcode`, `base_unit_label`,
  `units_per_pack`, `location_name` (all read-only, additive).
* **`StockMovementSerializer`**, **`PurchaseOrderLineSerializer`** — add `base_unit_label`.
* **`ProcurementItemLookupSerializer`** — adds `barcode`, `base_unit_label`, `reorder_level`
  (read-only).
* **`ProductProfitabilityRowSerializer`** — adds `base_unit_label`.
* **`IssueStockSerializer`** — new optional `unit` + `conversion_factor` (blank `unit` keeps the
  existing base-unit contract).
* **New input serializers**: `StockBalanceAdjustSerializer` (quantity ≥ 0, reason ≥ 5 chars, notes,
  idempotency key), `StockReplenishSerializer` (location, quantity, unit, factor, unit cost,
  supplier reference, notes, idempotency key — deliberately no master-data fields).
* **New read serializers**: `StockBalanceAdjustmentSerializer`, `ReorderBalanceSerializer`,
  `ReorderContextSerializer`.

---

## D. Backend — API views and routes

### MODIFIED `backend/apps/inventory/views.py`

| View | Change |
|---|---|
| `StockItemListCreateView.get` | `search` now also matches `category` and an exact `barcode`; exact SKU matches are ordered first; new whitelisted `ordering`. |
| `StockItemListCreateView.post` | Item + opening movement are now **one transaction** (a rejected opening quantity used to leave a half-created catalog row); opening fields are validated up front with field-level errors; uses the shared conversion contract. |
| `_opening_in_base_units` | Rewritten on top of `resolve_entered_quantity()` (same public behaviour, consistent messages, contradiction checks). |
| `StockBalanceListView.get` | New `search` (SKU, name, category, barcode, location code, location name), `category`, `active`, whitelisted `ordering`; `select_related` kept so a page is one query + count. |
| **`StockBalanceAdjustView` (new)** | `POST balances/<pk>/adjust/`, capability `inventory.adjust.approve`; posts the correction, writes the audit log (`STOCK_BALANCE_ADJUSTED`) and returns the adjustment + refreshed balance. |
| **`StockBalanceAdjustmentListView` (new)** | `GET adjustments/`, capability `inventory.manage`, filterable by `item_id`/`location_id`/`balance_id`. |
| **`StockItemReplenishView` (new)** | `POST items/<pk>/replenish/`, capability `inventory.manage`; records the receipt, logs `STOCK_REPLENISHED`, returns movement + balance + item. |
| `StockIssueView.post` | Optional `unit`/`conversion_factor` → converted exactly once with the item's declared pack size; a factor without a unit is a field error; omitting both keeps the old contract. |
| **`ProcurementItemReorderContextView` (new)** | `GET procurement/items/<pk>/reorder-context/`, capability `procurement.manage`; three bounded reads (item, balances, last receipt line + open-order outstanding). |

### MODIFIED `backend/apps/inventory/urls.py`

```python
path("balances/<int:pk>/adjust/", views.StockBalanceAdjustView.as_view(), name="balance-adjust")
path("adjustments/", views.StockBalanceAdjustmentListView.as_view(), name="adjustments")
path("items/<int:pk>/replenish/", views.StockItemReplenishView.as_view(), name="item-replenish")
path("procurement/items/<int:pk>/reorder-context/", views.ProcurementItemReorderContextView.as_view(),
     name="procurement-item-reorder-context")
```

---

## E. Frontend

### MODIFIED `frontend/dashboard/inventory.html`

* **Stock-at-hand toolbar**: new `Replenish stock` button (Task 3) next to `Add item`.
* **Filter bar**: new `Search stock at hand` box (SKU / name / barcode / category / store) with a
  hint, plus a `Clear` button. Typing re-renders only the balances table (debounced 320 ms, Enter
  submits immediately) — no page reload, filters preserved.
* **Balances table**: rows now show the labelled unit (`12 Pieces`, not `12 PIECE`) and a category
  suffix; new actions column with **Edit** (only when `inventory.adjust.approve` is held) and
  **Reorder** (only when `procurement.manage` is held, deep-links to
  `procurement.html?reorder=<item id>`). Loading/empty/error states updated, pagination kept, and a
  `balanceCache` prevents an extra request when an action is used.
* **New: `adjustBalance()`** — administrator correction modal (read-only context, corrected
  quantity, required reason, notes) → `API.resourceAction("inventoryBalances", id, "adjust", …)`
  with one idempotency key per dialog; refreshes the table in place.
* **New: `replenishStock()`** — replenishment modal: search an existing item, see its category, unit
  and current balance (fetched from the server), then enter quantity, unit, optional factor, cost,
  supplier reference, notes. Posts only those fields to `items/<id>/replenish/`.
* **New: `skuSearchField()`** — the Add-stock-item SKU input is now a search field over existing
  SKUs with a blocking duplicate callout and a “Replenish this item instead” action.
* **Add / edit item dialogs**: new optional `Barcode` field; clearer help text separating the counted
  base unit from the opening quantity unit.
* **Issue dialog**: new `Quantity unit` + conversion-factor fields so a crate can be issued as a
  crate (converted exactly once, server-side).
* **Unit labels**: `unitLabel()`, `balanceUnitLabel()`, `qtyLabel()`, `signedQtyLabel()` used by the
  balances table, the catalog table and the movement ledger (falling back to the API's
  `base_unit_label`).
* New CSS for the search field, the adjustment/replenishment context cards and the duplicate-SKU
  callout (inline in the page's existing `<style>` block, responsive, no new asset file).

### MODIFIED `frontend/dashboard/procurement.html`

* New `Reorder stock item` button in the purchase-orders panel.
* **New reorder dialog** — `reorderItem(itemId)`, `pickReorderItem()`, `reorderContext()`:
  selects an item (or arrives preselected via `?reorder=<id>`), fetches
  `procurement/items/<id>/reorder-context/`, shows identity/unit/balance/last-purchase-cost
  read-only, and asks only for quantity, purchase unit, unit purchase price, supplier, delivery
  location, expected date and notes.
* **New pure helpers** (unit-testable, no DOM): `reorderPurchaseUnits`, `reorderUnitsPerPurchaseUnit`,
  `reorderBaseQuantity`, `reorderBaseUnitCost`, `reorderLinePayload`, `reorderQuotedTotalText`,
  `reorderTotalText`, `reorderReviewText`, `reorderLocationInitial`, `reorderUnitLabel`.
* Live order-total preview, review-before-submit confirmation (Back to edit keeps the entries),
  idempotent draft creation through the existing `purchaseOrders` endpoint, and a disclosure when the
  supplier's pack quote and the stored per-base-unit cost differ by a kobo rounding.
* `supplierPicker()` / `locationPicker()` accept an optional `onSelect` so the review can name the
  chosen supplier and delivery store (additive; existing callers unchanged).

### MODIFIED `frontend/js/config.js`

* New endpoint constant `inventoryAdjustments` (`/api/admin/inventory/adjustments/`). The other new
  endpoints are per-id actions and therefore use the existing resource + `API.resourceAction`
  mechanism — no hardcoded URLs anywhere.

### MODIFIED `frontend/js/api.js`

* New `API.inventoryReorderContext(itemId)` helper (GET `procurement/items/<id>/reorder-context/`,
  with a fallback to the inventory item path if a stale cached config lacks the procurement lookup).

### MODIFIED `frontend/js/operations.js`

* `searchPicker()` accepts an optional **`onSelect(row)`** callback (called on selection and on
  “Change” with `null`). Purely additive: every existing picker call site behaves exactly as before.

---

## F. Tests added

* **NEW `backend/tests/test_inventory_batch01_management.py`** — 38 tests in 6 classes:
  `StockUnitFieldTests`, `CrateIssueTests`, `StockAtHandAdjustmentTests`, `SearchableSkuTests`,
  `StockAtHandSearchTests`, `ReorderingTests`.
* **NEW `frontend/tests-js/test-inventory-batch01-management.js`** — 11 Node tests slicing the real
  page functions (unit labels, duplicate-SKU message, exact reorder arithmetic, review text, gating
  and wiring assertions).

---

## G. Explicitly NOT changed

Accounting, procurement approval logic (`submit/approve/reject/cancel/ordered/pay`, approvals inbox),
bookings/stays, staff approval permissions, kitchen/production, POS order logic, `apps/audit`
services, `apps/core` permissions/pagination, authentication, and every other dashboard page.
`log_action()` is **used** (never modified) for the two new audited inventory actions.
