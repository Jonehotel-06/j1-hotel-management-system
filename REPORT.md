# Final Report — POS & Room Service Out-of-Stock Validation and Stock-Source Display

**Project:** J-ONE Hotel & Lodge — `j1-hotel-management-system` (Django + DRF + MySQL backend, vanilla JS frontend)
**Baseline:** commit `5aeeb18` ("cashier to restaurants")
**Scope actually changed:** POS page and Room Service order mode only (both live in `frontend/dashboard/pos.html`; Room Service is `PosOrder.Mode.ROOM_SERVICE`)
**Result:** 9 files modified + 2 test files added; 844 insertions / 85 deletions; **no schema or migration changes**; related backend suite 186/186 OK; new backend tests 31/31; new frontend contract tests 15/15; no regressions vs. baseline.

---

## A. Root-cause analysis

1. **Order entry never consulted inventory.** There is exactly one order-creation entry point — `PosOrderListCreateView.post` (baseline `views.py:780`) → `order_service.create_order` (also used by the QR guest-request flow). Drafts are created atomically with all their lines in one POST; there are no add-line endpoints. Neither `create_order` (baseline `order_service.py:133`) nor `submit_order` (baseline `order_service.py:308`) looked at stock. A bar product with zero or insufficient stock sailed into DRAFT/SUBMITTED/PREPARING/READY, and the failure surfaced only at DELIVERED, where `consume_stock_for_delivered_order` parks the deduction when the store is short (guest already promised the item). Nothing prevented several open orders from collectively promising more than a store held.

2. **The menu/barcode APIs carried no inventory facts.** `MenuItemSerializer` had no stock field, and `barcode_service.stock_summary_for_menu_item` returned only a stub summary. The POS page therefore could not display the stock source even if it wanted to.

3. **The POS page displayed no stock information anywhere.** The item picker, cart lines and scan flow showed no store, no available quantity, no unit — and the scan flow's only stock handling was a soft "recorded stock may be insufficient" note that still added the item.

The stock *deduction* itself was already correct (delivery-time, exact quantities, from the configured store, once, all-or-nothing, never negative). The fix therefore adds **validation and display** in front of it and changes nothing about the deduction.

---

## B. Files changed (9 modified, 2 new)

| # | File | Change |
|---|------|--------|
| 1 | `backend/apps/core/exceptions.py` | New `PosStockConflictError` (HTTP 409, `POS_STOCK_CONFLICT`) with `errors_payload` carrying product/store/quantity figures; auto-surfaced by the existing `jone_exception_handler`. |
| 2 | `backend/apps/inventory/services/inventory_service.py` | New public resolvers `pos_stock_rules()` (baseline line 868 → inserted at new 869) and `pos_stock_summaries()` (new 1023): the recipe/direct-sale → source-store mapping, identical to what the deduction uses. |
| 3 | `backend/apps/pos/services/order_service.py` | New `_validate_order_lines_stock()` + helpers, wired into `create_order` and `submit_order`. |
| 4 | `backend/apps/pos/services/barcode_service.py` | `stock_summary_for_menu_item` delegates to `pos_stock_summaries`; new `resolve_menu_item()`; `barcode_lookup_payload` accepts item-or-code (`matched_by="menu_item"`). |
| 5 | `backend/apps/pos/serializers.py` | `MenuItemSerializer.stock` method field; `get_items` forwards context; `matched_by` choices + `"menu_item"`. |
| 6 | `backend/apps/pos/views.py` | `PosMenuView` bulk stock summaries into serializer context (one query per page, no N+1); `PosBarcodeLookupView` optional `?menu_item_id=`. |
| 7 | `frontend/dashboard/pos.html` | Stock-source display in picker/cart/scan; live pre-add verification (fail-safe); the four refusal messages; `+`-button ceiling; 409 refresh; room-service wording; no hardcoded store names. |
| 8 | `backend/tests/test_pos_stock_validation.py` | NEW — 31 tests (see E). |
| 9 | `frontend/tests-js/test-pos-stock-ui.js` | NEW — 15 static contract tests. |
| 10 | `backend/tests/test_inventory_sales_depletion.py` | `InsufficientStockTests` rewritten for the new create-time policy (3 tests; delivery-time parking still covered for shortages arising after creation). |
| 11 | `backend/tests/test_inventory_item_management.py` | 2 tests updated to the new policy (invalid-recipe blocks at creation — delivery-time evidence path still covered; barcode/total test seeds stock). |

