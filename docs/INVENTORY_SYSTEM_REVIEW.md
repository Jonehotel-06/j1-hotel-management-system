# Inventory system review — sellable products, ingredients, consumables, assets

Release **1.1.22**. Everything below was implemented in this repository, migrated, and
verified by the tests named in §8. Nothing was rebuilt from scratch, nothing was
deleted, and no existing stock record, recipe, purchase, sale, receipt or accounting
transaction was changed.

Two rules governed the whole task:

* **Never fabricate configuration.** No recipe, balance, cost or deduction is invented,
  guessed or adjusted to make a test pass. A product deducts stock only because a human
  configured it — a real `StockRecipe`, or an explicit direct-sale configuration.
* **Never change a financial or stock figure silently.** Anything the operator cannot
  see (an unconfigured product, a shortage, an over-return) parks with written evidence,
  and every internal issue is an auditable ledger row rather than a quiet quantity edit.

---

## 1. What the system already had (discovered functionality)

The inventory app was already a complete ledger; the review found that most of the
requested behaviour existed and had to be *wired to* rather than replaced.

| Surface | What it already did |
|---|---|
| `StockLocation`, `StockItem`, `StockBalance` | Every item/location pair carries a locked balance projection (`unique(item, location)`); balances are disposable, movements are the truth. |
| `StockMovement` | **Append-only** signed movement (save/delete raise). Ten types: `OPENING`, `RECEIPT`, `ISSUE`, `POS_CONSUMPTION`, `MAINTENANCE_ISSUE`, `MAINTENANCE_RETURN`, `TRANSFER_OUT`, `TRANSFER_IN`, `ADJUSTMENT`, `WRITE_OFF`. Idempotent on a unique `source_key`, and `record_stock_movement` never drives a balance below zero. |
| Row locking | `_locked_balance()` takes `select_for_update` on the item, the location and the balance row before every write — concurrent issues/sales already could not oversell. |
| Procurement | `PurchaseOrder` + `GoodsReceipt` (+lines) with the whole draft → submitted → approved → ordered → received workflow, and `receive_goods` writing `RECEIPT` movements. |
| Recipes | Versioned `StockRecipe` (`unique(menu_item, version)`, one active version) with `COMPONENTS` or `NO_STOCK` tracking, per-line quantities, idempotency keys, retirement of the previous version, and inventory counts (`StockCount`). |
| POS hand-off | `StockConsumptionRequest` (+ append-only events, frozen `recipe_snapshot`, `resolution_payload`) created on POS delivery, processed in the same transaction by `process_pos_stock_consumption`, reversed on void/return by `reverse_pos_stock_consumption`, with an idempotent catch-up sweep (`process_pending_pos_consumption`). |
| Transfers | `transfer_stock` writes both legs (`…:out` / `…:in`) atomically towards a transfer key. |
| Valuation | `valuation_service` derives the weighted-average purchase cost from `RECEIPT` movements, falls back to `standard_cost` and discloses which basis (`cost_basis`) was used. |
| Barcode | `barcode_service.resolve_barcode` resolves a scanned code to an active, available menu item in the caller's service areas and returns the authoritative `base_price`; it never deducts. |
| Console | `frontend/dashboard/inventory.html` with stock, counts, consumption, recipes, catalog, ledger and profitability panels, bounded pagination and skeleton/error states. |
| Check-out | `_folio_clearance_payload` already reports delivered-but-unbilled POS orders and unbilled laundry as `unpaid_requests` and blocks the gate pass on `OUTSTANDING_BALANCE` / `UNPAID_REQUESTS` / `OPEN_REQUESTS`. |

## 2. What was actually wrong (the six findings)

1. **A wrong unit was deducted silently.** `convert_stock_quantity()` returned a quantity
   *unchanged* when it did not recognise a unit pair, so a line typed in kilograms
   against an item stocked in litres (or in sachets) posted a plausible-looking but wrong
   deduction with no error at all.
