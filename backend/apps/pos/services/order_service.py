"""Transactional POS and room-service workflow services."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction, Folio, FolioPosting
from apps.guest_services.models import ServiceRequest, ServiceRequestEvent
from apps.guest_services.services.request_service import service_request_queryset_for_staff
from apps.finance.services import accounting
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.references import generate_finance_reference
from apps.finance.services.tax_policy import customer_tax_amount, statutory_ledger_lines
from apps.stays.models import Stay

from ..models import KitchenTicket, MenuCategory, MenuItem, MenuModifier, PosOrder, PosOrderEvent, PosOrderLine, PosTender
from .table_service import lock_open_table_session, record_order_added

CENT = Decimal("0.01")
MAX_ORDER_LINES = 100
MAX_LINE_QUANTITY = 100


def _normalise_table_label(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _money(value, *, field="amount", positive=False) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid money value."}) from exc
    if positive and amount <= Decimal("0.00"):
        raise ValidationError({field: "Value must be greater than zero."})
    return amount


def _new_reference(prefix, model, *, field="reference") -> str:
    for _ in range(8):
        reference = generate_finance_reference(prefix)
        if not model.objects.filter(**{field: reference}).exists():
            return reference
    raise RuntimeError(f"Could not allocate a unique {prefix} reference.")


def _snapshot_line(*, raw_line: Mapping, currency: str, service_areas=None) -> dict:
    try:
        menu_item_id = int(raw_line.get("menu_item_id"))
        quantity = int(raw_line.get("quantity"))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"lines": "Each line needs an integer menu_item_id and quantity."}) from exc
    if not 1 <= quantity <= MAX_LINE_QUANTITY:
        raise ValidationError({"lines": f"Line quantity must be between 1 and {MAX_LINE_QUANTITY}."})
    menu_item = (
        MenuItem.objects.select_for_update()
        .select_related("category")
        .filter(pk=menu_item_id, is_active=True, is_available=True, category__is_active=True)
        .first()
    )
    if menu_item is None:
        raise ValidationError({"lines": "One or more menu items are not currently available."})
    if service_areas is not None and menu_item.category.service_area not in service_areas:
        raise ValidationError({"lines": "One or more menu items are outside this order's service area."})
    if menu_item.currency.upper() != currency.upper():
        raise ValidationError({"lines": "All menu items must use the order currency."})

    modifier_ids = raw_line.get("modifier_ids") or []
    if not isinstance(modifier_ids, (list, tuple)) or len(modifier_ids) > 20:
        raise ValidationError({"lines": "modifier_ids must be a list of at most 20 choices."})
    try:
        modifier_ids = [int(value) for value in modifier_ids]
    except (TypeError, ValueError) as exc:
        raise ValidationError({"lines": "Modifier identifiers must be integers."}) from exc
    if len(set(modifier_ids)) != len(modifier_ids):
        raise ValidationError({"lines": "A modifier may only be selected once per line."})
    modifiers = list(
        MenuModifier.objects.select_for_update()
        .filter(pk__in=modifier_ids, menu_item=menu_item, is_active=True)
        .order_by("pk")
    )
    if len(modifiers) != len(modifier_ids):
        raise ValidationError({"lines": "One or more selected modifiers are not available for this item."})
    modifier_total = sum((_money(modifier.price_delta) for modifier in modifiers), Decimal("0.00"))
    unit_price = _money(menu_item.base_price, positive=True) + modifier_total
    if unit_price <= Decimal("0.00"):
        raise ValidationError({"lines": "The configured item/modifier price must be positive."})
    line_subtotal = (unit_price * quantity).quantize(CENT)
    # CUSTOMER-BILLING TAX REMOVAL (owner policy 2026-10-11): POS lines never
    # carry an appended tax, regardless of any legacy MenuItem.tax_rate_percent
    # value. The approved selling price is the price the guest pays. The
    # hotel's statutory exposure is accrued separately and invisibly to guests
    # (apps.finance.services.tax_policy). Historical order lines keep their
    # stored tax snapshots untouched.
    tax_amount = customer_tax_amount()
    return {
        "menu_item": menu_item,
        "item_name": menu_item.name,
        "item_sku": menu_item.sku,
        "unit_price": unit_price,
        "quantity": quantity,
        "modifier_total": modifier_total,
        "modifiers_snapshot": [
            {"id": modifier.pk, "name": modifier.name, "price_delta": str(_money(modifier.price_delta))}
            for modifier in modifiers
        ],
        "line_total": line_subtotal,
        "tax_amount": tax_amount,
        "notes": str(raw_line.get("notes") or "")[:500],
    }


def _room_service_context(*, stay, folio=None, actor=None):
    stay = Stay.objects.select_for_update().select_related("booking", "guest").get(pk=stay.pk)
    if stay.status != Stay.Status.IN_HOUSE:
        raise ValidationError("Room-service orders require an in-house stay.")
    main_folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=actor)
    if folio is not None:
        folio = Folio.objects.select_for_update().get(pk=folio.pk)
        if folio.pk != main_folio.pk:
            raise ValidationError("This room-service order must use the stay's active main folio.")
    if main_folio.status != Folio.Status.OPEN:
        raise ValidationError("Room-service orders require an open guest folio.")
    return stay, main_folio


#: Orders that will still consume stock when they are delivered. Delivered
#: orders already did (the on-hand balance reflects their deduction) and
#: cancelled/voided ones never will, so only these statuses count as an open
#: commitment against the same physical stock.
_STOCK_COMMITTED_STATUSES = (
    PosOrder.Status.DRAFT, PosOrder.Status.SUBMITTED, PosOrder.Status.PREPARING, PosOrder.Status.READY,
)


def _display_unit(unit, quantity) -> str:
    """Human singular/plural form of a stock base unit for staff messages."""
    token = str(unit or "").strip().lower() or "unit"
    if token.endswith("(s)"):
        token = token[:-3]
    if token.endswith("s"):
        token = token[:-1] if int(quantity) == 1 else token
        return token
    return token if int(quantity) == 1 else token + "s"


def _qty_text(value) -> str:
    """Exact decimal quantity without trailing zeros or exponent notation."""
    amount = Decimal(value).quantize(Decimal("0.0001")).normalize()
    if amount.as_tuple().exponent > 0:
        amount = amount.quantize(Decimal("1"))
    return str(amount)


def _stock_conflict_context(reason, items):
    return {"reason": reason, "items": items}


def _validate_order_lines_stock(lines, *, exclude_order=None):
    """Authoritative pre-acceptance stock validation for POS order lines.

    Runs INSIDE the caller's transaction for both draft creation and draft
    submission, and rejects — before any line is saved or advanced — a
    stock-tracked product that cannot be fulfilled from the exact store the
    delivery-time deduction will draw from.

    Resolution uses ``pos_stock_rules`` (inventory service), which is built on
    the SAME recipe/direct-sale mapping ``consume_stock_for_delivered_order``
    uses, so the store named in the refusal is the store the sale would deduct.

    Rules, matching the repository's deliberate inventory design:

    * only stock-TRACKED menu items are validated (active COMPONENTS recipe or
      an explicit direct-sale configuration). Explicitly non-stock recipes and
      entirely unconfigured products keep their existing behaviour — POS stock
      rules are not imposed on them;
    * a product that IS claimed for stock tracking but whose mapping is broken
      (inactive stock item, missing/inactive store, zero per-sale quantity,
      recipe without components) is rejected instead of silently passing;
    * required base units are aggregated per (stock item, location) across all
      lines of this order — including different menu items that draw from the
      same physical stock — and compared against the locked balance;
    * quantities already committed by OTHER open (non-delivered, non-cancelled)
      orders reduce availability, so two orders cannot collectively accept
      more than the store holds. This is a best-effort soft commitment inside
      the existing architecture (no reservation tables): it is released
      automatically when an order is cancelled, and the delivery-time
      all-or-nothing shortage check remains the final authority that can never
      drive a balance negative.

    Raises :class:`~apps.core.exceptions.PosStockConflictError` (HTTP 409,
    machine code ``POS_STOCK_CONFLICT``) with the product, store, and actual
    quantities in ``errors``.
    """
    from apps.core.exceptions import PosStockConflictError
    from apps.inventory.models import StockItem as _StockItem, StockLocation as _StockLocation
    from apps.inventory.services.inventory_service import _locked_balance, open_pos_order_demand, pos_stock_rules

    normalised = []
    for line in lines or ():
        menu_item = line.get("menu_item") if hasattr(line, "get") else None
        menu_item_id = getattr(menu_item, "pk", None) or line.get("menu_item_id")
        if menu_item_id is None:
            continue  # legacy free-text line: never resolvable to stock, never blocked here
        try:
            quantity = int(line.get("quantity"))
        except (TypeError, ValueError):
            continue
        normalised.append((int(menu_item_id), quantity, str(line.get("item_name") or "")))
    if not normalised:
        return

    rules = pos_stock_rules({row[0] for row in normalised})
    names = {}
    unconfigured = []
    for menu_item_id, quantity, item_name in normalised:
        rule = rules.get(menu_item_id)
        if rule is None:
            continue
        names[menu_item_id] = item_name
        if rule["unconfigured"]:
            unconfigured.append((menu_item_id, item_name, rule["unconfigured_reason"]))
    if unconfigured:
        menu_item_id, item_name, reason = unconfigured[0]
        message = (
            f"Stock location unavailable for {item_name or 'this product'}: {reason}. "
            "This item cannot be added to the order until its inventory configuration is corrected."
        )
        extra = "".join(f" {name} is also not configured for stock." for _, name, _ in unconfigured[1:])
        raise PosStockConflictError(
            message + extra,
            context=_stock_conflict_context(
                "stock_not_configured",
                [{"menu_item_id": mid, "item_name": name, "reason": why} for mid, name, why in unconfigured],
            ),
        )

    # Required base units per (stock item, store), and which menu items feed it.
    required = {}
    contributors = {}
    for menu_item_id, quantity, item_name in normalised:
        rule = rules.get(menu_item_id)
        if not rule or not rule["tracked"]:
            continue
        names[menu_item_id] = item_name or names.get(menu_item_id, "")
        for component in rule["components"]:
            key = (component["item_id"], rule["location_id"])
            amount = (component["quantity_per_menu_unit"] * quantity).quantize(Decimal("0.0001"))
            required[key] = (required.get(key, Decimal("0.0000")) + amount).quantize(Decimal("0.0001"))
            contributors.setdefault(key, []).append((menu_item_id, quantity, component))
    if not required:
        return

    # Stock already committed by other open orders (resolved through the same
    # rules so shared physical stock is counted). The query runs while the
    # balance locks are held, so a draft committed just before this order's
    # locks were taken is already visible to it.
    # Lock the balances in the same sorted per-(item, location) order the
    # delivery-time deduction uses, so validation and deduction serialise on
    # identical row sequences and can never deadlock against each other.
    balances = {}
    for key in sorted(required):
        item_id, location_id = key
        balance = _locked_balance(item=_StockItem(pk=item_id), location=_StockLocation(pk=location_id))
        balances[key] = Decimal(balance.quantity_on_hand)

    # The "stock other open orders already promised" rule lives in ONE place:
    # the inventory service's shared helper, which the POS menu availability
    # projection also uses. Keeping the two identical is what makes a displayed
    # quantity agree with what this validator will accept. It runs while the
    # balance locks above are held, so a draft committed a moment earlier is
    # already visible here.
    demand = open_pos_order_demand(required, exclude_order=exclude_order)

    shortages = []
    for key in sorted(required):
        item_id, location_id = key
        on_hand = balances.get(key, Decimal("0.0000"))
        needed = required[key]
        held = demand.get(key, Decimal("0.0000"))
        available = on_hand - held
        if available >= needed:
            continue
        shortages.append((key, on_hand, held, available, needed, list(contributors.get(key, []))))
    if not shortages:
        return

    messages = []
    conflict_items = []
    for key, on_hand, held, available, needed, lines_in in shortages:
        # Group the contributing lines by product so repeated lines of one
        # item report one precise, product-specific message.
        by_product = {}
        for menu_item_id, quantity, component in lines_in:
            grouped = by_product.setdefault(menu_item_id, [menu_item_id, 0, component])
            grouped[1] += quantity
        menu_item_id, total_quantity, component = sorted(by_product.values(), key=lambda row: -row[1])[0]
        rule = rules.get(menu_item_id, {})
        location_name = rule.get("location_name") or rule.get("location_code") or "its stock location"
        item_name = names.get(menu_item_id) or component.get("item_name") or "This product"
        unit = component.get("unit") or ""
        per_unit = component["quantity_per_menu_unit"]
        conflict_items.append({
            "menu_item_id": menu_item_id,
            "item_name": item_name,
            "item_sku": component.get("item_sku", ""),
            "location": location_name,
            "on_hand": _qty_text(on_hand),
            "committed_to_open_orders": _qty_text(held),
            "available": _qty_text(max(available, Decimal("0.0000"))),
            "requested": _qty_text(needed),
            "unit": _display_unit(unit, Decimal("1")),
        })
        if len(by_product) == 1 and len(rule.get("components") or []) == 1:
            available_unit_word = _display_unit(unit, max(available, Decimal("1")))
            if on_hand <= Decimal("0.0000"):
                messages.append(
                    f"Out of stock: {item_name} is unavailable at {location_name}. "
                    f"Available stock: 0 {available_unit_word}. This item cannot be added to the order."
                )
            elif available <= Decimal("0.0000"):
                messages.append(
                    f"Insufficient stock: all {_qty_text(on_hand)} {available_unit_word} of {item_name} "
                    f"at {location_name} are already held by other open orders. This item cannot be added "
                    "until those orders are delivered or cancelled."
                )
            else:
                requested_unit_word = _display_unit(unit, needed)
                requested_text = (
                    f"{_qty_text(needed)} {requested_unit_word}"
                    if per_unit == Decimal("1")
                    else f"{total_quantity} × {item_name} ({_qty_text(needed)} {requested_unit_word})"
                )
                held_note = (
                    f" ({_qty_text(held)} {available_unit_word} are already held by other open orders)"
                    if held > Decimal("0.0000") else ""
                )
                messages.append(
                    f"Insufficient stock: only {_qty_text(available)} {available_unit_word} of {item_name} "
                    f"are available at {location_name}{held_note}, but {requested_text} were requested. "
                    "Reduce the quantity or replenish stock before adding this item."
                )
        else:
            # A prepared food is limited by an INGREDIENT, so the refusal must
            # name both the ingredient that ran short and the dish(es) that
            # asked for it — otherwise an operator has no way to know which
            # quantity to reduce.
            dish_labels = []
            for menu_item_id_in, _quantity_in, _component_in in lines_in:
                dish_label = names.get(menu_item_id_in) or ""
                if dish_label and dish_label not in dish_labels:
                    dish_labels.append(dish_label)
            dish_note = (" for " + ", ".join(dish_labels)) if dish_labels else ""
            unit_word = _display_unit(component.get("unit"), Decimal("2"))
            messages.append(
                f"Insufficient stock: {location_name} has {_qty_text(on_hand)} {unit_word} of "
                f"{component.get('item_name') or component.get('item_sku')} on hand"
                + (f" ({_qty_text(held)} {unit_word} already held by other open orders)" if held > Decimal("0.0000") else "")
                + f", but this order needs {_qty_text(needed)} {unit_word}{dish_note}. "
                "Reduce the quantities or replenish stock before adding these items."
            )
    raise PosStockConflictError(
        " ".join(messages),
        context=_stock_conflict_context("insufficient_stock", conflict_items),
    )


@transaction.atomic
def create_order(
    *,
    mode: str,
    lines: Iterable[Mapping],
    actor,
    stay=None,
    folio=None,
    guest_name="",
    table_number="",
    table_session_reference="",
    delivery_location="",
    notes="",
    idempotency_key=None,
    service_request=None,
) -> tuple[PosOrder, bool]:
    """Create a price-snapshotted draft. Client totals are never accepted."""
    if mode not in PosOrder.Mode.values:
        raise ValidationError({"mode": "Invalid POS order mode."})
    idempotency_key = (idempotency_key or "").strip() or None
    qr_request = None
    if service_request is not None:
        qr_request = (
            ServiceRequest.objects.select_for_update()
            .select_related("qr_link")
            .filter(pk=service_request.pk)
            .first()
        )
        if qr_request is None:
            raise ValidationError({"service_request_reference": "The guest service request was not found."})
        if not service_request_queryset_for_staff(actor).filter(pk=qr_request.pk).exists():
            raise PermissionDenied("This QR request is no longer in the caller's permitted guest-service queue.")
        if (
            qr_request.channel != ServiceRequest.Channel.QR
            or qr_request.category != ServiceRequest.Category.FOOD_BEVERAGE
            or qr_request.qr_link_id is None
            or qr_request.qr_link.target_type != "TABLE"
            or not qr_request.table_number
        ):
            raise ValidationError({"service_request_reference": "Only a table QR food-and-beverage request can be linked to a POS draft."})
        if qr_request.status in {
            ServiceRequest.Status.RESOLVED, ServiceRequest.Status.CLOSED, ServiceRequest.Status.CANCELLED,
        }:
            raise ValidationError({"service_request_reference": "A resolved, closed, or cancelled request cannot create a POS draft."})
        if mode not in {PosOrder.Mode.RESTAURANT, PosOrder.Mode.BAR}:
            raise ValidationError({"mode": "Table QR requests can create only restaurant or bar drafts."})

    if idempotency_key:
        existing = PosOrder.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.service_request_id != getattr(qr_request, "pk", None):
                raise ValidationError({"idempotency_key": "This key was already used for a different POS order request."})
            return existing, False
    if qr_request is not None:
        linked_order = PosOrder.objects.select_for_update().filter(service_request=qr_request).first()
        if linked_order:
            return linked_order, False

    table_session_reference = str(table_session_reference or "").strip()
    table_session = None
    if qr_request is not None and mode == PosOrder.Mode.RESTAURANT and not table_session_reference:
        raise ValidationError({"table_session_reference": "Restaurant orders from a table QR request require its open registered table session."})
    if table_session_reference:
        if mode != PosOrder.Mode.RESTAURANT:
            raise ValidationError({"table_session_reference": "Registered table sessions are for restaurant dine-in orders only."})
        table_session = lock_open_table_session(table_session_reference)
        table_number = table_session.table.code
    if qr_request is not None:
        request_table_label = _normalise_table_label(qr_request.table_number)
        if mode == PosOrder.Mode.RESTAURANT:
            if request_table_label != _normalise_table_label(table_session.table.code):
                raise ValidationError({"table_session_reference": "The QR request must use the exact registered table code for the selected open session."})
        else:  # BAR keeps the request's optional free-text table/pickup context.
            if table_session_reference:
                raise ValidationError({"table_session_reference": "Bar orders do not use restaurant table sessions."})
            table_number = qr_request.table_number

    supplied_lines = list(lines or ())
    if not 1 <= len(supplied_lines) <= MAX_ORDER_LINES:
        raise ValidationError({"lines": f"An order must have between 1 and {MAX_ORDER_LINES} lines."})

    order_stay = None
    order_folio = None
    currency = "NGN"
    snapshot_guest = (guest_name or "")[:200]
    if mode == PosOrder.Mode.ROOM_SERVICE:
        if not stay:
            raise ValidationError({"stay": "Room-service orders require a stay."})
        order_stay, order_folio = _room_service_context(stay=stay, folio=folio, actor=actor)
        currency = order_folio.currency.upper()
        snapshot_guest = order_stay.guest.full_name
    elif stay or folio:
        raise ValidationError("Only room-service orders may attach a stay or folio in this workflow.")

    if mode == PosOrder.Mode.BAR:
        service_areas = {MenuCategory.ServiceArea.BAR}
    elif mode == PosOrder.Mode.ROOM_SERVICE:
        service_areas = {MenuCategory.ServiceArea.RESTAURANT, MenuCategory.ServiceArea.BAR}
    else:
        service_areas = {MenuCategory.ServiceArea.RESTAURANT}
    snapshotted_lines = [
        _snapshot_line(raw_line=raw_line, currency=currency, service_areas=service_areas)
        for raw_line in supplied_lines
    ]
    # Authoritative stock validation: an unavailable product must never become
    # an accepted order line, so this runs before anything is persisted. A
    # rejection rolls the whole transaction back — no order row, no lines, and
    # any pre-existing drafts by other staff are untouched.
    _validate_order_lines_stock(snapshotted_lines)
    subtotal = sum((item["line_total"] for item in snapshotted_lines), Decimal("0.00")).quantize(CENT)
    tax_amount = sum((item["tax_amount"] for item in snapshotted_lines), Decimal("0.00")).quantize(CENT)
    total_amount = (subtotal + tax_amount).quantize(CENT)
    settlement_status = (
        PosOrder.SettlementStatus.NOT_APPLICABLE
        if mode == PosOrder.Mode.ROOM_SERVICE
        else PosOrder.SettlementStatus.UNPAID
    )
    order = PosOrder.objects.create(
        reference=_new_reference("POS", PosOrder),
        idempotency_key=idempotency_key,
        mode=mode,
        settlement_status=settlement_status,
        stay=order_stay,
        folio=order_folio,
        guest_name=snapshot_guest,
        table_number=(table_number or "")[:40],
        table_session=table_session,
        service_request=qr_request,
        delivery_location=(delivery_location or "")[:160],
        notes=notes or "",
        currency=currency,
        subtotal=subtotal,
        tax_amount=tax_amount,
        total_amount=total_amount,
        created_by=actor,
    )
    PosOrderLine.objects.bulk_create(
        [
            PosOrderLine(
                order=order,
                menu_item=item["menu_item"],
                item_name=item["item_name"],
                item_sku=item["item_sku"],
                unit_price=item["unit_price"],
                quantity=item["quantity"],
                modifier_total=item["modifier_total"],
                modifiers_snapshot=item["modifiers_snapshot"],
                line_total=item["line_total"],
                notes=item["notes"],
            )
            for item in snapshotted_lines
        ]
    )
    PosOrderEvent.objects.create(
        order=order,
        type=PosOrderEvent.Type.CREATED,
        actor=actor,
        details={
            "line_count": len(snapshotted_lines),
            "total_amount": str(total_amount),
            "mode": mode,
            "service_request_reference": qr_request.reference if qr_request else "",
        },
    )
    if qr_request:
        ServiceRequestEvent.objects.create(
            request=qr_request,
            type=ServiceRequestEvent.Type.POS_ORDER_LINKED,
            actor=actor,
            message=f"POS draft {order.reference} created from this QR request.",
            guest_visible=False,
            previous_status=qr_request.status,
            new_status=qr_request.status,
            details={"pos_order_reference": order.reference, "pos_order_status": order.status},
        )
    if table_session:
        record_order_added(table_session=table_session, order=order, actor=actor)
    return order, True


@transaction.atomic
def submit_order(*, order, actor, priority=0) -> PosOrder:
    """Send a nonempty frozen order to the kitchen exactly once."""
    order = PosOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == PosOrder.Status.SUBMITTED:
        return order
    if order.status != PosOrder.Status.DRAFT:
        raise ValidationError(f"Order {order.reference} cannot submit from {order.status}.")
    if not PosOrderLine.objects.filter(order=order).exists():
        raise ValidationError("A POS order needs at least one line before submission.")
    # Re-validate stock at submission: stock that was available when the draft
    # was created may have been consumed by another delivered order since. The
    # draft itself stays intact (DRAFT, unchanged) when this rejects, so staff
    # can correct or cancel it. This order's own committed lines are excluded
    # from the open-order demand so it is not counted against itself.
    _validate_order_lines_stock(
        PosOrderLine.objects.filter(order=order).values("menu_item_id", "quantity", "item_name"),
        exclude_order=order,
    )
    now = timezone.now()
    order.status = PosOrder.Status.SUBMITTED
    order.submitted_at = now
    order.save(update_fields=["status", "submitted_at", "updated_at"])
    station = KitchenTicket.Station.BAR if order.mode == PosOrder.Mode.BAR else KitchenTicket.Station.KITCHEN
    KitchenTicket.objects.create(order=order, station=station, priority=max(0, int(priority or 0)), notes=order.notes)
    PosOrderEvent.objects.create(order=order, type=PosOrderEvent.Type.SUBMITTED, actor=actor)
    return order


def _post_order_charge(*, order, actor):
    """Charge delivered food/service separately from any tender collection."""
    if order.charge_transaction_id:
        return order.charge_transaction
    if order.total_amount <= Decimal("0.00"):
        raise ValidationError("A POS order must have a positive total before it can be charged.")
    if order.mode == PosOrder.Mode.ROOM_SERVICE:
        if not order.stay_id or not order.folio_id:
            raise ValidationError("Room-service order is missing its stay/folio.")
        stay = Stay.objects.select_for_update().get(pk=order.stay_id)
        if stay.status != Stay.Status.IN_HOUSE:
            raise ValidationError("Room-service charge requires the guest to remain in house.")
        folio = Folio.objects.select_for_update().get(pk=order.folio_id)
        if folio.status != Folio.Status.OPEN:
            raise ValidationError("Room-service charge requires an open folio.")
    else:
        folio = None

    source_key = f"pos-charge:{order.reference}"
    lines = [
        {
            "account_code": accounting.ACCOUNTS_RECEIVABLE,
            "direction": FinancialLine.Direction.DEBIT,
            "amount": order.total_amount,
            "folio": folio,
            "description": f"POS order {order.reference}",
        },
        {
            "account_code": accounting.FOOD_BEVERAGE_REVENUE,
            "direction": FinancialLine.Direction.CREDIT,
            "amount": order.subtotal,
            "description": f"Food and beverage sale {order.reference}",
        },
    ]
    if order.tax_amount > Decimal("0.00"):
        # HISTORICAL orders only: tax actually billed to the guest before the
        # customer-billing tax removal is recognised as collected tax payable.
        lines.append(
            {
                "account_code": accounting.TAX_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": order.tax_amount,
                "description": f"POS tax {order.reference}",
            }
        )
    else:
        # New customer transactions carry no billed tax. The hotel's own
        # statutory exposure (if any) is accrued on the net sale through a
        # separate expense/liability pair that never touches guest balances.
        lines.extend(
            {
                "account_code": line["account_code"],
                "direction": (
                    FinancialLine.Direction.DEBIT
                    if line["direction"] == "DEBIT"
                    else FinancialLine.Direction.CREDIT
                ),
                "amount": line["amount"],
                "description": line["description"],
            }
            for line in statutory_ledger_lines(
                net_amount=order.subtotal, reference=order.reference
            )
        )
    postings = []
    if folio:
        postings.append(
            {
                "line_index": 0,
                "folio": folio,
                "kind": FolioPosting.Kind.CHARGE,
                "effect": FolioPosting.Effect.DEBIT,
                "amount": order.total_amount,
                "description": f"Room service {order.reference}",
                "source_reference": order.reference,
            }
        )
    financial_transaction, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.POS_CHARGE,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=order.reference,
        external_reference=order.reference,
        narrative=f"Delivered POS order {order.reference}",
        metadata={"pos_order_id": order.pk, "mode": order.mode, "stay_id": order.stay_id},
        currency=order.currency,
        business_date=hotel_today(),
        lines=lines,
        postings=postings,
    )
    order.charge_transaction = financial_transaction
    order.save(update_fields=["charge_transaction", "updated_at"])
    PosOrderEvent.objects.create(
        order=order,
        type=PosOrderEvent.Type.CHARGED,
        actor=actor,
        details={"financial_transaction": financial_transaction.reference, "amount": str(order.total_amount)},
    )
    return financial_transaction


@transaction.atomic
def bill_unbilled_order_charge(*, order, actor):
    """Post this order's folio charge now, exactly once.

    Delivery posts the charge, so a delivered order normally already owns one.
    This entry point exists for the anomaly a check-out desk can actually meet:
    an order that carries no charge transaction (posted by an older build, or
    interrupted mid-transition). It reuses the same idempotent posting, so a
    settled order is never charged twice.
    """
    return _post_order_charge(order=order, actor=actor)


@transaction.atomic
def transition_order(*, order, target_status: str, actor) -> PosOrder:
    """Advance kitchen/fulfilment workflow; delivery posts the immutable charge.

    This function OWNS its transaction (the same contract as
    ``create_order``/``submit_order``/``capture_direct_tender``): the row
    lock below, the kitchen-ticket update, the folio charge posting, the
    inventory stock consumption on delivery and the order event must all
    commit or roll back together, and concurrent transitions of the same
    order must serialize on the locked row. Callers must NOT need to open
    a transaction for it — production raised
    ``TransactionManagementError: select_for_update cannot be used outside
    of a transaction`` (HTTP 500) when this decorator was missing.
    """
    order = PosOrder.objects.select_for_update().get(pk=order.pk)
    target_status = str(target_status or "").upper()
    if target_status == order.status:
        return order
    allowed = {
        PosOrder.Status.DRAFT: {PosOrder.Status.CANCELLED},
        PosOrder.Status.SUBMITTED: {PosOrder.Status.PREPARING, PosOrder.Status.READY, PosOrder.Status.DELIVERED, PosOrder.Status.CANCELLED},
        PosOrder.Status.PREPARING: {PosOrder.Status.READY, PosOrder.Status.DELIVERED, PosOrder.Status.CANCELLED},
        PosOrder.Status.READY: {PosOrder.Status.DELIVERED, PosOrder.Status.CANCELLED},
    }
    if target_status not in allowed.get(order.status, set()):
        raise ValidationError(f"Order {order.reference} cannot transition from {order.status} to {target_status}.")

    ticket = KitchenTicket.objects.select_for_update().filter(order=order).first()
    now = timezone.now()
    if target_status == PosOrder.Status.PREPARING:
        if ticket is not None:
            ticket.status = KitchenTicket.Status.PREPARING
            ticket.started_at = now
            ticket.assigned_to = actor
            ticket.save(update_fields=["status", "started_at", "assigned_to", "updated_at"])
        event_type = PosOrderEvent.Type.PREPARING
    elif target_status == PosOrder.Status.READY:
        if ticket is not None:
            if not ticket.started_at:
                ticket.started_at = now
                ticket.assigned_to = ticket.assigned_to or actor
            ticket.status = KitchenTicket.Status.READY
            ticket.ready_at = now
            ticket.save(update_fields=["status", "started_at", "assigned_to", "ready_at", "updated_at"])
        event_type = PosOrderEvent.Type.READY
    elif target_status == PosOrder.Status.DELIVERED:
        _post_order_charge(order=order, actor=actor)
        if ticket is not None:
            if not ticket.started_at:
                ticket.started_at = now
                ticket.assigned_to = ticket.assigned_to or actor
            if not ticket.ready_at:
                ticket.ready_at = now
            ticket.status = KitchenTicket.Status.COMPLETED
            ticket.completed_at = now
            ticket.save(update_fields=["status", "started_at", "assigned_to", "ready_at", "completed_at", "updated_at"])
        order.delivered_at = now
        event_type = PosOrderEvent.Type.DELIVERED
    else:  # CANCELLED only — paid/delivered orders deliberately cannot reach here.
        if ticket is not None:
            ticket.status = KitchenTicket.Status.CANCELLED
            ticket.save(update_fields=["status", "updated_at"])
        order.cancelled_at = now
        event_type = PosOrderEvent.Type.CANCELLED
    order.status = target_status
    update_fields = ["status", "updated_at"]
    if target_status == PosOrder.Status.DELIVERED:
        update_fields.append("delivered_at")
    if target_status == PosOrder.Status.CANCELLED:
        update_fields.append("cancelled_at")
    order.save(update_fields=update_fields)
    if target_status == PosOrder.Status.DELIVERED:
        # Inventory owns recipe resolution and stock balances. POS only emits a
        # source-keyed immutable hand-off from its frozen order-line snapshot.
        #
        # The hand-off is then resolved IN THE SAME TRANSACTION, because
        # delivery IS this repository's stock-consumption event. Leaving it
        # PENDING (the previous behaviour) meant no stock-consuming sales
        # channel ever reduced Stock on Hand until somebody opened the
        # inventory console and pressed Process.
        from apps.inventory.services.inventory_service import consume_stock_for_delivered_order

        consume_stock_for_delivered_order(order=order, actor=actor)
    PosOrderEvent.objects.create(order=order, type=event_type, actor=actor)
    return order


def _tender_asset_account(method: str) -> str:
    if method == PosTender.Method.CASH:
        return accounting.CASH_ON_HAND
    if method == PosTender.Method.CARD:
        return accounting.CARD_CLEARING
    return accounting.BANK_CLEARING


def tender_captured_amount(order) -> Decimal:
    """Money already collected against one POS order (captured tenders only).

    Voided and refunded tenders never count towards what the guest has paid, so
    only ``CAPTURED`` rows are summed. Uses the prefetched ``tenders`` cache when
    the caller has it (the order detail API does) so serialising a list does not
    issue a query per row.
    """
    prefetched = getattr(order, "_prefetched_objects_cache", None) or {}
    if "tenders" in prefetched:
        return sum(
            (Decimal(row.amount) for row in prefetched["tenders"] if row.status == PosTender.Status.CAPTURED),
            Decimal("0.00"),
        ).quantize(CENT)
    total = order.tenders.filter(status=PosTender.Status.CAPTURED).aggregate(total=Sum("amount"))["total"]
    return Decimal(total or 0).quantize(CENT)


def order_outstanding_balance(order, *, captured=None) -> Decimal:
    """What the guest still owes on a POS order: ``net_total - captured``.

    ``net_total`` (order total minus unopened-item returns) is the collection
    ceiling — never the frozen ``total_amount`` — so a return reduces what may
    be collected instead of leaving the guest owing money they already got back.
    Fully paid orders return ``0.00`` and an over-collected legacy order is
    clamped to ``0.00`` so a display can never show a negative balance.
    """
    total = Decimal(order.net_total)
    paid = Decimal(captured if captured is not None else tender_captured_amount(order))
    outstanding = (total - paid).quantize(CENT)
    return outstanding if outstanding > Decimal("0.00") else Decimal("0.00")


@transaction.atomic
def capture_direct_tender(
    *, order, method: str, amount, actor, cash_session=None, external_reference="", notes="",
    currency="", idempotency_key="", expected_outstanding_balance=None,
):
    """Capture tender for a delivered direct sale as a distinct collection entry.

    The browser never decides how much is owed. Everything that gates the
    collection is derived here, inside one transaction holding the order's row
    lock:

    * the outstanding balance is recomputed from ``net_total`` and the captured
      tenders (so a partially paid order can only ever be topped up by the
      remainder, and a fully paid order cannot be collected again);
    * the amount is validated against that server figure — never against a
      client-submitted total — and an over-collection is refused;
    * the currency must match the order's currency;
    * when the caller states the balance it displayed
      (``expected_outstanding_balance``), a mismatch is refused so a stale
      screen cannot collect an amount that another cashier already changed;
    * a repeated ``idempotency_key`` (double click, retried request, lost
      response) returns the tender that already exists instead of collecting a
      second time.

    The hotel's existing evidence conventions are preserved: the tender keeps its
    own reference, is posted to the ledger as a PAYMENT_COLLECTION against the
    method's clearing account, and updates the cashier's expected cash for cash
    collections. Room-service orders still settle through the guest folio and are
    refused here, and no processing fee is invented (this codebase models no POS
    tender fee; if the hotel introduces one it must be added deliberately).
    """
    order = PosOrder.objects.select_for_update().get(pk=order.pk)
    if order.mode == PosOrder.Mode.ROOM_SERVICE:
        raise ValidationError("Room-service orders settle through the guest folio, not a direct POS tender.")
    if order.status != PosOrder.Status.DELIVERED or not order.charge_transaction_id:
        raise ValidationError("Only a delivered and charged POS order can accept tender.")
    if method not in PosTender.Method.values:
        raise ValidationError("Invalid POS tender method.")
    order_currency = (order.currency or "NGN").upper()
    presented_currency = str(currency or "").strip().upper()
    if presented_currency and presented_currency != order_currency:
        raise ValidationError(
            f"This order is denominated in {order_currency}; a {presented_currency} tender cannot settle it."
        )
    amount = _money(amount, positive=True)

    # Recompute what is owed from the immutable order figures and the tenders
    # already captured, while holding the order lock: two cashiers (or a double
    # click) can never both pass on the same balance.
    captured_rows = list(
        PosTender.objects.select_for_update().filter(order=order, status=PosTender.Status.CAPTURED)
    )
    captured = sum((Decimal(row.amount) for row in captured_rows), Decimal("0.00")).quantize(CENT)
    # Returns of untouched, unopened items reduce what the guest actually owes.
    # ``total_amount`` stays frozen as the originally charged amount, so the
    # collection ceiling is the live ``net_total`` and over-collection after a
    # return is refused instead of silently banking the difference.
    outstanding = order_outstanding_balance(order, captured=captured)
    if outstanding <= Decimal("0.00"):
        raise ValidationError(
            f"Order {order.reference} is already fully settled "
            f"({captured} of {order.net_total} {order_currency} collected); there is no balance to collect."
        )

    idempotency_key = str(idempotency_key or "").strip()[:96]
    if idempotency_key:
        # Checked under the order row lock, so a concurrent duplicate either
        # waits for this transaction or finds the tender below.
        existing = (
            PosTender.objects.select_for_update()
            .filter(order=order, metadata__idempotency_key=idempotency_key)
            .order_by("pk")
            .first()
        )
        if existing is not None:
            # A replay must never collect twice: the caller gets the tender that
            # already exists, flagged so the API can answer 200 instead of 201.
            existing.idempotent_replay = True
            return existing

    if expected_outstanding_balance is not None:
        try:
            expected = Decimal(str(expected_outstanding_balance)).quantize(CENT)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError(
                {"expected_outstanding_balance": "Enter a valid outstanding balance."}
            ) from exc
        if expected != outstanding:
            raise ValidationError(
                f"The outstanding balance changed: this screen showed {expected} {order_currency}, "
                f"but {outstanding} {order_currency} is still owed. Reload the order and collect the "
                "current balance."
            )
    if amount > outstanding:
        raise ValidationError(
            f"Tender exceeds the order's unpaid balance: {amount} {order_currency} was presented but only "
            f"{outstanding} {order_currency} is still owed. Collect at most the remaining balance."
        )

    session = None
    if method == PosTender.Method.CASH:
        if cash_session is None:
            raise ValidationError("Cash POS tender requires an open cashier session.")
        session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
        if session.status != CashSession.Status.OPEN:
            raise ValidationError("Cash tender requires an open cashier session.")
        if actor and session.cashier_id != actor.pk:
            raise ValidationError("Cash tender must use the acting cashier's open session.")

    tender = PosTender.objects.create(
        reference=_new_reference("TND", PosTender),
        order=order,
        method=method,
        amount=amount,
        currency=order_currency,
        external_reference=(external_reference or "")[:120],
        cash_session=session,
        captured_by=actor,
        notes=(notes or "")[:500],
        metadata={"idempotency_key": idempotency_key} if idempotency_key else {},
    )
    source_key = f"pos-collection:{tender.reference}"
    financial_transaction, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=order.reference,
        external_reference=tender.external_reference or tender.reference,
        narrative=f"POS tender {tender.reference} for {order.reference}",
        metadata={"pos_order_id": order.pk, "pos_tender_id": tender.pk, "method": method},
        currency=order_currency,
        business_date=hotel_today(),
        lines=[
            {
                "account_code": _tender_asset_account(method),
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "description": f"POS tender received {tender.reference}",
            },
            {
                "account_code": accounting.ACCOUNTS_RECEIVABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": f"Settle POS order {order.reference}",
            },
        ],
    )
    tender.status = PosTender.Status.CAPTURED
    tender.captured_at = timezone.now()
    tender.collection_transaction = financial_transaction
    tender.save(update_fields=["status", "captured_at", "collection_transaction", "updated_at"])
    if session:
        CashMovement.objects.create(
            cash_session=session,
            transaction=financial_transaction,
            type=CashMovement.Type.COLLECTION,
            amount=amount,
            currency=order_currency,
            actor=actor,
            source_reference=tender.reference,
            notes=f"POS order {order.reference}",
        )
        session.expected_cash = (session.expected_cash + amount).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])
    new_captured = (captured + amount).quantize(CENT)
    order.settlement_status = (
        PosOrder.SettlementStatus.PAID
        if new_captured >= order.net_total
        else PosOrder.SettlementStatus.PARTIALLY_PAID
    )
    order.save(update_fields=["settlement_status", "updated_at"])
    PosOrderEvent.objects.create(
        order=order,
        type=PosOrderEvent.Type.TENDER_CAPTURED,
        actor=actor,
        details={
            "tender_reference": tender.reference,
            "amount": str(amount),
            "method": method,
            "balance_before": str(outstanding),
            "balance_after": str(order_outstanding_balance(order, captured=new_captured)),
        },
    )
    return tender