**Explicitly untouched:** front-desk sign-in, auth, staff accounts/roles, bookings, room assignments, payments/Paystack, accounting, payroll, kitchen ops, menu management, suppliers, receipts, reports; `frontend/js/config.js` / `api.js` (existing endpoints reused); CSS (existing `.hint`, `.field-error.is-banner` classes reused); the deduction flow itself (`transition_order`, `consume_stock_for_delivered_order`, restock sweep, returns).

---

## C. Isolated-folder contents (`JONE_POS_ROOMSERVICE_STOCK_FIX/`)

```
JONE_POS_ROOMSERVICE_STOCK_FIX/
├── FILE_MANIFEST.md          — every file, purpose, line-number table, test status
├── CHANGELOG.md              — before/after narrative
├── PATCH.diff                — unified diff vs 5aeeb18 (all 11 files, 2086 lines)
├── backend/apps/core/exceptions.py            (full fixed file)
├── backend/apps/inventory/services/inventory_service.py
├── backend/apps/pos/serializers.py
├── backend/apps/pos/services/barcode_service.py
├── backend/apps/pos/services/order_service.py
├── backend/apps/pos/views.py
├── backend/tests/test_inventory_item_management.py
├── backend/tests/test_inventory_sales_depletion.py
├── backend/tests/test_pos_stock_validation.py (new)
├── frontend/dashboard/pos.html
└── frontend/tests-js/test-pos-stock-ui.js    (new)
```

Verified: `PATCH.diff` applies cleanly (`git apply --check`) to a pristine `git archive 5aeeb18` tree, and every file in the folder is byte-identical to the fixed working tree. Tests were re-run **from the freshly patched tree** (82/82 OK) to prove the folder is self-sufficient. The original project repo was not replaced.

---

## D. Line-by-line manual-fix instructions (original → modified line numbers)

Apply order: 1 → 7 (backend first, then frontend). "o" = original line in `5aeeb18`, "n" = line after the fix.

**1. `backend/apps/core/exceptions.py`** (201 → 227 lines)
- Insert after **o132** (blank line following `RestaurantTableSessionConflictError`, which spans o125–o131; `ServiceQRExpiredError` follows at o133 and shifts down): the 26-line `class PosStockConflictError(JOneAPIError)` → **n133–158**. HTTP 409, `default_code="POS_STOCK_CONFLICT"`, `__init__(detail, *, context)`, `errors_payload` property returning `{reason, items}`.

**2. `backend/apps/inventory/services/inventory_service.py`** (1654 → 1885)
- Insert after **o868** (`return snapshots` that ends `_active_recipe_snapshots`; `_consumption_event` follows at o870 and shifts down): `pos_stock_rules(menu_item_ids)` → **n869–1019** and `pos_stock_summaries(menu_item_ids)` → **n1023–1099**. `pos_stock_rules` resolves, per menu item, the active COMPONENTS recipe or the direct-sale config into `{location_id, location_code, location_name, components: [{item_id, item_sku, item_name, unit, quantity_per_menu_unit}], direct_sale, active}`; `pos_stock_summaries` adds on-hand balances (locked read, same as the deduction) minus open-order demand → `{tracked, unconfigured, unconfigured_reason, location_code, location_name, saleable_quantity, sufficient_for_one, stock_unit, components}`.

**3. `backend/apps/pos/services/order_service.py`** (638 → 895)
- Insert after **o131** (end of `_room_service_context`, o118–131; `create_order` follows): `_STOCK_COMMITTED_STATUSES` → **n136**, `_display_unit` → **n141**, `_qty_text` → **n152**, `_stock_conflict_context` → **n160**, `_validate_order_lines_stock` → **n164–374**.
- In `create_order` (o133 → **n376**): insert `_validate_order_lines_stock(snapshotted_lines)` at **n483** — after the `_snapshot_line` loop (o~236), before subtotal computation.
- In `submit_order` (o308 → **n556**): insert the re-validation call at **n570–573** — after the state/lines guards, with `exclude_order=order`, reading `PosOrderLine.objects.filter(order=order).values("menu_item_id", "quantity", "item_name")`.
- `_validate_order_lines_stock` aggregates demand per (component item, location) across all lines and all *other* open orders (DRAFT/SUBMITTED/PREPARING/READY), resolves each line through `pos_stock_rules`, reads balances via the same locked helper and sort order the delivery-time deduction uses, and raises `PosStockConflictError` with the product/store/quantity/unit message variants (out-of-stock; insufficient with available + held figures; unconfigured/broken recipe; multi-component aggregate form). Untracked items are skipped (kitchen unaffected). Nothing is written before validation passes.

