"""Locked, source-keyed stock ledger operations and procurement workflow."""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_today

from ..models import (
    GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine, StockBalance,
    StockConsumptionRequest, StockConsumptionRequestEvent, StockCount, StockCountEvent, StockCountLine,
    StockItem, StockLocation, StockMovement, StockRecipe, StockRecipeLine, Supplier,
)

QTY = Decimal("0.0001")
MONEY = Decimal("0.01")


def _quantity(value, *, field="quantity", positive=False):
    try:
        amount = Decimal(str(value)).quantize(QTY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid quantity to four decimal places."}) from exc
    if positive and amount <= Decimal("0.0000"):
        raise ValidationError({field: "Quantity must be greater than zero."})
    if not positive and amount == Decimal("0.0000"):
        raise ValidationError({field: "Quantity delta cannot be zero."})
    return amount


def _cost(value, *, field="unit_cost"):
    try:
        amount = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid unit cost."}) from exc
    if amount < Decimal("0.00"):
        raise ValidationError({field: "Unit cost cannot be negative."})
    return amount


def _reference(prefix):
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def scoped_key(*, prefix, scope, raw_key):
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        return None
    if len(raw_key) > 128:
        raise ValidationError({"idempotency_key": "Idempotency key must be at most 128 characters."})
    return f"{prefix}:{hashlib.sha256(f'{scope}:{raw_key}'.encode()).hexdigest()}"


def _locked_balance(*, item, location):
    """Serialize a projection row even when this is its first movement."""
    StockItem.objects.select_for_update().get(pk=item.pk)
    StockLocation.objects.select_for_update().get(pk=location.pk)
    balance = StockBalance.objects.select_for_update().filter(item=item, location=location).first()
    if balance:
        return balance
    try:
        with transaction.atomic():
            return StockBalance.objects.create(item=item, location=location)
    except IntegrityError:
        return StockBalance.objects.select_for_update().get(item=item, location=location)


@transaction.atomic
def record_stock_movement(
    *, item, location, movement_type, quantity_delta, source_key, actor=None,
    unit_cost=None, source_reference="", notes="", metadata=None, allow_negative=False,
) -> tuple[StockMovement, bool]:
    """Append an immutable movement and update the locked balance projection."""
    if movement_type not in StockMovement.Type.values:
        raise ValidationError({"type": "Invalid stock movement type."})
    source_key = str(source_key or "").strip()
    if not source_key:
        raise ValidationError("A stable stock movement source key is required.")
    existing = StockMovement.objects.select_for_update().filter(source_key=source_key).first()
    if existing:
        return existing, False
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    location = StockLocation.objects.select_for_update().get(pk=location.pk)
    if not item.is_active:
        raise ValidationError("Inactive stock items cannot receive new movements.")
    if not location.is_active:
        raise ValidationError("Inactive stock locations cannot receive new movements.")
    quantity_delta = _quantity(quantity_delta, field="quantity_delta")
    balance = _locked_balance(item=item, location=location)
    new_quantity = (Decimal(balance.quantity_on_hand) + quantity_delta).quantize(QTY)
    if new_quantity < Decimal("0.0000") and not allow_negative:
        raise ValidationError({"quantity_delta": "Movement would make on-hand stock negative."})
    for _ in range(8):
        try:
            with transaction.atomic():
                movement = StockMovement.objects.create(
                    reference=_reference("STM"), source_key=source_key, item=item, location=location,
                    balance=balance, type=movement_type, quantity_delta=quantity_delta,
                    unit_cost=_cost(unit_cost) if unit_cost is not None else None,
                    currency=item.currency.upper(), source_reference=(source_reference or "")[:120],
                    notes=(notes or "")[:500], metadata=dict(metadata or {}), actor=actor,
                )
            break
        except IntegrityError:
            existing = StockMovement.objects.select_for_update().filter(source_key=source_key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate a unique stock movement reference; please retry.")
    balance.quantity_on_hand = new_quantity
    balance.last_movement_at = movement.occurred_at
    balance.save(update_fields=["quantity_on_hand", "last_movement_at", "updated_at"])
    return movement, True


@transaction.atomic
def issue_stock(*, item, location, quantity, actor, source_key, notes="", source_reference="", movement_type=StockMovement.Type.ISSUE):
    if movement_type not in {StockMovement.Type.ISSUE, StockMovement.Type.MAINTENANCE_ISSUE, StockMovement.Type.WRITE_OFF}:
        raise ValidationError("This issue operation only supports controlled outgoing stock movement types.")
    quantity = _quantity(quantity, positive=True)
    return record_stock_movement(
        item=item, location=location, movement_type=movement_type, quantity_delta=-quantity,
        source_key=source_key, actor=actor, source_reference=source_reference, notes=notes,
    )


@transaction.atomic
def transfer_stock(*, item, from_location, to_location, quantity, actor, transfer_key, notes=""):
    if from_location.pk == to_location.pk:
        raise ValidationError("Stock transfer locations must differ.")
    quantity = _quantity(quantity, positive=True)
    transfer_key = str(transfer_key or "").strip()
    if not transfer_key:
        raise ValidationError("A stable transfer key is required.")
    # Lock locations consistently before both legs; the movement service locks
    # the item/balance rows and source keys make a retry converge.
    locations = sorted([from_location, to_location], key=lambda row: row.pk)
    list(StockLocation.objects.select_for_update().filter(pk__in=[row.pk for row in locations]).order_by("pk"))
    outbound, outbound_created = record_stock_movement(
        item=item, location=from_location, movement_type=StockMovement.Type.TRANSFER_OUT, quantity_delta=-quantity,
        source_key=f"transfer:{transfer_key}:out", actor=actor, source_reference=transfer_key, notes=notes,
    )
    inbound, inbound_created = record_stock_movement(
        item=item, location=to_location, movement_type=StockMovement.Type.TRANSFER_IN, quantity_delta=quantity,
        source_key=f"transfer:{transfer_key}:in", actor=actor, source_reference=transfer_key, notes=notes,
    )
    return (outbound, inbound), outbound_created or inbound_created


def _validate_po_lines(lines):
    values = list(lines or ())
    if not 1 <= len(values) <= 200:
        raise ValidationError({"lines": "A purchase order needs between 1 and 200 lines."})
    item_ids = []
    for raw in values:
        try: item_ids.append(int(raw["item_id"]))
        except (KeyError, TypeError, ValueError) as exc: raise ValidationError({"lines": "Every line needs an item_id."}) from exc
    if len(set(item_ids)) != len(item_ids):
        raise ValidationError({"lines": "An item may appear only once per purchase order."})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        raise ValidationError({"lines": "One or more stock items are inactive or missing."})
    normalized = []
    for raw, item_id in zip(values, item_ids):
        item = items[item_id]
        normalized.append({
            "item": item,
            "quantity_ordered": _quantity(raw.get("quantity_ordered"), field="quantity_ordered", positive=True),
            "unit_cost": _cost(raw.get("unit_cost")),
            "notes": str(raw.get("notes") or "")[:500],
        })
    return normalized


@transaction.atomic
def create_purchase_order(*, supplier, delivery_location, lines, requested_by, expected_on=None, notes="", supplier_reference="", idempotency_key=None):
    if supplier.status != Supplier.Status.ACTIVE:
        raise ValidationError("Purchase orders require an active supplier.")
    if not delivery_location.is_active:
        raise ValidationError("Purchase orders require an active delivery location.")
    idempotency_key = str(idempotency_key or "").strip() or None
    if idempotency_key:
        existing = PurchaseOrder.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing: return existing, False
    normalized = _validate_po_lines(lines)
    for _ in range(8):
        try:
            with transaction.atomic():
                po = PurchaseOrder.objects.create(
                    reference=_reference("PO"), idempotency_key=idempotency_key, supplier=supplier,
                    delivery_location=delivery_location, currency="NGN", requested_by=requested_by,
                    expected_on=expected_on, notes=(notes or ""), supplier_reference=(supplier_reference or "")[:120],
                )
            break
        except IntegrityError:
            if idempotency_key:
                existing = PurchaseOrder.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
                if existing: return existing, False
    else:
        raise ValidationError("Could not allocate a unique purchase-order reference; please retry.")
    PurchaseOrderLine.objects.bulk_create([
        PurchaseOrderLine(purchase_order=po, item=row["item"], item_sku=row["item"].sku, item_name=row["item"].name,
                          quantity_ordered=row["quantity_ordered"], unit_cost=row["unit_cost"], notes=row["notes"])
        for row in normalized
    ])
    return po, True


@transaction.atomic
def submit_purchase_order(*, purchase_order, actor):
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.SUBMITTED: return po
    if po.status != PurchaseOrder.Status.DRAFT: raise ValidationError(f"Purchase order cannot submit from {po.status}.")
    if not PurchaseOrderLine.objects.filter(purchase_order=po).exists(): raise ValidationError("A purchase order needs at least one line.")
    po.status = PurchaseOrder.Status.SUBMITTED; po.save(update_fields=["status", "updated_at"])
    return po


@transaction.atomic
def approve_purchase_order(*, purchase_order, actor):
    if not has_capability(actor, "procurement.approve"):
        raise PermissionDenied("This user cannot approve purchase orders.")
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.APPROVED: return po
    if po.status != PurchaseOrder.Status.SUBMITTED: raise ValidationError(f"Purchase order cannot approve from {po.status}.")
    if po.requested_by_id == actor.pk: raise PermissionDenied("A requester cannot approve their own purchase order.")
    po.status = PurchaseOrder.Status.APPROVED; po.approved_by = actor; po.approved_at = timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return po


@transaction.atomic
def mark_purchase_order_ordered(*, purchase_order, actor):
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.ORDERED: return po
    if po.status != PurchaseOrder.Status.APPROVED: raise ValidationError(f"Purchase order cannot be ordered from {po.status}.")
    po.status = PurchaseOrder.Status.ORDERED; po.ordered_at = timezone.now(); po.save(update_fields=["status", "ordered_at", "updated_at"])
    return po


@transaction.atomic
def receive_goods(*, purchase_order, receipt_lines, actor, source_key, received_on=None, supplier_delivery_reference="", notes=""):
    """Receive bounded PO quantities and append stock receipts exactly once."""
    po = PurchaseOrder.objects.select_for_update().select_related("supplier", "delivery_location").get(pk=purchase_order.pk)
    if po.status not in {PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIALLY_RECEIVED}:
        raise ValidationError(f"Goods cannot be received for purchase order status {po.status}.")
    source_key = str(source_key or "").strip()
    if not source_key: raise ValidationError("A stable receipt source key is required.")
    existing = GoodsReceipt.objects.select_for_update().filter(source_key=source_key).first()
    if existing: return existing, False
    raw_lines = list(receipt_lines or ())
    if not 1 <= len(raw_lines) <= 200: raise ValidationError({"lines": "A receipt needs between 1 and 200 lines."})
    line_ids=[]
    for raw in raw_lines:
        try: line_ids.append(int(raw["purchase_order_line_id"]))
        except (KeyError, TypeError, ValueError) as exc: raise ValidationError({"lines": "Each receipt line needs purchase_order_line_id."}) from exc
    if len(set(line_ids)) != len(line_ids): raise ValidationError({"lines": "A purchase-order line may appear once per receipt."})
    po_lines = {line.pk: line for line in PurchaseOrderLine.objects.select_for_update().select_related("item").filter(purchase_order=po, pk__in=line_ids)}
    if len(po_lines) != len(line_ids): raise ValidationError({"lines": "One or more purchase-order lines do not belong to this order."})
    normalized=[]
    for raw, line_id in zip(raw_lines, line_ids):
        po_line=po_lines[line_id]; qty=_quantity(raw.get("quantity_received"), field="quantity_received", positive=True)
        if Decimal(po_line.quantity_received) + qty > Decimal(po_line.quantity_ordered):
            raise ValidationError({"lines": f"Receipt exceeds remaining quantity for {po_line.item_sku}."})
        unit_cost = _cost(raw.get("unit_cost", po_line.unit_cost))
        normalized.append((po_line, qty, unit_cost))
    for _ in range(8):
        try:
            with transaction.atomic():
                receipt=GoodsReceipt.objects.create(
                    reference=_reference("GRN"), source_key=source_key, purchase_order=po, location=po.delivery_location,
                    supplier=po.supplier, received_on=received_on or hotel_today(), supplier_delivery_reference=(supplier_delivery_reference or "")[:120],
                    received_by=actor, notes=notes or "",
                )
            break
        except IntegrityError:
            existing=GoodsReceipt.objects.select_for_update().filter(source_key=source_key).first()
            if existing: return existing, False
    else: raise ValidationError("Could not allocate a unique goods-receipt reference; please retry.")
    for po_line, qty, unit_cost in normalized:
        movement, _ = record_stock_movement(
            item=po_line.item, location=po.delivery_location, movement_type=StockMovement.Type.RECEIPT,
            quantity_delta=qty, source_key=f"goods-receipt:{receipt.reference}:{po_line.pk}", actor=actor,
            unit_cost=unit_cost, source_reference=receipt.reference, notes=f"Goods receipt {receipt.reference}",
            metadata={"purchase_order": po.reference, "purchase_order_line_id": po_line.pk},
        )
        GoodsReceiptLine.objects.create(goods_receipt=receipt, purchase_order_line=po_line, item=po_line.item,
                                        quantity_received=qty, unit_cost=unit_cost, stock_movement=movement)
        po_line.quantity_received = (Decimal(po_line.quantity_received) + qty).quantize(QTY)
        po_line.save(update_fields=["quantity_received"])
    all_received = not PurchaseOrderLine.objects.filter(purchase_order=po, quantity_received__lt=F("quantity_ordered")).exists()
    # Import locally to keep the hot stock path's imports simple.
    if all_received:
        po.status = PurchaseOrder.Status.RECEIVED
    else:
        po.status = PurchaseOrder.Status.PARTIALLY_RECEIVED
    po.save(update_fields=["status", "updated_at"])
    return receipt, True


def _active_recipe_snapshots(menu_item_ids):
    """Return the current active, serializable recipe mapping for menu IDs.

    The caller freezes this mapping into a hand-off or its resolution evidence;
    it must never rely on a mutable recipe after a stock movement is posted.
    """
    ids = sorted({int(value) for value in menu_item_ids if value is not None})
    if not ids:
        return {}
    recipes = (
        StockRecipe.objects.filter(menu_item_id__in=ids, is_active=True)
        .select_related("location", "menu_item")
        .prefetch_related("lines__item")
        .order_by("menu_item_id", "-version", "-pk")
    )
    snapshots = {}
    for recipe in recipes:
        key = str(recipe.menu_item_id)
        # Service creation guarantees one active recipe; choosing the newest
        # defensively makes an old imported duplicate deterministic.
        if key in snapshots:
            continue
        snapshots[key] = {
            "recipe_reference": recipe.reference,
            "recipe_version": recipe.version,
            "menu_item_id": recipe.menu_item_id,
            "location_id": recipe.location_id,
            "location_code": recipe.location.code,
            "tracking_mode": recipe.tracking_mode,
            "components": [
                {
                    "item_id": line.item_id,
                    "item_sku": line.item.sku,
                    "quantity_per_menu_unit": str(line.quantity_per_menu_unit),
                }
                for line in recipe.lines.all()
            ],
        }
    return snapshots


def _consumption_event(*, request, event_type, actor=None, details=None):
    return StockConsumptionRequestEvent.objects.create(
        request=request, type=event_type, actor=actor, details=dict(details or {}),
    )


@transaction.atomic
def create_stock_recipe(*, menu_item, location, tracking_mode, components, actor, idempotency_key, notes=""):
    """Create a new immutable-version BOM and retire its prior active version."""
    if tracking_mode not in StockRecipe.TrackingMode.values:
        raise ValidationError({"tracking_mode": "Invalid recipe tracking mode."})
    if not location.is_active:
        raise ValidationError("An active stock location is required for a recipe.")
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key:
        raise ValidationError({"idempotency_key": "A recipe idempotency key is required."})
    existing = StockRecipe.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing, False

    # Lock the menu row even when it has no current recipe to serialize version
    # allocation and active-version retirement between concurrent requests.
    from apps.pos.models import MenuItem
    menu_item = MenuItem.objects.select_for_update().get(pk=menu_item.pk)
    raw_components = list(components or ())
    if tracking_mode == StockRecipe.TrackingMode.COMPONENTS and not raw_components:
        raise ValidationError({"components": "A component-tracked recipe needs at least one BOM line."})
    if tracking_mode == StockRecipe.TrackingMode.NO_STOCK and raw_components:
        raise ValidationError({"components": "An explicitly non-stock recipe cannot have BOM lines."})
    if len(raw_components) > 100:
        raise ValidationError({"components": "A recipe may have at most 100 BOM lines."})
    item_ids = []
    for raw in raw_components:
        try:
            item_ids.append(int(raw["item_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError({"components": "Every BOM line needs an item_id."}) from exc
    if len(set(item_ids)) != len(item_ids):
        raise ValidationError({"components": "A stock item may appear once per recipe."})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        raise ValidationError({"components": "One or more stock items are inactive or missing."})
    normalized = [
        (items[item_id], _quantity(raw.get("quantity_per_menu_unit"), field="quantity_per_menu_unit", positive=True))
        for raw, item_id in zip(raw_components, item_ids)
    ]
    prior = list(StockRecipe.objects.select_for_update().filter(menu_item=menu_item).order_by("-version", "-pk"))
    version = (prior[0].version if prior else 0) + 1
    for active in (row for row in prior if row.is_active):
        active.is_active = False; active.retired_at = timezone.now(); active.retired_by = actor
        active.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])
    for _ in range(8):
        try:
            with transaction.atomic():
                recipe = StockRecipe.objects.create(
                    reference=_reference("BOM"), idempotency_key=idempotency_key, menu_item=menu_item,
                    location=location, version=version, tracking_mode=tracking_mode, created_by=actor, notes=notes or "",
                )
            break
        except IntegrityError:
            existing = StockRecipe.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate a unique recipe reference; please retry.")
    StockRecipeLine.objects.bulk_create([
        StockRecipeLine(recipe=recipe, item=item, quantity_per_menu_unit=quantity) for item, quantity in normalized
    ])
    return recipe, True


@transaction.atomic
def retire_stock_recipe(*, recipe, actor):
    recipe = StockRecipe.objects.select_for_update().get(pk=recipe.pk)
    if not recipe.is_active:
        return recipe
    recipe.is_active = False; recipe.retired_at = timezone.now(); recipe.retired_by = actor
    recipe.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])
    return recipe


@transaction.atomic
def request_pos_stock_consumption(*, order, actor=None):
    """Persist an idempotent POS hand-off with its best available BOM snapshot."""
    source_key = f"pos-stock-consumption:{order.reference}"
    existing = StockConsumptionRequest.objects.select_for_update().filter(source_key=source_key).first()
    if existing:
        return existing, False
    lines = list(order.lines.order_by("pk").values(
        "menu_item_id", "item_name", "item_sku", "quantity", "unit_price", "modifier_total", "line_total", "modifiers_snapshot"
    ))
    recipes = _active_recipe_snapshots([line["menu_item_id"] for line in lines])
    for _ in range(8):
        try:
            with transaction.atomic():
                request = StockConsumptionRequest.objects.create(
                    reference=_reference("SCR"), source_key=source_key, pos_order=order,
                    payload={
                        "order_reference": order.reference,
                        "mode": order.mode,
                        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
                        "recipe_snapshot": recipes,
                        "lines": [
                            {
                                **line,
                                "unit_price": str(line["unit_price"]),
                                "modifier_total": str(line["modifier_total"]),
                                "line_total": str(line["line_total"]),
                            }
                            for line in lines
                        ],
                    },
                )
                _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.CREATED, actor=actor,
                                   details={"recipe_snapshot_count": len(recipes), "line_count": len(lines)})
            return request, True
        except IntegrityError:
            existing = StockConsumptionRequest.objects.select_for_update().filter(source_key=source_key).first()
            if existing:
                return existing, False
    raise ValidationError("Could not allocate a POS stock-consumption request; please retry.")