2. **Recipes could not be trusted per line.** The console accepted a bare number per BOM
   line with an optional unit from a short allow-list, and never checked that the unit
   was even expressible in the component item's own base unit.
3. **Recipes were fabricated from a name.** On POS delivery, a menu item whose SKU or
   name matched a stock item caused the system to *create* a `StockRecipe`
   (`notes="Auto-linked 1:1 …"`, key `auto-sku-recipe:…`) behind the operator's back, and
   that invented recipe then deducted stock forever.
4. **Internal issues were anonymous.** An issue recorded only item/location/quantity/actor:
   no receiving department, no recipient, no reason, no way to say who held a reusable
   asset, and no ceiling on "returning" the same issue twice.
5. **Nothing distinguished a towel from a bag of rice.** There was no field separating a
   directly sellable product, a recipe ingredient, an operational consumable and a
   reusable asset, so no screen, filter or report could treat them differently.
6. **A crate could only be expressed as a recipe**, and two stock items could claim the
   same menu item — the same sale deducted twice.

## 3. What was implemented

### 3.1 Classification (`StockItem.item_type`)

Four classes, exactly the four the brief names: `SELLABLE_PRODUCT`,
`RECIPE_INGREDIENT`, `CONSUMABLE` (default), `REUSABLE_ASSET`. The field is indexed,
filterable (`?item_type=` on balances and movements), shown in the catalog, and selects
the rules that apply: asset issues demand a named custodian, and the audit reports the
three questions an operator has to answer. `REUSABLE_ASSET` is the only class with an
extra runtime rule, so the default is deliberately inert.

### 3.2 Honest units, exact conversions

`UNIT_FAMILIES` gives every known unit a *family* and a *ratio*: mass (`mg 0.001`, `g 1`,
`kg 1000`, `t 1e6`), volume (`ml 1`, `cl 10`, `dl 100`, `l 1000`, `shot 50`) and count
(`pc/unit 1`, `dozen 12`, `pair 2`). A conversion is allowed only when **both** units are
in the same family; packs (`pack`, `carton`, `case`, `crate`, `bag`, `box`, `sack`, `roll`,
`bundle`) convert only through the item's own `units_per_pack`. Anything else raises
`ValidationError` naming both units and telling the operator what to do instead. Blank or
identical units are the identity, an explicit `conversion_factor` still wins, and every
result is an exact `Decimal` quantised to four places (`ROUND_HALF_UP`) — 250 g of rice is
`0.2500 KG`, never a rounded guess. Recipe lines store the converted base-unit quantity
**and** keep the originally typed value as evidence (`entered_quantity`, `entered_unit`),
and the deduction never uses the typed value.

### 3.3 Direct POS sales are explicit (`direct_sale_*`)

A stock item can name the POS menu item it is sold as (`direct_sale_menu_item`), the store
it is drawn from (`direct_sale_location`) and how many base units one menu unit consumes
(`direct_sale_quantity`: `1` for a bottle, `24` for a crate sold as 24 bottles). A partial
unique constraint plus a serializer pre-check make **one menu item claimable by exactly one
stock item**, so the same sale cannot be deducted twice. The configuration is resolved by
`_direct_sale_snapshots()` into the same snapshot shape a recipe produces
(`tracking_mode=COMPONENTS`, one component), so shortage checks, per-item movements,
evidence metadata and reversal are unchanged — and **no recipe row is created**. Clearing
the menu item also clears the store and resets the quantity, so a stale half-configuration
can never keep deducting. The whole sale path (`request_pos_stock_consumption` →
`process_pos_stock_consumption` → `consume_stock_for_delivered_order`, the catch-up sweep,
and `reverse_pos_stock_consumption`) is the pre-existing one; barcode scanning, receipts
and order totals are untouched.

### 3.4 No inferred recipe (requirement 5)

