# Dependencies and module boundaries

## Runtime
No new package, service, queue, websocket, cache server or external API is introduced.
Use the existing backend/requirements.txt and existing static frontend deployment.
Implementation uses Django transactions, row locks, database constraints, Django REST Framework,
standard Python libraries, the central frontend API/config layer, existing modal/pagination/
capability components, and the existing production queue.

Test environment: Python 3.13, Django 5.2.18, Django REST Framework 3.18.3,
Node.js 20.20.2, SQLite. The supplied requirements already permit these package versions.
MySQL driver/server were not installed or tested. No requirements or lockfile was changed.

## Exact inventory-owned dependency (no inventory files modified)
Existing `apps.inventory.services.inventory_service` contracts:
- `pos_stock_rules`: resolves only explicitly configured active recipes/direct-sale links.
- `pos_stock_summaries`: existing raw inventory availability and open-order demand.
- `request_pos_stock_consumption`: creates the existing source-keyed delivery hand-off.
- `consume_stock_for_delivered_order`: existing delivery-time ingredient deduction/idempotency.
- `restock_returned_pos_items`: unchanged path for explicit direct-sale product returns.
- Existing recipe create/retire APIs and quantity conversion services remain the only recipe lifecycle.

Existing `StockConsumptionRequest.payload.recipe_snapshot` accepts snapshots keyed by string menu ID,
with `tracking_mode=NO_STOCK` and empty components. The POS integration uses that existing contract
ONLY for newly created recipe-free prepared-food hand-offs. It does not manufacture recipe database
records, mutate posted stock movements, rewrite previous hand-offs, or infer recipes from names/SKUs.
An active NO_STOCK recipe already has its own snapshot and is not replaced.

Prepared balances/allocations/audit events are POS-owned and never raw StockBalance rows.
Returned cooked-food lines bypass inventory restocking; existing financial return records/credits/
refund permissions remain unchanged. Historical consumption snapshots distinguish direct-sale returns.

If another batch changes the above payload schema, mapping resolution, deduction event or return
contract, reconcile that integration with the inventory owner. No independent inventory schema,
valuation, procurement, raw-stock balance or accounting algorithm is included in this package.

## Ordering and portal dependency
POS create/submit/transition remain the single authoritative workflow for staff and guest orders.
The guest service-request cancellation flow already delegates to POS transitions, so it receives
portion release without modifying the guest-services module. Existing stay/folio eligibility,
portal authentication, service routing, pricing, payment/ledger posting and bar stock rules stay intact.
The only portal backend change is its menu availability projection.

## Migration
POS 0009 depends on POS 0008 and the existing swappable user model. No inventory migration dependency
is added, because no inventory data migration or schema change is performed. Deploy the backend
schema before enabling the frontend controls. All application instances must use the new submission
checks before portion tracking is enabled.

## Production validation dependency
Run the five threaded prepared-food tests plus existing portal concurrency/isolation tests against
a disposable MySQL test database with the deployed isolation policy. The supplied SQLite runner is
for deterministic local concurrency checks and is not a substitute for production row-lock testing.