@transaction.atomic
def process_pos_stock_consumption(*, request, actor):
    """Inventory-only, idempotent posting of POS_CONSUMPTION ledger movements.

    A missing/invalid recipe never causes partial deduction: the request is
    marked failed with review evidence and can be retried once configuration is
    corrected. Each aggregate movement has a deterministic request/location/
    item source key, so a retried processing command converges safely.
    """
    request = StockConsumptionRequest.objects.select_for_update().select_related("pos_order").get(pk=request.pk)
    if request.status in {StockConsumptionRequest.Status.PROCESSED, StockConsumptionRequest.Status.SKIPPED}:
        return request, False
    raw_lines = list((request.payload or {}).get("lines") or ())
    if not raw_lines:
        request.status = StockConsumptionRequest.Status.FAILED
        request.resolution_note = "The POS hand-off has no lines to resolve."
        request.save(update_fields=["status", "resolution_note", "updated_at"])
        _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.FAILED, actor=actor, details={"reason": "empty_lines"})
        return request, False
    snapshot_recipes = dict((request.payload or {}).get("recipe_snapshot") or {})
    needed_menu_ids = []
    for raw in raw_lines:
        if raw.get("menu_item_id") is not None:
            try: needed_menu_ids.append(int(raw["menu_item_id"]))
            except (TypeError, ValueError): pass
    fallback_recipes = _active_recipe_snapshots(needed_menu_ids)
    resolved = {}
    missing = []
    aggregates = {}
    for raw in raw_lines:
        try:
            menu_item_id = int(raw["menu_item_id"])
            quantity = _quantity(raw["quantity"], field="POS line quantity", positive=True)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValidationError("The POS hand-off includes an invalid immutable line snapshot.") from exc
        recipe = snapshot_recipes.get(str(menu_item_id)) or fallback_recipes.get(str(menu_item_id))
        if not recipe:
            missing.append(str(raw.get("item_sku") or raw.get("item_name") or menu_item_id))
            continue
        if recipe.get("tracking_mode") == StockRecipe.TrackingMode.NO_STOCK:
            resolved[str(menu_item_id)] = recipe
            continue
        components = list(recipe.get("components") or ())
        if recipe.get("tracking_mode") != StockRecipe.TrackingMode.COMPONENTS or not components:
            missing.append(f"invalid recipe for {raw.get('item_sku') or raw.get('item_name') or menu_item_id}")
            continue
        try:
            location_id = int(recipe["location_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("The frozen recipe location is invalid.") from exc
        resolved[str(menu_item_id)] = recipe
        for component in components:
            try:
                item_id = int(component["item_id"])
                amount = _quantity(component["quantity_per_menu_unit"], field="recipe component", positive=True) * quantity
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise ValidationError("The frozen recipe component is invalid.") from exc
            key = (item_id, location_id)
            aggregates[key] = (aggregates.get(key, Decimal("0.0000")) + amount).quantize(QTY)
    if missing:
        request.status = StockConsumptionRequest.Status.FAILED
        request.resolution_note = "Missing or invalid active recipe for: " + ", ".join(sorted(set(missing)))[:2000]
        request.resolution_payload = {"resolved_recipes": resolved, "missing": sorted(set(missing))}
        request.save(update_fields=["status", "resolution_note", "resolution_payload", "updated_at"])
        _consumption_event(request=request, event_type=StockConsumptionRequestEvent.Type.FAILED, actor=actor,
                           details={"missing": sorted(set(missing))})
        return request, False
    items = {item.pk: item for item in StockItem.objects.filter(pk__in=[key[0] for key in aggregates], is_active=True)}
    locations = {location.pk: location for location in StockLocation.objects.filter(pk__in=[key[1] for key in aggregates], is_active=True)}
    if len(items) != len({key[0] for key in aggregates}) or len(locations) != len({key[1] for key in aggregates}):
        raise ValidationError("A frozen recipe references an inactive or removed stock item/location.")
    for (item_id, location_id), quantity in sorted(aggregates.items()):
        recipe_evidence = [recipe for recipe in resolved.values() if recipe.get("location_id") == location_id and recipe.get("tracking_mode") == StockRecipe.TrackingMode.COMPONENTS]
        record_stock_movement(
            item=items[item_id], location=locations[location_id], movement_type=StockMovement.Type.POS_CONSUMPTION,
            quantity_delta=-quantity, source_key=f"pos-consumption:{request.reference}:{location_id}:{item_id}", actor=actor,
            unit_cost=items[item_id].standard_cost, source_reference=request.pos_order.reference,
            notes=f"POS consumption for {request.pos_order.reference}",
            metadata={"stock_consumption_request": request.reference, "pos_order": request.pos_order.reference,
                      "resolved_recipes": recipe_evidence},
        )
    has_components = bool(aggregates)
    request.status = StockConsumptionRequest.Status.PROCESSED if has_components else StockConsumptionRequest.Status.SKIPPED
    request.processed_by = actor; request.processed_at = timezone.now()
    request.resolution_note = "POS recipe consumption posted." if has_components else "All menu items are explicitly non-stock tracked."
    request.resolution_payload = {"resolved_recipes": resolved, "movement_count": len(aggregates)}
    request.save(update_fields=["status", "processed_by", "processed_at", "resolution_note", "resolution_payload", "updated_at"])
    _consumption_event(request=request,
                       event_type=StockConsumptionRequestEvent.Type.PROCESSED if has_components else StockConsumptionRequestEvent.Type.SKIPPED,
                       actor=actor, details={"movement_count": len(aggregates)})
    return request, True


def _count_event(*, stock_count, event_type, actor=None, details=None):
    return StockCountEvent.objects.create(stock_count=stock_count, type=event_type, actor=actor, details=dict(details or {}))


@transaction.atomic
def start_stock_count(*, location, item_ids, actor, notes=""):
    """Snapshot selected item/location balances for a controlled physical count."""
    if not location.is_active:
        raise ValidationError("Stock count requires an active location.")
    try:
        item_ids = [int(value) for value in item_ids or ()]
    except (TypeError, ValueError) as exc:
        raise ValidationError({"item_ids": "Item identifiers must be integers."}) from exc
    if not 1 <= len(item_ids) <= 500 or len(set(item_ids)) != len(item_ids):
        raise ValidationError({"item_ids": "Provide 1–500 distinct active stock item identifiers."})
    items = {item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids, is_active=True)}
    if len(items) != len(item_ids):
        raise ValidationError({"item_ids": "One or more items are inactive or missing."})
    balances = {
        balance.item_id: balance
        for balance in StockBalance.objects.select_for_update().filter(location=location, item_id__in=item_ids)
    }
    stock_count = StockCount.objects.create(reference=_reference("CNT"), location=location, initiated_by=actor, notes=notes or "")
    StockCountLine.objects.bulk_create([
        StockCountLine(stock_count=stock_count, item=items[item_id], expected_quantity=balances.get(item_id).quantity_on_hand if item_id in balances else Decimal("0.0000"))
        for item_id in item_ids
    ])
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.CREATED, actor=actor, details={"line_count": len(item_ids)})
    return stock_count