**4. `backend/apps/pos/services/barcode_service.py`** (150 → 138)
- o11–18 imports → **n11–16**: remove the unused `Decimal` import.
- Replace `stock_summary_for_menu_item` (o73–92) → **n71–90**: now delegates to `pos_stock_summaries` and returns its real figures.
- Insert `resolve_menu_item(menu_item_id, *, service_areas=None)` → **n104–117** (area-scoped menu-item resolution, same visibility rules as `resolve_barcode`).
- Replace `barcode_lookup_payload(code, …)` (o134) with `barcode_lookup_payload(item_or_code, *, service_areas=None)` → **n119–138**: accepts a `MenuItem` (calls the summary directly, `matched_by="menu_item"`) or a code string (previous behaviour unchanged).

**5. `backend/apps/pos/serializers.py`** (645 → 668)
- In `MenuItemSerializer` (o21): add `stock = serializers.SerializerMethodField()` (→ **n24**) and `get_stock` (→ **n35–51**) reading the bulk `pos_stock_summaries` dict from context, with a per-item fallback.
- `BarcodeLookupSerializer.matched_by` choices → **n67**: `["barcode", "sku", "menu_item"]`.
- `MenuCategoryMenuSerializer.get_items` (o~70 → **n86–94**): pass `self.context` into the child serializer so the stock context flows through.

**6. `backend/apps/pos/views.py`** (1142 → 1172)
- `PosMenuView.get` (o179): after the queryset/serializer setup (o~214), add the bulk resolver → **n208–227** (docstring note + import) and **n231–243** (collect `menu_item_ids` from the serialized categories, call `pos_stock_summaries` once, inject into `context`).
- `PosBarcodeLookupView.get` (o227): docstring → **n247–258**; parameter parsing → **n289–304** — read optional `menu_item_id` query param, `int()`-parse with a 400 on garbage, resolve via `resolve_menu_item` with the same `mode`/service-area scoping, build the payload with `barcode_lookup_payload(menu_item)`, and return **404 (NotFound)** when the item is not sellable in the current mode.

**7. `frontend/dashboard/pos.html`** (1223 → 1349)
- **o147 → n150**: add `<div class="hint" data-pos-item-stock role="status" aria-live="polite">…</div>` inside the menu-item field (reuses the existing `.hint` CSS).
- **o379–393 → n383–432**: new helper block after `renderItemChoices`' old body — `orderNoun()` (n388), `unitWord()` (n389, singular/plural from the server unit), `stockLocationName()` (n397), `stockOptionSuffix()` (n398, option label suffix " · Store · N available" / " · out of stock" / " · stock location unavailable"), `renderItemStockHint()` (n406, the live status line: "Stock location: … · Available stock: N unit"), and `renderItemChoices` now appends the suffix and calls `renderItemStockHint()` (**o362 → n365** wiring plus the `pos-item` change listener at n~535 calls both `renderModifiers()` and `renderItemStockHint()`).
- **o549–564 → n588–668**: replace `addCartLine` with `verifyItemStock(item)` (**n597** — GET `posBarcodeLookup` with `params={menu_item_id, mode}` and `cache:"no-store"`; 404 → "no longer available"; any other error → "We couldn't verify the stock for X. The item has not been added."; **never falls back to stale figures**), `reportItemNotAdded(message)` (**n612** — toast + inline status), and `async addCartLine()` (**n617**) guarded by `JONE.guardSubmit`/`releaseGuard` on the Add button, refusing unconfigured / zero / insufficient quantities with the exact messages, then pushing the line with `stock_location` / `stock_available` / `stock_unit` fields.
- **o757 → n857**: cart `+` handler — `stockCeiling = stock_available == null ? 100 : min(100, stock_available)`; hitting the ceiling toasts the insufficient-stock message with the real figures.
- **o788 → n880–887**: create-draft `catch` — on `err.code === "POS_STOCK_CONFLICT"` call `loadMenu(state.menuArea)` so the picker/hints show current figures; the cart is preserved (unchanged behaviour of the existing handler).
- **o1147–1162 → n1244–1284**: replace `addScannedItem` — hard refusals (returning `false`) for unconfigured / out-of-stock / repeat-scan-exceeds-store, success path stores the same stock fields and sets a status naming store and availability.
- **o1168 → n1293–1303**: `handleScan` now checks `var added = addScannedItem(item); if (added !== false) { …success status… }` so a blocked scan's message is not overwritten.
- Also **o~362 (renderCart region) → n365–366**: each cart row renders `Stock location: {store} · N units available` (small caption line inside the row).

