# Batch 05 — Kitchen management, recipes & food availability

## Delivery summary
Implemented only Batch 05 against `Jonehotel-06/j1-hotel-management-system`,
commit `0d76219b72b65018ae160b8e20ac586683e0b49c` (2026-10-30).
The source clone was never edited. Work and baseline tests used separate copies;
no changes were pushed to GitHub. This is a changed-files overlay, not a full repository.

### Completed
- Existing `MenuItem.is_available` remains the canonical Ready / Not Available control.
- Restaurant food can be sold in POS and guest Room Service without a recipe or ingredient setup.
- Configured active recipes, stock links, conversion rules and ingredient deductions remain available.
  Retire a recipe (or use the existing explicit NO_STOCK version) to disable ingredient tracking;
  disabling prepared portions does **not** disable an active recipe.
- Added per-item optional prepared-portion balances, a unique order/item allocation,
  and staff/time-stamped food events. Existing dishes default to portion tracking off.
- Draft creation validates availability and aggregate portions. Submission rechecks,
  then reserves portions atomically. Other drafts do not hold prepared portions.
- Delivery consumes the reservation without another deduction; cancellation releases it once.
- Wastage requires permission, a reason, a positive whole quantity and enough unreserved food.
  Replenishment, wastage and status/configuration changes accept replay-safe keys.
- Cooked food returns use the existing financial return flow but are discarded, never
  resold or converted back into raw inventory. Explicit direct-sale product returns retain
  their existing inventory restocking path. Frozen consumption evidence classifies food returns.
- Existing accepted orders and historical lines are never deleted on availability changes.
- Added a small prepared-food panel beside the existing production queue, not a POS redesign.
  Search, pagination, status, portions, preparation, wastage and recent audit history are included.
- POS uses its existing 15-second live refresh loop; guest menu refreshes silently every
  15 seconds while the document is visible. Selections/modifiers are retained where still valid.
  Failed silent refreshes retain the last successful data; backend validation remains mandatory.

## Files and original locations
`MANIFEST.md` is the exact modified/new file inventory, with original repository-relative
locations and SHA-256 checksums. No inventory-owned file was changed. No unchanged files,
credentials, databases, dependency caches or test fixture data are included.

## Output directory
`ai_updates/batch_05_kitchen_food_availability/`

## Migration requirements
Apply `backend/apps/pos/migrations/0009_prepared_food_availability.py` after POS 0008.
It adds two MenuItem columns, FoodAvailabilityEvent and PreparedPortionAllocation.
There are **no recipe data migrations**, inferred recipes, name/SKU matching, or recipe deletions.
Current availability and all existing active/retired recipe rows remain unchanged.
Existing accepted orders are not retroactively allocated. See the initial-count instructions
in KITCHEN_WORKFLOW.md before enabling portions on an already operating kitchen.