@transaction.atomic
def submit_stock_count(*, stock_count, counts, actor):
    """Freeze physical counts; nonzero variances require a separate approver."""
    stock_count = StockCount.objects.select_for_update().get(pk=stock_count.pk)
    if stock_count.status != StockCount.Status.DRAFT:
        if stock_count.status in {StockCount.Status.PENDING_APPROVAL, StockCount.Status.POSTED}:
            return stock_count
        raise ValidationError(f"Stock count cannot submit from {stock_count.status}.")
    raw_counts = list(counts or ())
    lines = {line.pk: line for line in StockCountLine.objects.select_for_update().filter(stock_count=stock_count)}
    if len(raw_counts) != len(lines):
        raise ValidationError({"lines": "A counted quantity is required for every stock-count line."})
    seen = set()
    for raw in raw_counts:
        try: line_id = int(raw["line_id"])
        except (KeyError, TypeError, ValueError) as exc: raise ValidationError({"lines": "Each count needs line_id."}) from exc
        if line_id in seen or line_id not in lines: raise ValidationError({"lines": "Count lines must each appear exactly once."})
        seen.add(line_id)
        try:
            counted = Decimal(str(raw.get("counted_quantity"))).quantize(QTY)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError({"counted_quantity": "A valid counted quantity is required."}) from exc
        if counted < 0:
            raise ValidationError({"counted_quantity": "Counted quantity cannot be negative."})
        line = lines[line_id]
        line.counted_quantity = counted
        line.variance = (counted - Decimal(line.expected_quantity)).quantize(QTY)
        line.save(update_fields=["counted_quantity", "variance"])
    has_variance = StockCountLine.objects.filter(stock_count=stock_count).exclude(variance=Decimal("0.0000")).exists()
    stock_count.status = StockCount.Status.PENDING_APPROVAL if has_variance else StockCount.Status.POSTED
    stock_count.submitted_by = actor; stock_count.submitted_at = timezone.now()
    stock_count.save(update_fields=["status", "submitted_by", "submitted_at", "updated_at"])
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.SUBMITTED, actor=actor, details={"has_variance": has_variance})
    if not has_variance:
        _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.POSTED, actor=actor, details={"movement_count": 0})
    return stock_count


