# BATCH 01 — Inventory Management, Stock Control & Reordering

**Output package:** `ai_updates/batch_01_inventory_management/`
**Source repository:** https://github.com/Jonehotel-06/j1-hotel-management-system
**Base commit audited:** `d9683e1` (`admin manager/procurement dashboard`)
**Batch scope:** inventory stock creation, unit handling, SKU search, stock-at-hand management, search and reordering only.

This package contains **only the files this batch created or modified**, reproduced at their
original repository-relative paths, plus the four required documents. Nothing else from the
application is copied.

---

## 1. How to read this package

| Path in this package | Repository path | Status |
|---|---|---|
| `backend/apps/inventory/models.py` | `backend/apps/inventory/models.py` | modified |
| `backend/apps/inventory/serializers.py` | `backend/apps/inventory/serializers.py` | modified |
| `backend/apps/inventory/services/inventory_service.py` | `backend/apps/inventory/services/inventory_service.py` | modified |
| `backend/apps/inventory/services/valuation_service.py` | `backend/apps/inventory/services/valuation_service.py` | modified |
| `backend/apps/inventory/views.py` | `backend/apps/inventory/views.py` | modified |
| `backend/apps/inventory/urls.py` | `backend/apps/inventory/urls.py` | modified |
| `backend/apps/inventory/migrations/0014_batch01_barcode_and_balance_adjustments.py` | same | **new** |
| `backend/tests/test_inventory_batch01_management.py` | same | **new** |
| `frontend/dashboard/inventory.html` | `frontend/dashboard/inventory.html` | modified |
| `frontend/dashboard/procurement.html` | `frontend/dashboard/procurement.html` | modified |
| `frontend/js/api.js` | `frontend/js/api.js` | modified |
| `frontend/js/config.js` | `frontend/js/config.js` | modified |
| `frontend/js/operations.js` | `frontend/js/operations.js` | modified |
| `frontend/tests-js/test-inventory-batch01-management.js` | same | **new** |
| `PATCH.diff` | — | **new** (integration convenience, git-format unified diff of the 14 files above) |

Counts: **1 new database migration**, **4 new API endpoints**, **5 extended API endpoints**,
**1 new backend test module (38 tests)** and **1 new frontend test module (11 tests)**;
2062-test backend suite and 700-test frontend suite re-run with no new failure.

---

## 2. Task-to-implementation map

### TASK 1 — Fix stock unit fields (base unit vs opening-quantity unit)

| Requirement | Where it is satisfied |
|---|---|
| 1. Distinguish the base unit from the unit used to enter opening quantities | `resolve_entered_quantity()` (single conversion contract) in `inventory_service.py`; `_opening_in_base_units()` in `views.py`; the add-item dialog now labels the two fields explicitly (`Count stock in (base unit)` / `Opening quantity unit`) with help text on each |
| 2. Units consistent through creation, receiving, adjustments, consumption, reporting | one conversion helper used by item creation, opening stock, **replenishment**, **issues (new)**; balances/movements/PO lines/profitability rows now return `base_unit_label` |
| 3. Prevent incorrect conversions (piece/pack/bag/bottle/crate/can/box/carton/gallon/bucket/sachet/kg/litre) | `resolve_entered_quantity()` refuses: different families (kg ↔ litre), a factor that contradicts an exact ratio (2 kg entered as "2000 g with factor 500"), a factor that contradicts the item's own pack size (24-piece crate entered with factor 12), and a container unit with no declared size |
| 4. Crate-based stock consistent when crates are received **or issued** | receiving: `replenish_stock_item()` + existing goods-receipt pack→pieces conversion; issuing: new optional `unit`/`conversion_factor` on `POST /issues/` converts with the item's declared `units_per_pack`; ledger always stores base units |
| 5. Understandable unit labels | server `unit_label()` → `base_unit_label` on items, balances, movements, PO lines, procurement items, profitability rows; page-level `unitLabel()`/`qtyLabel()`/`signedQtyLabel()` fallback for cached payloads |
| 6. Validate quantities and conversion factors | field errors for: quantity ≤ 0, quantity ≥ ledger maximum (10¹⁰), factor ≤ 0, factor contradicting an exact ratio or the declared pack size, factor supplied with a base-unit quantity, container unit without size |
| 7. Preserve balances and history; never silently recalculate | balances/movements untouched; `PATCH items/<pk>/` now **refuses** a `base_unit` change once ledger history exists (it would silently re-interpret every posted quantity); no data migration rewrites a single row |

### TASK 2 — Admin-only stock-at-hand editing

* `POST /api/admin/inventory/balances/<pk>/adjust/` — capability **`inventory.adjust.approve`**
  (Administrator / General Manager / Manager), enforced by `HasCapability` **on the backend,
  including direct API calls**.
* Frontend `Edit` button is rendered only when the same capability is present (`canAdjustBalance()`);
  hiding it is convenience, never the security boundary.