The `auto-sku-recipe:…` fabrication is gone: the POS path never creates, replaces, retires
or edits a recipe. Existing auto-created recipes keep deducting exactly as they did, and
the audit lists them with advice. A delivered line with no recipe and no configuration
deducts nothing; it is recorded as `uncontrolled` evidence, and **if nothing at all could
be deducted the hand-off parks `FAILED`** with a note naming the products and the two ways
to configure them, so the gap is visible in the console and the catch-up sweep keeps
offering it until someone configures it. When some lines resolved, the configured lines
post and the unconfigured ones are listed as "Not deducted — …". An *invalid* recipe, an
inactive item/location or a shortage still parks the whole hand-off **before** anything
posts, so a partial deduction never reaches the ledger.

### 3.5 Internal issuance with custody (requirement 4)

`issue_stock()` now records `department`, `recipient` and the reason (`notes`) on the
immutable movement, alongside the issuing user and date; `record_stock_return()` records
the returning department/recipient and — when the reference cites an issue — **caps the
return at what was actually issued** by locking the cited issue and summing prior returns.
Reusable assets require a named custodian (and a reason before write-off). Because an issue
is the movement of goods that already entered stock through a goods receipt, an internal
issue creates **no journal entry, no POS revenue and no supplier payment** — it cannot
double-count an expense — while the ledger keeps the full issue/return/adjustment history.
Server-side permissions are unchanged and still enforced (`inventory.manage` for issues,
returns and recipe writes: a housekeeping login is refused with 403, proved by test).

### 3.6 Locations, balances and valuation (requirement 6)

Balances are only ever moved through `record_stock_movement`, which locks item + location +
balance row, refuses a negative result, and is idempotent on its `source_key`; transfers
write both legs atomically, so the total is preserved. The weighted-average purchase cost
still comes from `RECEIPT` movements with the disclosed `standard_cost` fallback; a
configured direct sale is priced at `base_price / direct_sale_quantity` (a crate of 24 at
₦6,000 is ₦250 per bottle), and a `MENU_ITEM_MATCH` projection reports `recipe_count = 0`
because no recipe exists for it. Nothing merges apparently-duplicate items: the audit lists
them side by side with per-location balances, movement counts and received units, because
two items with the same name can be genuinely different products.

### 3.7 Operator surfaces

* `frontend/dashboard/inventory.html`: an **item type** select in both the create and the
  edit dialog; a "sold through POS as / stock is deducted from / base units one menu unit
  deducts / remove the configuration" block using the existing search pickers; a **Type**
  column that shows the direct-sale configuration; an **Item type** filter on stock on hand;
  a **receiving department**, **custodian** and **reason** on the internal issue dialog; a
  **returning department / person / issue reference** on the return dialog; a **Department**
  filter and the issuing evidence in the movement ledger; and a hand-off detail that names
  the direct sale, lists what was *not* deducted, and states that the guest's charge is
  unaffected. No new endpoint, no new client, no new dependency.
* `manage.py inventory_configuration_audit` (read-only, `--limit`, `--json`): configured
  direct sales (and whether each is complete), auto-linked 1:1 recipes that still deduct,
  stock items that look like an active menu item but deduct nothing (with the exact `PATCH`
  advice), recent internal issues, reusable-asset custody with units still out, and
  candidate duplicate records with their balances and histories. It writes nothing.

## 4. Files created and modified

**Created**

| Path | Purpose |
|---|---|
| `backend/apps/inventory/migrations/0006_item_types_direct_sale_and_issue_departments.py` | The eight new columns + the one-menu-item-per-stock-item constraint. |
| `backend/apps/inventory/migrations/0007_classify_existing_stock_items.py` | Conservative, evidence-only `item_type` backfill (reversible). |
| `backend/apps/inventory/management/commands/inventory_configuration_audit.py` | The read-only configuration audit / duplicate report. |
| `backend/tests/test_inventory_item_management.py` | 32 tests covering the ten required areas, the configuration rules and the backfill. |
| `frontend/tests-js/test-inventory-item-management-ui.js` | 10 tests over the shipped page source (types, direct sale, issues, returns, ledger, hand-off evidence). |
| `docs/INVENTORY_SYSTEM_REVIEW.md` | This report. |

**Modified**