@transaction.atomic
def approve_stock_count(*, stock_count, actor):
    if not has_capability(actor, "inventory.adjust.approve"):
        raise PermissionDenied("This user cannot approve stock-count adjustments.")
    stock_count = StockCount.objects.select_for_update().select_related("location", "initiated_by").get(pk=stock_count.pk)
    if stock_count.status == StockCount.Status.POSTED: return stock_count
    if stock_count.status != StockCount.Status.PENDING_APPROVAL:
        raise ValidationError(f"Stock count cannot approve from {stock_count.status}.")
    if stock_count.initiated_by_id == actor.pk:
        raise PermissionDenied("The stock-count initiator cannot approve its own variance adjustments.")
    lines = list(StockCountLine.objects.select_for_update().select_related("item").filter(stock_count=stock_count))
    for line in lines:
        if line.counted_quantity is None or line.variance is None:
            raise ValidationError("All stock-count lines must be submitted before approval.")
        if line.variance == Decimal("0.0000"):
            continue
        balance = _locked_balance(item=line.item, location=stock_count.location)
        if Decimal(balance.quantity_on_hand) != Decimal(line.expected_quantity):
            raise ValidationError("Stock changed since this count began; start a new count instead of applying a stale variance.")
        movement, _ = record_stock_movement(
            item=line.item, location=stock_count.location, movement_type=StockMovement.Type.ADJUSTMENT,
            quantity_delta=line.variance, source_key=f"stock-count:{stock_count.reference}:{line.pk}", actor=actor,
            source_reference=stock_count.reference, notes="Approved stock-count variance",
            metadata={"stock_count_id": stock_count.pk, "stock_count_line_id": line.pk, "expected": str(line.expected_quantity), "counted": str(line.counted_quantity)},
        )
        line.adjustment_movement = movement
        line.save(update_fields=["adjustment_movement"])
    stock_count.status = StockCount.Status.POSTED; stock_count.approved_by = actor; stock_count.approved_at = timezone.now()
    stock_count.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.APPROVED, actor=actor)
    _count_event(stock_count=stock_count, event_type=StockCountEvent.Type.POSTED, actor=actor, details={"movement_count": sum(1 for line in lines if line.variance)})
    return stock_count