## API changes
All paths below are relative to the existing backend origin, with normal authentication/envelopes.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/pos/kitchen-food/?q=&page=&page_size=` | Paginated active restaurant food controls |
| POST | `/api/admin/pos/kitchen-food/{id}/` | Apply one audited, idempotent food change |
| GET | `/api/admin/pos/kitchen-food/{id}/?page=` | Paginated append-only event history |

POST accepts `action`, `idempotency_key` (required, <=100 characters), `quantity`
(whole number 1..1000000 for quantity actions), and optional `reason` (<=500 characters).
Actions: `READY`, `NOT_AVAILABLE`, `ENABLE_PORTIONS`, `DISABLE_PORTIONS`, `REPLENISH`, `WASTE`.
WASTE requires a reason. Non-quantity actions must not include a nonzero quantity.
Reusing a key with a different action/quantity/reason is rejected. The key is scoped to the food item.

List permissions: any of kitchen.queue.view, kitchen.ticket.manage, pos.menu.manage,
restaurant.menu.manage. Changes/history: any of kitchen.ticket.manage, pos.menu.manage,
restaurant.menu.manage. A bar-only menu capability cannot operate kitchen food controls.
Authentication/permission errors follow the existing API policy; invalid controls, portion
shortages and unavailable food return validation errors (400). Configured raw stock conflicts
retain the existing 409 POS_STOCK_CONFLICT contract; missing records return 404.

Food payload: id, name, is_available, availability_label, portion_tracking,
prepared_portions (unreserved), updated_at; POST also returns changed.
History includes actor_id, time, action, delta, balance, status/configuration snapshots,
reason and order/return evidence. Guest cancellation can have actor_id=null: the order and
existing guest service-request events retain guest attribution.

Existing staff menu serializers add read-only portion_tracking/prepared_portions.
POS/menu and barcode stock payloads overlay prepared limits using the existing stock shape;
raw component details remain in components. The prepared pool is clearly labelled Prepared food.
Guest menu adds portion_tracking and quantity_limited. Unlimited food uses the existing
100-per-line order ceiling, **not** a claim that 100 portions physically exist.
Existing order endpoints, statuses, payment/folio handling and bar stock enforcement remain.

## Tests and results
See TEST_REPORT.md for executed commands, exact counts, baseline failures, skips and limitations.

## Integration instructions
1. Back up code and database; integrate in a branch based on the manifest commit.
2. Compare local changes to the listed original locations; merge conflicts deliberately.
3. Pause order submission/kitchen changes during rollout. Copy the package's backend/ and
   frontend/ trees into the repository root, preserving paths. Do not copy the metadata
   markdown files over unrelated root documents.
4. With the deployment's normal environment/settings active, run:
   `cd backend && python manage.py migrate pos && python manage.py check`.
5. Deploy/restart all backend processes and deploy the matching frontend together.
   New endpoint configuration is in js/config.js; no API host needs editing.
   Updated POS/config and guest-catalog script references have Batch 05 cache-busting queries.
6. Re-run the documented suites on staging, especially MySQL row-lock tests, before production.
7. Open POS → Prepared food availability; verify a Ready recipe-free dish in POS and the guest
   portal. Do not enable portions until all older backend instances have stopped accepting orders.
8. Reconcile pre-existing accepted food obligations, then record only genuinely free prepared
   quantities. Recipes are optional and are managed separately by existing inventory permissions.

Rollback: stop new orders, retain a database backup and food event/allocation tables.
Rolling back application code without reversing 0009 preserves audit history but disables new
portion enforcement. Reverse 0009 only on an unused deployment or restored backup: it drops
new portion/audit tables. The automated reverse/forward check used disposable test data only.

## Known limitations / release gates
- MySQL/MariaDB row-lock and isolation behavior was not executed in this environment.
  Five new threaded tests passed on file-backed SQLite; five existing MySQL-specific tests
  remain skipped. Run those on the production database engine before release.
- Ten unrelated full-frontend-suite failures also occur on the untouched baseline; not repaired
  because accounting/deployment/PWA/global-table redesigns are outside Batch 05.
- No live-production, real-browser viewport/mobile, deployment or external-payment test was run.
  Existing responsive components are reused; DOM/helper tests and static validation were run.
- Ready with zero tracked portions is effectively unavailable for ordering, even if the kitchen
  manually leaves its status Ready. Replenishment does not automatically override Not Available.
- Already accepted pre-migration orders have no retroactive portion allocations. Initial free
  quantities must exclude their obligations. Existing history is not rewritten or guessed.
- Guest drafts can become stale before staff acceptance. They remain visible and are refused
  on submission if food/portions are no longer available; they are not silently removed.
- Room Service delivered financial corrections retain the existing front-desk workflow;
  this batch does not add a new delivered room-service return/refund route. Existing restaurant
  and bar returns are supported; cooked food is never automatically made sellable again.
- Existing immutable ingredient hand-offs and pre-existing pending review work are not rewritten
  when recipes are retired. Inventory-owned historical reconciliation remains inventory-owned.