| Path | Change |
|---|---|
| `backend/apps/inventory/models.py` | `StockItem.ItemType` + `item_type`; the `direct_sale_*` trio and its partial unique constraint; `is_direct_sale_configured` / `is_reusable_asset`; `StockMovement.department` / `recipient`; `StockRecipeLine.entered_quantity` / `entered_unit`. |
| `backend/apps/inventory/services/inventory_service.py` | Unit families + strict `convert_stock_quantity`; `DIRECT_SALE_SOURCE` and `_direct_sale_snapshots()`; department validation; `issue_stock` custody rules; `record_stock_return` ceiling; `create_stock_recipe` unit conversion + typed-value evidence; `process_pos_stock_consumption` unconfigured/park rules. |
| `backend/apps/inventory/serializers.py` | Item `item_type` + direct-sale read/write fields with the claim pre-check and clearing normalisation; balance/ledger/movement `item_type`, `department`, `recipient`, `base_unit`; recipe line `entered_*`; issue/return `department` + `recipient`. |
| `backend/apps/inventory/views.py` | `?item_type=` and `?department=` filters; issue/return endpoints pass the new evidence fields; permissions unchanged. |
| `backend/apps/inventory/services/valuation_service.py` | Direct-sale pricing per base unit, direct-sale resolution before the display-only menu-item match, `recipe_count` 0 for `MENU_ITEM_MATCH`. |
| `backend/tests/test_enterprise_upgrade_suite.py` | Its fixture configures the water direct sale explicitly instead of relying on the removed 1:1 fabrication. |
| `frontend/dashboard/inventory.html` | All of §3.7. |
| `docs/USAGE.md`, `ACCOUNTING_IMPLEMENTATION_REPORT.md` | Operating instructions (§13) and the release record. |

No file in `backend/apps/pos/` was modified: the POS deduction, barcode and receipt code
paths are exactly as they were.

## 5. Migrations and deployment

```
backend/apps/inventory/migrations/0006_item_types_direct_sale_and_issue_departments.py
backend/apps/inventory/migrations/0007_classify_existing_stock_items.py
```

`0006` only **adds** nullable/defaulted columns plus one conditional unique constraint; it
reads nothing and changes no row. `0007` reads the existing recipe book and labels what the
data already proves:

* an item that is a BOM line of an active `COMPONENTS` recipe which is *not* a one-for-one
  single-line recipe → `RECIPE_INGREDIENT` (rice, Maggi, oil);
* an item that is the single one-for-one line of an active recipe → `SELLABLE_PRODUCT`
  (a bar bottle/water/pack deducted once per menu unit);
* everything else keeps the inert `CONSUMABLE` default, and a row an operator has already
  classified is never overwritten. The reverse operation sets back only rows that still
  match the same evidence.

Deployment (production):

```bash
# 0. Take the usual database backup first. Nothing here is destructive, but a backup is
#    still the only way to undo a deployment you did not intend.
cd backend
git pull                       # or deploy the built release
python manage.py migrate       # applies inventory.0006 then inventory.0007
python manage.py check
python manage.py makemigrations --check --dry-run   # must print "No changes detected"
python manage.py inventory_configuration_audit      # read-only: what to configure next
python manage.py inventory_configuration_audit --json > /tmp/inventory-audit.json
```

Then release the frontend as usual (`frontend/` build → 1.1.22). Nothing else is required:
no data reset, no revaluation, no re-post, no background job. Balances, movements, recipes,
purchases, receipts and valuations are byte-for-byte what they were, and the audit is the
supported way to confirm it (`nothing was changed, merged, reset or deleted`).

Roll back with `python manage.py migrate inventory 0005_stockitem_pack_packaging` if the
release has to be withdrawn; `0007`'s reverse pass restores only the labels it set, and
`0006` drops only the new columns.

## 6. Manual configuration needed for existing items and recipes

The migration classifies what the recipe book proves and nothing else. These are the
operator steps that remain — all of them optional in the sense that nothing breaks without
them, and all of them visible in `inventory_configuration_audit`:

