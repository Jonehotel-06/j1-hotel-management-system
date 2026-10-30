"""Prepared FOOD only; never writes raw inventory, recipes, money or order lines.

Menu rows are the serialization boundary. Call order helpers inside the order
transaction, AFTER locking the order, and acquire menu locks in primary-key order.
Drafts check but do not reserve portions; submission is the acceptance boundary.
"""
from collections import Counter

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import FoodAvailabilityEvent, MenuCategory, MenuItem, PreparedPortionAllocation


def food_payload(item):
    return {
        "id": item.pk, "name": item.name, "is_available": item.is_available,
        "availability_label": "Ready" if item.is_available else "Not Available",
        "portion_tracking": item.portion_tracking,
        "prepared_portions": item.prepared_portions,
        "updated_at": item.updated_at,
    }


def _event(item, *, action, actor, delta=0, source_key=None, reason="", details=None):
    return FoodAvailabilityEvent.objects.create(
        menu_item=item, action=action, actor=actor, quantity_delta=delta,
        portions_after=item.prepared_portions, ready_after=item.is_available,
        tracking_after=item.portion_tracking, source_key=source_key,
        reason=reason, details=details or {},
    )


@transaction.atomic
def change_food(*, item_id, action, actor, quantity=0, reason="", idempotency_key):
    item = MenuItem.objects.select_for_update().select_related("category").get(pk=item_id)
    if item.category.service_area != MenuCategory.ServiceArea.RESTAURANT:
        raise ValidationError("Prepared food controls apply only to restaurant food items.")
    source = f"food-control:{item.pk}:{idempotency_key}"
    signature = {"action": action, "quantity": quantity, "reason": reason}
    existing = FoodAvailabilityEvent.objects.filter(source_key=source).first()
    if existing:
        if existing.details != signature:
            raise ValidationError({"idempotency_key": "This key was used for a different food change."})
        return item, False
    delta = 0
    if action in {"READY", "NOT_AVAILABLE"}:
        item.is_available = action == "READY"
    elif action == "ENABLE_PORTIONS":
        item.portion_tracking = True
    elif action == "DISABLE_PORTIONS":
        if PreparedPortionAllocation.objects.filter(menu_item=item, state="RESERVED").exists():
            raise ValidationError("Cancel or deliver reserved orders before disabling portion tracking.")
        if item.prepared_portions:
            raise ValidationError("Record remaining portions as wastage before disabling portion tracking.")
        item.portion_tracking = False
    elif action in {"REPLENISH", "WASTE"}:
        if not item.portion_tracking:
            raise ValidationError("Enable portion tracking for this food first.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 1000000:
            raise ValidationError({"quantity": "Enter a whole number from 1 to 1000000."})
        if action == "WASTE" and not reason.strip():
            raise ValidationError({"reason": "A wastage reason is required."})
        delta = quantity if action == "REPLENISH" else -quantity
        if item.prepared_portions + delta < 0:
            raise ValidationError("Wastage cannot exceed unreserved prepared portions.")
        if item.prepared_portions + delta > 2147483647:
            raise ValidationError("Prepared portion balance is too large.")
        item.prepared_portions += delta
    else:
        raise ValidationError({"action": "Unknown prepared-food action."})
    item.save(update_fields=["is_available", "portion_tracking", "prepared_portions", "updated_at"])
    _event(item, action=action, actor=actor, delta=delta, source_key=source, reason=reason, details=signature)
    return item, True


def validate_food_lines(lines, *, order=None, actor=None, reserve=False):
    """Aggregate duplicate lines and lock all menu rows before checking quantities."""
    totals = Counter()
    for line in lines:
        menu_id = line.get("menu_item_id") or getattr(line.get("menu_item"), "pk", None)
        if menu_id:
            totals[int(menu_id)] += int(line["quantity"])
    items = list(MenuItem.objects.select_for_update().select_related("category").filter(
        pk__in=totals,
    ).order_by("pk"))
    allocations = []
    if reserve and PreparedPortionAllocation.objects.filter(order=order).exists():
        raise ValidationError("Prepared portions were already allocated to this draft.")
    for item in items:
        if not item.is_active or not item.is_available or not item.category.is_active:
            raise ValidationError({"lines": f"{item.name} is Not Available. Refresh the menu before submitting."})
        if item.category.service_area != MenuCategory.ServiceArea.RESTAURANT or not item.portion_tracking:
            continue
        quantity = totals[item.pk]
        if quantity > item.prepared_portions:
            raise ValidationError({"lines": f"{item.name}: only {item.prepared_portions} prepared portions available; {quantity} requested."})
        if reserve:
            allocations.append(PreparedPortionAllocation(order=order, menu_item=item, quantity=quantity))
            item.prepared_portions -= quantity
            item.save(update_fields=["prepared_portions", "updated_at"])
            _event(item, action="RESERVE", actor=actor, delta=-quantity,
                   source_key=f"food-reserve:{order.pk}:{item.pk}", details={"order": order.reference})
    if allocations:
        PreparedPortionAllocation.objects.bulk_create(allocations)


def finish_portions(*, order, actor, cancelled=False):
    allocations = list(PreparedPortionAllocation.objects.filter(order=order, state="RESERVED").order_by("menu_item_id"))
    items = {item.pk: item for item in MenuItem.objects.select_for_update().filter(
        pk__in=[row.menu_item_id for row in allocations],
    ).order_by("pk")}
    for allocation in allocations:
        item = items[allocation.menu_item_id]
        if cancelled:
            item.prepared_portions += allocation.quantity
            item.save(update_fields=["prepared_portions", "updated_at"])
        allocation.state = "RELEASED" if cancelled else "CONSUMED"
        allocation.save(update_fields=["state"])
        _event(item, action="RELEASE" if cancelled else "CONSUME", actor=actor,
               delta=allocation.quantity if cancelled else 0,
               source_key=f"food-finish:{order.pk}:{item.pk}", details={"order": order.reference})


def prepared_stock_summaries(items, summaries):
    """Overlay prepared portion ceilings without changing inventory-owned projections."""
    items = list(items)
    food_ids = set(MenuItem.objects.filter(pk__in=[item.pk for item in items], category__service_area="RESTAURANT").values_list("pk", flat=True))
    for item in items:
        summary = dict(summaries.get(item.pk) or {})
        if item.pk in food_ids and not summary.get("tracked") and not summary.get("unconfigured"):
            summary.update({"prepared_food": True, "no_stock": True})
            summaries[item.pk] = summary
        if not item.portion_tracking:
            continue
        raw_available = summary.get("available_quantity") if summary.get("tracked") else None
        available = min(item.prepared_portions, int(raw_available)) if raw_available is not None else item.prepared_portions
        summary.update({
            "prepared_portions": item.prepared_portions, "portion_tracking": True,
            "tracked": True, "available_quantity": str(available),
            "saleable_quantity": str(available), "available_sufficient_for_one": available > 0,
            "sufficient_for_one": available > 0, "availability_unit": "portion",
            "stock_unit": "portion", "location_name": "Prepared food",
        })
        summaries[item.pk] = summary
    return summaries


def prepare_nonstock_food_handoff(*, order, actor):
    """Freeze deliberate no-recipe food as NO_STOCK in the hand-off, not as recipes.

    Uses the existing inventory hand-off contract. This prevents the catch-up
    worker from later deducting ingredients for a simple prepared-food sale.
    Invalid configured mappings are NOT masked; inventory remains authoritative.
    """
    from apps.inventory.services.inventory_service import pos_stock_rules, request_pos_stock_consumption
    food_ids = list(order.lines.filter(menu_item__category__service_area="RESTAURANT").values_list("menu_item_id", flat=True))
    rules = pos_stock_rules(food_ids)
    simple = [mid for mid, rule in rules.items() if not rule["tracked"] and not rule["unconfigured"] and not rule["no_stock"]]
    if not simple:
        return
    request, created = request_pos_stock_consumption(order=order, actor=actor)
    if not created:
        return
    payload = dict(request.payload)
    snapshots = dict(payload.get("recipe_snapshot") or {})
    for menu_id in simple:
        snapshots[str(menu_id)] = {"tracking_mode": "NO_STOCK", "components": [], "prepared_food": True}
    payload["recipe_snapshot"] = snapshots
    request.payload = payload
    request.save(update_fields=["payload", "updated_at"])


def record_prepared_returns(*, order, planned, actor, return_reference):
    """Discard cooked-food returns; never resell them or restore raw ingredients.

    Explicit direct-sale products keep the existing unopened-stock return path.
    Caller owns the order lock, return transaction and return idempotency.
    """
    from apps.inventory.services.inventory_service import pos_stock_rules
    ids = [row["line"].menu_item_id for row in planned if row["line"].menu_item_id]
    rules = pos_stock_rules(ids)
    from apps.inventory.models import StockConsumptionRequest
    payload = StockConsumptionRequest.objects.filter(pos_order=order).values_list("payload", flat=True).first() or {}
    snapshots = payload.get("recipe_snapshot") or {}
    def is_direct_sale(menu_id):
        snapshot = snapshots.get(str(menu_id))
        # A cooked-food return must not be reclassified by a later mapping edit.
        return bool(snapshot.get("direct_sale")) if snapshot is not None else rules.get(menu_id, {}).get("source") == "direct_sale"
    food = {item.pk: item for item in MenuItem.objects.select_for_update().filter(
        pk__in=ids, category__service_area="RESTAURANT",
    ).order_by("pk") if not is_direct_sale(item.pk)}
    discarded = set()
    for row in planned:
        line = row["line"]
        if line.menu_item_id in food:
            discarded.add(line.pk)
            _event(food[line.menu_item_id], action="RETURN_DISCARD", actor=actor,
                   source_key=f"food-return:{return_reference}:{line.pk}",
                   reason="Returned cooked food is not put back into available portions.",
                   details={"order": order.reference, "return": return_reference, "quantity_discarded": row["quantity"]})
    return discarded
