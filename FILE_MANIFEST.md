# JONE_POS_ROOMSERVICE_STOCK_FIX — File Manifest

Fix target: **POS page + Room Service mode** out-of-stock validation and
stock-deduction location display (J-ONE Hotel & Lodge, repo
`Jonehotel-06/j1-hotel-management-system`, baseline commit `5aeeb18`).

**11 files: 9 modified, 2 new. No schema/migration changes. No other
subsystem is touched** (front desk, auth, staff, bookings, payments,
accounting, payroll, kitchen ops, menu management, suppliers, receipts and
reports are untouched).

Every file below is included **in full**, in its repo-relative path, exactly
as it exists after the fix. `PATCH.diff` is the unified diff of the whole set
against baseline `5aeeb18` (`git diff 5aeeb18` for tracked files plus
`git diff --no-index /dev/null <file>` for the two new files).

| # | File (repo-relative) | Status | Purpose |
|---|----------------------|--------|---------|
| 1 | `backend/apps/core/exceptions.py` | modified | New `PosStockConflictError` (HTTP 409, code `POS_STOCK_CONFLICT`) carrying the affected products, stores and quantities in `errors` via `errors_payload`. Surfaced automatically by the existing `jone_exception_handler`. |
| 2 | `backend/apps/inventory/services/inventory_service.py` | modified | Two new public resolvers appended: `pos_stock_rules(menu_item_ids)` (recipe/direct-sale → source store + components) and `pos_stock_summaries(menu_item_ids)` (per menu item: tracked/unconfigured, location code+name, saleable quantity, sufficient-for-one, unit, components). Single source of truth shared by order validation, menu payload, and barcode lookup — the same mapping the delivery-time deduction uses. |
| 3 | `backend/apps/pos/services/order_service.py` | modified | Order-entry enforcement: `_validate_order_lines_stock()` counts open-order demand and available stock (same locked-balance read the deduction uses) and raises `PosStockConflictError` for out-of-stock, insufficient, and unconfigured/broken-configuration items; wired into `create_order` **and** re-checked in `submit_order` (self excluded). Helpers `_display_unit`, `_qty_text`, `_stock_conflict_context`. Deduction timing unchanged (still delivery-time, once). |
| 4 | `backend/apps/pos/services/barcode_service.py` | modified | `stock_summary_for_menu_item` now delegates to `pos_stock_summaries` (real figures, no fabricated data); new `resolve_menu_item(menu_item_id, service_areas)`; `barcode_lookup_payload` accepts item-or-code and reports `matched_by="menu_item"`. |
| 5 | `backend/apps/pos/serializers.py` | modified | `MenuItemSerializer.stock` (SerializerMethodField) emits the stock summary per item (bulk dict injected into context by the view, per-item fallback); `MenuCategoryMenuSerializer.get_items` forwards serializer context; `BarcodeLookupSerializer.matched_by` choices now include `"menu_item"`. |
| 6 | `backend/apps/pos/views.py` | modified | `PosMenuView.get` computes all menu-item ids and injects one bulk `pos_stock_summaries` call into the serializer context; `PosBarcodeLookupView.get` accepts optional `?menu_item_id=` (same mode/area scoping, 404 for non-sellable) so the page can re-verify a known item live. |
| 7 | `frontend/dashboard/pos.html` | modified | Item picker options show store + availability; live stock status line under the picker; `verifyItemStock()` re-checks a stock-tracked product by `menu_item_id` before it enters the cart (fail-safe: verification failure = item not added); out-of-stock / insufficient / unconfigured / verification-unavailable messages with product, store, quantity, unit; cart lines display their stock source; `+` button respects the available-quantity ceiling; scan path enforces the same rules on the live lookup answer; a 409 `POS_STOCK_CONFLICT` on draft creation refreshes the menu figures and keeps the cart. Room-service wording in room-service mode. No hardcoded store names. |
| 8 | `backend/tests/test_pos_stock_validation.py` | **new** | 31 backend tests: create/submit refusal variants, unconfigured/broken configuration, open-order commitment, draft integrity (nothing partially written), deduction-location consistency (menu says Bar Store → deduction posted at Bar Store), API envelope (409 `POS_STOCK_CONFLICT`), menu/barcode payload shape, restock, cancellation release, sequential + threaded no-oversell. |
| 9 | `frontend/tests-js/test-pos-stock-ui.js` | **new** | 15 static contract tests over `pos.html`: stock-source display, live re-verification via `menu_item_id` on the barcode-lookup endpoint, fail-safe on verification failure, exact refusal messages, double-add guard, cart-line stock display, `+` ceiling, scan enforcement, 409 refresh + cart preservation, no hardcoded store names, unit singular/plural. |
| 10 | `backend/tests/test_inventory_sales_depletion.py` | modified | `InsufficientStockTests` updated from the old policy (accept, park at delivery) to the new one: create-time rejection; shortage arising AFTER creation still parks at delivery; restock sweep still completes the deduction. Other 15 tests untouched. |
| 11 | `backend/tests/test_inventory_item_management.py` | modified | `test_an_invalid_recipe_still_blocks_the_whole_order` now expects the earlier create-time refusal (and still covers the delivery-time evidence path when a recipe breaks after submit); `test_barcode_lookup_still_resolves_the_sku_and_the_order_total_is_unchanged` seeds stock for its sale (the item is stock-tracked now). |