**8–9. New test files:** `backend/tests/test_pos_stock_validation.py` (706 lines) and `frontend/tests-js/test-pos-stock-ui.js` (162 lines) — see E.

**10. `backend/tests/test_inventory_sales_depletion.py`** (470 → 519): rewrite `InsufficientStockTests` (**o317–348 → n317–397**): (i) create-time rejection, (ii) shortage arising *after* creation still parks at delivery and never goes negative, (iii) restock sweep completes the parked deduction. Same fixture and helpers as the rest of the file.

**11. `backend/tests/test_inventory_item_management.py`** (990 → 1019): `test_an_invalid_recipe_still_blocks_the_whole_order` (**o216–236 → n216–263**) now expects the create-time refusal and additionally covers the delivery-time parking when the recipe breaks after submit; `test_barcode_lookup_still_resolves_the_sku_and_the_order_total_is_unchanged` (**o742–747 → n769–775**) seeds stock before selling (its product is stock-tracked now).

---

## E. Test results (all actually run)

**New backend module — `tests/test_pos_stock_validation.py`: 31 tests, 31 OK** (~7 s, SQLite test DB):
- `CreateOrderStockValidationTests` (8): zero stock, insufficient, two-lines-aggregated, exact-quantity boundary, multi-component recipe, crate (24-per-unit) conversion, untracked item passes, room-service mode identical.
- `BrokenConfigurationTests` (3): recipe with no components, inactive location, direct-sale item with no location.
- `DraftIntegrityTests` (2): refused order writes no rows/no lines; partial lines never persisted.
- `DeductionLocationConsistencyTests` (4): menu payload location == deduction movement location (bar store and main store); open-order commitment counted and released on cancellation; delivered order deducts exactly its own quantity.
- `PosStockApiTests` (10): 409 envelope `{success:false, code:"POS_STOCK_CONFLICT", errors:{reason, items:[…]}}` via the real `/api/admin/pos/orders/` endpoint; room-service rejection; menu `items[].stock` payload shape; barcode-lookup `?menu_item_id=`; scan by barcode; area-scoped 404.
- `ConcurrencyTests` (2): sequentially-committed orders cannot collectively oversell (5 bottles, 3+3 refused, 3+2 delivered → 0); threaded burst (4 workers × 6 bottles vs 5) — never oversells, never negative, no partial orders.
- `PosStockValidationTestCase` fixture (2 direct-sale products at two stores, unconfigured kitchen item, in-house stay).

**Related backend suite (17 modules — pos, pos_api, pos_department_api, pos_menu_management, pos_restaurant_tables, pos_unopened_returns, inventory, inventory_api, inventory_item_management, inventory_sales_depletion, inventory_unit_contract, barcode_lookup, barcode_auto_assign, menu_price_cost_guard, service_request_pos_handoff, error_contract, pos_stock_validation): 186 tests, all OK** (~125 s).

**Full backend suite (`python manage.py test`): 1820 tests — 26 failures + 4 errors + 17 skipped.** Every failure was re-run **on the untouched baseline** (fix stashed) with the identical result: all 30 sit in `test_deployment_readiness`, `test_finance` (nightly task), `test_email_pipeline` (redis/celery assertions), `test_attendance_code_and_visibility` (role code issuing) — modules this fix never touches, failing for environment reasons (no celery/redis/SMTP in the sandbox). **Zero regressions attributable to this change.**

**Frontend JS (`node --test tests-js/`): 629/632 pass.** The 15 new `test-pos-stock-ui.js` contracts all pass. The only 3 failures are the pre-existing baseline ones (`test-cloudflare-northflank-deployment.js` "expected 72 HTML pages, found 73", `test-deploy-config.js`, `test-pwa-dashboard-only.js`) — unrelated to POS, not fixed by request (smallest-change rule).