| Item | Console step | Why |
|---|---|---|
| Bottled drinks, beer, packaged water, packs/crates sold through the POS | Inventory → Stock catalog → edit the item → *How this item is used* = **Directly sellable product**, *Sold through POS as* = the menu item, *Stock is deducted from* = the bar/store it is drawn from, *Base units one menu unit deducts* = `1` (bottle) or `24` (crate). Save. | Until then a sale of that product deducts nothing and parks a reviewable hand-off. |
| Rice, Maggi, salt, oil, tomatoes, meat | Inventory → Recipes → the menu item's recipe: type the quantity in the unit the kitchen actually uses (`250` + `g`) and Save. | The deduction is exact and the typed value is kept as evidence. |
| Cleaning products, laundry detergent, toiletries | *How this item is used* = **Operational consumable**; then Inventory → Issue stock with the receiving department and the reason. | Consumables leave through an issue; they are not sold and not re-expensed. |
| Towels, bedsheets, glasses, plates, cutlery, equipment | *How this item is used* = **Reusable asset**; issue with the person taking custody; record returns with the issue reference. | Custody is then named, write-offs need a reason, and the audit shows what is still out. |
| Recipes created automatically by the old 1:1 behaviour (`notes` starts with `Auto-linked 1:1`) | The audit lists each one. Confirm it is correct, **or** configure the direct sale and retire the recipe deliberately. | They still deduct; nothing is replaced automatically — that decision is the operator's. |
| Items that appear twice with the same name | The audit prints both records with their balances, movement counts and received units. | Only a human can tell a genuine second product from a duplicate; nothing is merged. |

## 7. What already worked and was deliberately not changed

* The POS delivery → hand-off → consumption → reversal chain, its statuses, its
  `source_key`s and its catch-up sweep (`process_pending_pos_consumption`).
* Barcode resolution and the receipts (POS, thermal, folio/gate pass) — byte-for-byte the
  same payloads, including the SKU-as-barcode behaviour.
* Procurement (`PurchaseOrder`/`GoodsReceipt`), counts, adjustments, write-offs, opening
  stock, transfers and their existing validations.
* Weighted-average valuation from receipts, the `standard_cost` fallback and its
  disclosure; no figure was recalculated and no valuation rule was altered.
* Server-side permissions, the capability names, the API paths and the request/response
  shapes of every existing endpoint (`?item_type=`/`?department=` are additive filters, and
  the new request fields are optional).
* The check-out folio clearance and gate pass (delivered-but-unbilled POS orders and
  unbilled laundry already appear as `unpaid_requests`).
* The inventory console's existing tabs, pagination, empty states and table contracts.

## 8. Test evidence

Backend, `backend/` (`python3 manage.py test … --parallel 1`):