* Controlled modal: read-only item/store/current-balance context + **corrected quantity** (required,
  in the item's base unit) + **reason** (required, ≥ 5 chars) + optional notes.
* Records administrator, previous quantity, new quantity, delta, base unit, reason, notes and
  timestamp in the append-only `StockBalanceAdjustment` table, posts an immutable `ADJUSTMENT`
  movement, and writes an `STOCK_BALANCE_ADJUSTED` audit-log entry (`previous_quantity`,
  `new_quantity`, `quantity_delta`, reason, movement reference).
* Never overwrites a balance in place (the projection moves by the delta), and a replay of the same
  idempotency key returns the existing correction instead of double-posting.
* Valuation/reporting follow the corrected quantity because they read the same ledger/projection.
* Audit trail list: `GET /api/admin/inventory/adjustments/` (`inventory.manage`).

### TASK 3 — Searchable SKU in Add Stock Item

* The SKU field in **Add stock item** is now a search field: typing ≥ 2 characters searches the
  item catalogue (SKU, name, category, barcode). An exact SKU match raises a blocking callout that
  names the existing item and offers **“Replenish this item instead”** (opens the replenishment
  dialog for that item and retrieves its details from the server).
* Replenishment = `POST /api/admin/inventory/items/<pk>/replenish/` (`inventory.manage`). It accepts
  **only** location, quantity, unit/factor, unit cost, supplier reference and notes — it is
  structurally incapable of rewriting name, category, unit, barcode or selling references.
* Duplicate prevention: case-insensitive SKU validation in `StockItemSerializer.validate_sku()`
  (surfaces the existing item by name/sku) on top of the existing `unique=True` database constraint,
  plus a `try/except IntegrityError` → 400 mapping. Barcodes are also uniqueness-checked.
* New stock creation (full dialog) and replenishment (own dialog, own endpoint, own UI copy) are two
  clearly distinguished paths.

### TASK 4 — Search Stock at Hand

* `GET /api/admin/inventory/balances/` now accepts `search` (item SKU, name, category, barcode,
  location code, location name), `category`, `active` and a whitelisted `ordering`; existing
  `item_id`, `location_id`, `reorder`, `item_type` and pagination are unchanged.
* Frontend: a search box above the balances table with debounced, in-place re-render (no page
  reload), a `Clear` action, loading (`DATA.loading`), empty-result (names the searched term) and
  error (`DATA.error`) states, pagination, and the existing View / item-type filters preserved.
* Query cost: `select_related("item", "location")` on one queryset → one page query + one count, no
  N+1; only whitelisted sort keys reach the ORM.

### TASK 5 — Improved reordering workflow

* New **Reorder** dialog on the Procurement page (`Reorder stock item` button) plus a per-row
  **Reorder** link on Stock at Hand (`procurement.html?reorder=<item id>`), shown only to holders of
  `procurement.manage`.
* `GET /api/admin/inventory/procurement/items/<pk>/reorder-context/` (`procurement.manage`) supplies
  name, SKU, category, unit (+label), reorder level, standard cost, preferred supplier, per-store
  balances, total on hand, last actual purchase cost/supplier/date and quantity already on order.
* Staff enter only changeable fields: reorder quantity, purchase unit (base unit or one pack/bag when
  the item declares a pack size), unit purchase price, supplier, delivery location, expected delivery
  date and notes.
* Order total is computed with the same exact decimal arithmetic the server uses (BigInt-scaled
  quantity × cost, half-up to kobo) and is shown live before submission.
* Review step: a confirmation summarising item, quantity in purchase *and* base units, price, total,
  supplier, delivery store, expected date and the explicit statement that no stock is added now.
  “Back to edit” keeps every entry in the dialog.
* Submission creates an ordinary **DRAFT purchase order** through the existing
  `POST purchase-orders/` endpoint (same idempotency key, same validation, same approval workflow).
  **No stock moves**: only an authorised goods receipt increases stock (proved by test).

---

## 3. Safe integration instructions

```bash
# 0. Work on a branch — never on the live working copy.
git clone https://github.com/Jonehotel-06/j1-hotel-management-system.git
cd j1-hotel-management-system
git checkout -b batch-01-inventory-management

# 1. Copy the files of this package over their original paths (they are already
#    repository-relative), e.g.:
cp -a /path/to/ai_updates/batch_01_inventory_management/backend/. backend/
cp -a /path/to/ai_updates/batch_01_inventory_management/frontend/. frontend/

#    …or apply the single unified diff instead:
#    git apply --index ai_updates/batch_01_inventory_management/PATCH.diff

# 2. Migrate (adds one nullable column + one new table; no data rewrite).
cd backend
python manage.py migrate inventory

# 3. Verify.
python manage.py check
python manage.py makemigrations --check --dry-run     # must print "No changes detected"
python manage.py test tests.test_inventory_batch01_management
python manage.py test tests.test_inventory tests.test_inventory_api tests.test_inventory_agent_patch \
    tests.test_inventory_item_management tests.test_inventory_sales_depletion \
    tests.test_inventory_unit_contract tests.test_inventory_unit_migration \
    tests.test_procurement_agent_workflow tests.test_procurement_receipts_and_editing \
    tests.test_product_profitability tests.test_accounting_inventory_reconciliation
cd ../frontend && node --test tests-js/

# 4. Deploy backend and frontend together (the pages use the new endpoints).
```

**Rollback:** `python manage.py migrate inventory 0013` drops the new table and the nullable
`barcode` column. No existing row is altered by either direction, so historical balances and
movements survive a rollback unchanged. Reverting the frontend files alone is also safe: the new
endpoints are additive and the older pages never call them.