**Isolation-folder self-sufficiency:** `PATCH.diff` was applied to a pristine `git archive 5aeeb18` tree and the three core modules re-run there: **82/82 OK**.

---

## F. Regression status

- **Deduction timing preserved:** stock is still deducted only at DELIVERED via `consume_stock_for_delivered_order`, exactly once, from the same configured store, all-or-nothing, never negative — verified by the untouched lifecycle/retry/sweep/cancellation/fractional/concurrency tests in `test_inventory_sales_depletion` (18/18) and `test_inventory_item_management` (33/33). **No double deduction was introduced** (validation only reads balances through the same locked helper; it never writes movements).
- **Kitchen/untracked items unaffected** — validated by `test_pos.py`, `test_pos_api.py`, `test_service_request_pos_handoff.py` passing unchanged (their fixtures use unconfigured items).
- **Scanner behaviour for in-stock items unchanged** (same endpoint, same envelope; `matched_by` gains a value only when queried by `menu_item_id`).
- **Existing error-contract behaviour preserved** (`test_error_contract` green; `POS_STOCK_CONFLICT` follows the same envelope shape as the existing 409s).
- **Full-suite comparison:** identical results to baseline outside the fixed modules (see E).

## G. Outstanding issues & limitations

1. **Pre-existing failures (not this fix, not in scope):** 30 backend full-suite failures (celery/redis/finance/attendance — environment-dependent) and 3 JS failures (page-count/deploy-config/PWA-scope). Left untouched per the smallest-safe-change rule.
2. **Open-order commitment semantics:** a DRAFT now "holds" its quantity against availability. A draft abandoned in an open status keeps reserving stock until cancelled; the message tells staff how many units "are already held by other open orders". This is the intended anti-oversell behaviour, but staff should cancel abandoned drafts.
3. **Old orders created before deployment** are not retro-validated at creation (impossible); any still in DRAFT *will* be validated when submitted (submit re-checks). Orders already SUBMITTED/PREPARING/READY keep the delivery-time parking safety net.
4. **Frontend pre-check is UX, not security** — by design the backend is authoritative; the frontend never fabricates figures, it only displays server data and re-verifies before adding. A race between the pre-check and submission is still caught by the backend (409 → figures refresh).
5. **Environment limitations (honest statement):** developed and tested in a sandbox on SQLite (development settings); no production system, real MySQL, celery/redis or Paystack environment was available or touched; **no deployment was performed and no production inventory was modified**. Concurrency behaviour under MySQL row locks follows the same code paths exercised by the SQLite tests (the locked-read ordering matches the deduction's), but a staging run on the real MySQL topology before go-live is recommended.
6. No schema changes were needed, so none are pending approval.

## H. Deployment instructions

1. **Backup** the current deployment (at minimum the 9 files listed in B) and the database (read-only precaution — this fix ships no migrations).
2. **Apply the fix** — either copy the 9 modified + 2 new files from `JONE_POS_ROOMSERVICE_STOCK_FIX/` into the working tree at `5aeeb18` (paths are repo-relative), or `git apply PATCH.diff` from the repo root (verified to apply cleanly to a pristine baseline).
3. **No migrations, settings, or dependency changes** are required. `python manage.py check` should report no issues (it does in the test environment). Running `python manage.py migrate` is harmless but unnecessary.
4. **Restart the backend** (Django app server / workers) and **redeploy the static frontend** (`frontend/dashboard/pos.html`).
5. **Verify in staging first** (recommended command set):
   - `python manage.py test tests.test_pos_stock_validation tests.test_inventory_sales_depletion tests.test_inventory_item_management` (82 tests)
   - related regression: the 17-module list in E (186 tests)
   - frontend: `cd frontend && node --test tests-js/`
6. **Smoke test in staging:** open POS → item picker shows "Store · N available"; set quantity above availability → refusal with figures; scan an out-of-stock barcode → refusal; create a draft that exceeds stock → 409 banner + figures refresh; deliver an order → stock deducted from the displayed store.
7. **Rollback:** restore the 9 backed-up files (and delete the 2 new test files if desired); no data cleanup is needed — the fix writes no new tables or columns.