## Line-number summary (original `5aeeb18` → after fix)

| File | Baseline LOC | Fixed LOC | Change regions (original → new) |
|------|-------------|-----------|--------------------------------|
| `backend/apps/core/exceptions.py` | 201 | 227 | insert after o132 → n133–158 `PosStockConflictError` |
| `backend/apps/inventory/services/inventory_service.py` | 1654 | 1885 | append after o868 → n869–1019 `pos_stock_rules`, n1023–1885 `pos_stock_summaries` |
| `backend/apps/pos/services/order_service.py` | 638 | 895 | insert after o131 → n136–374 helpers + `_validate_order_lines_stock`; wire in create_order (o~238 → n483) and submit_order (o~319 → n570–573) |
| `backend/apps/pos/services/barcode_service.py` | 150 | 138 | o11–18 imports → n11–16 (unused `Decimal` removed); o73–138 → n71–90 `stock_summary_for_menu_item` (delegating), n104 `resolve_menu_item`, n119 `barcode_lookup_payload` (item-or-code) |
| `backend/apps/pos/serializers.py` | 645 | 668 | o21–35 → n21–52 `stock` field; o~45 → n67 `matched_by` choices; o~70 → n86–94 `get_items` context forwarding |
| `backend/apps/pos/views.py` | 1142 | 1172 | o208–214 → n208–227 `PosMenuView` bulk summaries; o218–223 → n231–243 context injection; o227–237 → n247–258 `PosBarcodeLookupView` docstring; o268–274 → n289–304 `menu_item_id` parsing + payload branch |
| `frontend/dashboard/pos.html` | 1223 | 1349 | o147 → n150 stock hint element; o362 → n365; o379–393 → n383–432 stock helpers + picker labels; o549–564 → n588–668 `verifyItemStock`/`addCartLine`; o757 → n857 `+` ceiling; o788 → n880–887 409 refresh; o1147–1162 → n1244–1284 `addScannedItem`; o1168 → n1293–1303 `handleScan` |
| `backend/tests/test_pos_stock_validation.py` | — | 706 | new file |
| `frontend/tests-js/test-pos-stock-ui.js` | — | 162 | new file |
| `backend/tests/test_inventory_sales_depletion.py` | 470 | 519 | o317–348 → n317–397 rewritten `InsufficientStockTests` |
| `backend/tests/test_inventory_item_management.py` | 990 | 1019 | o216–236 → n216–263 invalid-recipe test; o742–747 → n769–775 barcode test seeding |

Totals: **844 insertions, 85 deletions** across the 9 tracked files, plus the
2 new test files (868 lines).

## Files explicitly NOT changed

Order/deduction flows that already worked (`transition_order`,
`consume_stock_for_delivered_order`, sweep, returns), the QR guest-request
hand-off that funnels into `create_order`, `frontend/js/config.js` and
`frontend/js/api.js` (the fix reuses the existing `posMenu`,
`posBarcodeLookup`, `posOrders` endpoints unchanged), all CSS (the existing
`.hint`, `.field-error.is-banner` classes are reused), and every subsystem
outside POS/room-service ordering.

## Test status (this exact export)

- Backend, related suite (17 modules: pos, inventory, barcode, error
  contract, service-request hand-off, menu price guard + the new module):
  **186 tests, all OK** (`python manage.py test tests.test_pos …
  tests.test_pos_stock_validation`).
- Backend, full suite: 1820 tests, 26 failures + 4 errors — **all 30 verified
  pre-existing on the untouched baseline** (same results with this fix
  stashed; they live in `test_deployment_readiness`, `test_finance`,
  `test_email_pipeline`, `test_attendance_code_and_visibility` and are
  environment-dependent: celery/redis/SMTP are not installed in the sandbox).
- Frontend JS: **629/632 pass** — the only 3 failures are the pre-existing
  baseline ones (`test-cloudflare-northflank-deployment.js`,
  `test-deploy-config.js`, `test-pwa-dashboard-only.js`), unrelated to POS.
  The 15 new stock-UI tests all pass.
- Test databases: SQLite (development settings). No production system was
  touched.
