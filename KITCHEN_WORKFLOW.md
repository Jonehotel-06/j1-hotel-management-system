# Kitchen workflow

## 1. Simple availability — no recipes needed
Create/maintain food in the existing restaurant menu. Open the existing POS workspace and
use **Prepared food availability** beside the production queue.
- **Mark Ready**: staff may sell this food in POS and Room Service.
- **Mark Not Available**: removes it from new staff selections and disables new guest selections.
  Server-side draft creation and submission also check current availability.
- Leave **No portion tracking** selected for foods such as pepper soup when a binary status
  is sufficient. Do not configure placeholder ingredients or NO_STOCK recipes just to sell it.
- Accepted orders keep their lines, price snapshots, tickets and history, even if status changes.
  Staff may fulfil or cancel them through the existing workflow.

The original menu-management availability switch is still usable and is now audited too.
The new controls use existing kitchen/menu capabilities; an authorized chef need not receive
inventory-management permissions merely to record cooked-food readiness.

## 2. Optional prepared portions
For an individual food, choose **Track portions**, then **Record prepared quantity**.
For example, enter the actual free cooked portions of jollof rice, fried rice or white rice.
There is no sample food or quantity seeded into production.

`prepared_portions` is the **free/unreserved cooked-food balance**, not kilograms, bags,
raw ingredient quantities, or the total physically on the counter including reserved orders.

| Event | Prepared balance |
|---|---|
| Create draft | Validate current free quantity; do not reserve |
| Submit / accept draft | Reserve aggregate item quantities once; reduce free balance |
| Prepare / Ready ticket | No second portion deduction |
| Deliver | Mark reservation consumed; no additional balance deduction |
| Cancel an unaccepted draft | No quantity change |
| Cancel an undelivered accepted order | Release its reservation once |
| Record additional preparation | Add the entered whole quantity once |
| Record wastage | Reduce only free portions; require a reason |
| Return cooked food after delivery | Record discarded quantity; do not add to free balance |

Duplicate menu lines are aggregated. Concurrent acceptance locks menu rows in a stable order.
A competing draft can remain visible but cannot be accepted if another order took the portions.
A stock/portion validation failure rolls the complete acceptance transaction back.

When recording waste affecting a reserved meal, resolve/cancel the corresponding undelivered
order through the normal workflow first, then waste the released free portions. Wastage cannot
silently consume food already promised to an order.

To stop portion tracking: fulfil/cancel reservations and deliberately waste any remaining free
balance with a reason first. The system refuses to silently throw away counts or orphan holds.

**Initial rollout:** accepted orders from before this feature intentionally have no allocations.
Before enabling portions, count only portions free AFTER setting aside food already promised
to those orders. Reconcile with the existing production queue. No retroactive counts are guessed.

## 3. Optional advanced ingredients
Continue using the existing Inventory → Recipes workflow, with its existing permissions.
- An active COMPONENTS recipe enables ingredient-level tracking and the existing delivery-time
  stock deduction. Actual StockItem records and stock locations remain authoritative.
- Enter quantities in supported units: e.g. 250 g is converted to 0.2500 KG for a KG-base item.
  Existing conversion factors, typed-unit evidence and validation remain unchanged.
- Retire a recipe to disable it, or use the existing explicit NO_STOCK recipe version if that is
  the hotel's chosen inventory policy. Never delete historical recipe records.
- Re-enable through the existing versioned recipe-creation service; it retires the old active
  version while retaining all versions and lines.
- A missing/retired recipe does not force food to become unavailable. A broken active mapping
  is not silently ignored: inventory staff should fix or deliberately retire it.
- Portion tracking and ingredient tracking are independent. If both are enabled, both ceilings
  apply. New preparation counts do not deduct raw stock; existing ingredient timing stays delivery.

Recipe-free prepared food receives an explicit non-stock marker in its **order hand-off payload**,
not a generated StockRecipe record. This prevents a future catch-up worker from accidentally
assigning newly configured ingredients to that already delivered simple-food sale. Active explicit
NO_STOCK recipe provenance remains intact; historical hand-offs are not changed.

## 4. Cancellation, returns and audit
Undelivered order cancellation releases reserved food atomically, even while Not Available;
it does not force the item back to Ready. Guests retain the existing permitted cancellation stages.
Delivered orders cannot be cancelled or silently removed.

Existing restaurant/bar financial returns still create their existing credit/refund evidence.
Returned cooked food is audited as RETURN_DISCARD and cannot restore raw rice or other ingredients.
Sealed products explicitly mapped as direct sales keep the existing inventory restock flow.
Do not replenish returned cooked food as though it were a new preparation batch.

**Change history** shows recent events. The history API provides all events through pagination.
Events capture staff, time, status, tracking mode, quantity change and resulting free balance.
Order reservation/release/consumption events carry the order reference; return events carry the
return reference and discarded quantity. Balance deltas for CONSUME and RETURN_DISCARD are zero
because the portion already left the free pool at acceptance.

For API clients, reuse the SAME idempotency key after a timeout. Changing the payload requires a
new key. The UI retains keys for uncertain-network retries during the current page session;
after a browser reload, check history before re-entering an uncertain preparation/wastage action.