| Area | Where it is proved |
|---|---|
| 1. Direct POS bar deduction | `DirectSaleDeductionTests` — a configured crate deducts 24 bottles, from the configured store only, once, with the direct sale named in the movement evidence and **no recipe row created**; the POS stock summary reports 2 whole crates from 50 bottles. |
| 2. Recipe-only ingredient deduction | `RecipeUnitDeductionTests` — a plate deducts 0.2500 KG of rice, the typed `250 g` kept as evidence, no whole bag anywhere. |
| 3. Unit-conversion accuracy | `UnitConversionTests` — mass/volume exactness, packs only through `units_per_pack`, and an unknown or cross-family pair **refused** with both units named. |
| 4. Internal issue reduces the source and is auditable | `InternalIssueTests` — the source balance falls by exactly the issued quantity, the movement carries department, recipient, reason, actor and date; returns are capped by the issue they cite. |
| 5. Internal issues create no POS revenue | `InternalIssueTests` — no financial transaction, no folio posting, no expense, no POS order is created by an issue. |
| 6. Cancelled / failed / retried transactions | `IdempotencyTests` — a retried issue returns the same movement, a retried delivery or catch-up sweep deducts once, an unconfigured product never blocks the configured lines, and a parked hand-off deducts exactly once when the product is configured later. |
| 7. Concurrency cannot go negative | `ConcurrencyTests` — parallel issues against one balance: the ledger stays at or above zero and no oversell is posted. |
| 8. Transfers preserve the total | `TransferIntegrityTests` — both legs post or neither does, the item total is unchanged, the key is idempotent, and a transfer beyond the source balance is refused. |
| 9. Weighted-average cost / valuation | `ValuationIntegrityTests` — the weighted average survives receipts, issues and transfers and is disclosed by `cost_basis`; a direct sale is priced per base unit. |
| 10. Existing recipes, history and unrelated workflows | `ExistingBehaviourTests` + `ItemTypeApiTests` + `StorekeeperPermissionTests` — existing recipes still deduct and are never retired by an unrelated change, barcode lookup and order totals are unchanged, the audit changes nothing, the four types are stored/filterable, and a role without the capability is refused (403) server-side. |
| Requirement 5 (no inferred recipe) | `NoAutomaticRecipeTests` — a matching SKU creates no recipe and deducts nothing; an invalid recipe still parks the whole order with evidence. |
| Deployment backfill | `ItemTypeBackfillTests` — only recipe evidence is classified, an operator's own classification is never overwritten, balances/movements/recipes are untouched, running it twice changes nothing, and the reverse pass reverts only its own rows. |

Results on this revision:

| Command | Result |
|---|---|
| `python3 manage.py test tests.test_inventory_item_management --parallel 1` | **Ran 32 tests … OK** (18.6 s) |
| `python3 manage.py test tests.test_inventory tests.test_inventory_api tests.test_inventory_sales_depletion tests.test_accounting_inventory_reconciliation tests.test_enterprise_upgrade_suite tests.test_pos_api tests.test_pos_department_api tests.test_service_request_pos_handoff --parallel 1` | **Ran 47 tests … OK** |
| `python3 manage.py test tests.test_inventory_item_management tests.test_pos --parallel 1` | **Ran 39 tests … OK** |
| `python3 manage.py check` | no issues |
| `python3 manage.py makemigrations --check --dry-run` | `No changes detected` |
| `python3 manage.py migrate` (sandbox) | `inventory.0006` + `inventory.0007` applied |
| `python3 manage.py inventory_configuration_audit` (and `--json`) | read-only report produced; `nothing was changed, merged, reset or deleted` |
| `cd frontend && python3 validate.py` | 70 HTML / 30 JS **ALL OK** |
| `cd frontend && node --test tests-js/` | **471 tests: 469 pass, 1 skipped, 1 pre-existing failure** — `test-pwa-dashboard-only.js` root web-app-manifest scope, unrelated to this work and failing before it |
| `cd frontend && node --test tests-js/test-inventory-item-management-ui.js` | **10 tests, 10 pass** |

**Unresolved failures: none introduced by this work.** The only failing test in the
repository is the pre-existing PWA root-manifest assertion noted above; it is untouched by
this release.

## 9. Honest limits

* **Two production decisions belong to the operator**, and the system deliberately does not
  make them: which existing items are reusable assets (the migration classifies nothing by
  name), and whether an auto-created 1:1 recipe should become an explicit direct sale.
  The audit lists both with the exact advice; until the asset classification is made, those
  items behave as consumables and their issues do not demand a custodian.
* **A reusable asset cannot be returned twice against one issue**, and a return without an
  issue reference keeps the old uncapped behaviour — the ceiling needs a reference to check
  against.
* **`item_type` is a label, not a stock figure.** Changing it never moves a balance; the
  only runtime rule attached to it is the custodian requirement for `REUSABLE_ASSET`.
* **Direct-sale availability is per configured store.** If the configured store is empty,
  the POS *stock summary* reports zero saleable while another store holds the item — the
  ledger is right, the summary is the configured store's view, and a shortage parks the
  hand-off rather than deducting from a store the operator did not choose.
* The audit is read-only and therefore cannot detect a genuine duplicate on its own; it
  shows the balances and histories and waits for a human.
