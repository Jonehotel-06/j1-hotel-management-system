# Executed tests — Batch 05

Date: 2026-10-30. Source baseline: `0d76219b72b65018ae160b8e20ac586683e0b49c`.
All results below were actually executed in the isolated working copy. No production data was used.

## Final results
| Check | Executed result |
|---|---|
| Backend relevant/API/regression suite | **239 total: 229 passed, 10 skipped, 0 failures/errors**; 91.410 s test runtime |
| Batch 05 test module separately | 29 total: 24 passed, 5 in-memory-SQLite concurrency skips |
| Dedicated file-backed SQLite concurrency runner | **5 passed, 0 skipped**; 1.460 s test runtime |
| Targeted new/POS/portal frontend tests | **67 passed**, no failures/skips |
| Full frontend test suite | **700 passed, 10 failed** (710 total) |
| Untouched baseline full frontend suite | **693 passed, same 10 failed** (703 total) |
| Frontend repository validator | **ALL OK**: 73 HTML files, 33 standalone JavaScript files |
| Django system check | No issues, 0 silenced |
| makemigrations --check --dry-run | No changes detected |
| Python compile/import checks | compileall passed; Django startup/import checks passed |
| Migration execution/preservation | New schema applied by test DB creation; reverse/forward test passed, active/retired recipes, lines and availability preserved |
| git diff --check | Passed |
| Original source clone diff | Empty; source clone untouched |

The broader frontend suite is **not all green**. The exact same ten failing test names were
reproduced in a second untouched baseline copy; no unrelated repairs were made. These were:
- 3. All 72 deployed HTML pages load runtime-config.js before config.js and match version.json
- accounting surfaces supplier goods received separately from posted invoices
- no obsolete production API rewrite remains active in static hosting configs
- Accounting renders the night audit daily flash and checks
- the only web app manifest is scoped to the staff dashboard
- batch 02: export options retain both ISO dates for Excel and PDF
- the hotel's own statutory figure is still reported, not deleted with the fields
- Accounting renders balance sheet and cash flow statements
- every staff table scrolls inside a .table-wrap instead of the page
- accounting From/To fields are never disabled and a chosen date selects the custom range

## Backend command (from backend/)
```sh
python manage.py test \
  tests.test_batch05_food \
  tests.test_pos tests.test_pos_api tests.test_pos_department_api \
  tests.test_pos_menu_management tests.test_pos_restaurant_tables \
  tests.test_pos_room_service_availability tests.test_pos_stock_validation \
  tests.test_pos_tender_balance tests.test_pos_unopened_returns \
  tests.test_service_request_pos_handoff tests.test_guest_portal_room_orders \
  tests.test_portal_access tests.test_inventory tests.test_inventory_item_management \
  --noinput
```

Five skips are the new concurrency cases when the standard Django test database is shared-memory
SQLite. Those five were separately executed successfully by the file-backed runner below.
The other five skips are existing guest-portal MySQL-specific concurrency/isolation cases.
The legacy portal shared-memory concurrency probe also emits expected/handled database-table-locked
and HTTP 500 diagnostics during its race; the runner still exits successfully. This does not establish
production MySQL behavior. Staticfiles-not-collected warnings occurred in the test environment.

## Portion concurrency command (from backend/)
```sh
python tools/test_batch05_concurrency.py
```
Uses a temporary file-backed SQLite database, real threads, independent connections, a start
barrier, IMMEDIATE transactions and WAL. It does not modify the application's database.
Executed cases:
1. Competing orders cannot both reserve more than the free prepared balance.
2. Concurrent duplicate submission reserves once.
3. Concurrent duplicate replenishment adds once.
4. Concurrent duplicate cancellation releases once.
5. Concurrent wastage cannot make the free balance negative.

For MySQL validation, do NOT use that SQLite-specific helper. Configure the normal MySQL test
settings and run:
```sh
python manage.py test tests.test_batch05_food.PreparedFoodConcurrencyTests \
  tests.test_guest_portal_room_orders.ConcurrentPortalOrderTests --noinput
```
This MySQL command is an integration instruction, **not an executed pass**.

## Frontend and checks (from repository root)
```sh
node --test frontend/tests-js/test-batch05-food.js \
  frontend/tests-js/test-pos*.js frontend/tests-js/test-portal*.js
node --test frontend/tests-js/test-*.js
python frontend/validate.py
python backend/manage.py check
python backend/manage.py makemigrations --check --dry-run
python -m compileall -q backend/apps/pos backend/apps/portal/views_catalog.py \
  backend/tests/test_batch05_food.py backend/tests/test_guest_portal_room_orders.py \
  backend/tests/test_pos.py backend/tools/test_batch05_concurrency.py
node --check frontend/js/kitchen-food.js
node --check frontend/js/portal-catalog.js
node --check frontend/js/config.js
git diff --check
```
Node syntax checks were also run explicitly; frontend/validate.py checks script references,
inline JavaScript, HTML structure and local assets using the repository's existing validator.
The untouched frontend baseline was run with the same node test command in a separate baseline copy.

## Batch-specific coverage
- Recipe-free food through draft, acceptance, delivery and guest Room Service catalogue/order.
- Existing enabled COMPONENTS recipe, 250 g → 0.2500 KG conversion, ingredient deduction once.
- Retired recipe preserved; no raw ingredient deduction; explicit NO_STOCK provenance retained.
- Ready/Not Available controls, staff/time audit, old menu switch audit and API permissions.
- Unavailable new drafts rejected; stale draft submission refused without deleting lines.
- Existing accepted order can still be fulfilled after food is marked unavailable.
- Optional per-item portions, duplicate-line aggregation, competing drafts, depleted portions.
- Reservation on acceptance (not draft creation), replay protection and delivery without rededuction.
- Draft cancellation and accepted cancellation; guest cancellation releases portions once.
- Controlled wastage, over-wastage rejection, reserved-food protection and replenishment retries.
- Disabling tracking cannot discard balances or orphan active reservations.
- Raw-stock validation failure rolls back the portion reservation and all new audit writes.
- Returned cooked food does not restore free portions or raw ingredients; return retries are safe;
  existing financial credits and sealed-product return regressions remain covered.
- Mixed recipe/simple guest baskets, POS/guest quantity projections and backend guest limits.
- Constant-query food listing as menu size grows; bulk allocations avoid per-item allocation reads.
- Frontend no-recipe controls, read-only presentation, bounded quantities, central configured URL,
  uncertain-network key reuse, error retention, unlimited guest badge and modifier-preserving refresh.
- Schema roundtrip preserves active/retired recipes and recipe lines, original availability and defaults.

Two pre-existing recipe-mandatory assertions were intentionally updated: simple-food delivery now
records deliberate SKIPPED raw consumption instead of an inventory configuration failure; an explicit
NO_STOCK food recipe may now be ordered in guest Room Service. They are included in the package.

## Not executed / limits
No MySQL server tests, live production calls, external payment integration, physical scanners/printers,
real-browser accessibility audit, visual mobile/desktop viewport checks or manual user acceptance test
was executed. No claim of those passing is made. The existing responsive layout was reused rather
than redesigned. The full repository backend suite beyond the named relevant modules was not run.
