"""Transactional POS and room-service workflow services."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction, Folio, FolioPosting
from apps.guest_services.models import ServiceRequest, ServiceRequestEvent
from apps.guest_services.services.request_service import service_request_queryset_for_staff
from apps.finance.services import accounting
from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.references import generate_finance_reference
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
    tax_amount = (line_subtotal * Decimal(menu_item.tax_rate_percent or 0) / Decimal("100")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
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
        lines.append(
            {
                "account_code": accounting.TAX_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": order.tax_amount,
                "description": f"POS tax {order.reference}",
            }
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
def transition_order(*, order, target_status: str, actor) -> PosOrder:
    """Advance kitchen/fulfilment workflow; delivery posts the immutable charge."""
    order = PosOrder.objects.select_for_update().get(pk=order.pk)
    target_status = str(target_status or "").upper()
    if target_status == order.status:
        return order
    allowed = {
        PosOrder.Status.DRAFT: {PosOrder.Status.CANCELLED},
        PosOrder.Status.SUBMITTED: {PosOrder.Status.PREPARING, PosOrder.Status.CANCELLED},
        PosOrder.Status.PREPARING: {PosOrder.Status.READY, PosOrder.Status.CANCELLED},
        PosOrder.Status.READY: {PosOrder.Status.DELIVERED, PosOrder.Status.CANCELLED},
    }
    if target_status not in allowed.get(order.status, set()):
        raise ValidationError(f"Order {order.reference} cannot transition from {order.status} to {target_status}.")

    ticket = KitchenTicket.objects.select_for_update().filter(order=order).first()
    now = timezone.now()
    if target_status == PosOrder.Status.PREPARING:
        ticket.status = KitchenTicket.Status.PREPARING
        ticket.started_at = now
        ticket.assigned_to = actor
        ticket.save(update_fields=["status", "started_at", "assigned_to", "updated_at"])
        event_type = PosOrderEvent.Type.PREPARING
    elif target_status == PosOrder.Status.READY:
        ticket.status = KitchenTicket.Status.READY
        ticket.ready_at = now
        ticket.save(update_fields=["status", "ready_at", "updated_at"])
        event_type = PosOrderEvent.Type.READY
    elif target_status == PosOrder.Status.DELIVERED:
        _post_order_charge(order=order, actor=actor)
        ticket.status = KitchenTicket.Status.COMPLETED
        ticket.completed_at = now
        ticket.save(update_fields=["status", "completed_at", "updated_at"])
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
        from apps.inventory.services.inventory_service import request_pos_stock_consumption
        request_pos_stock_consumption(order=order, actor=actor)
    PosOrderEvent.objects.create(order=order, type=event_type, actor=actor)
    return order


def _tender_asset_account(method: str) -> str:
    if method == PosTender.Method.CASH:
        return accounting.CASH_ON_HAND
    if method == PosTender.Method.CARD:
        return accounting.CARD_CLEARING
    return accounting.BANK_CLEARING


@transaction.atomic
def capture_direct_tender(*, order, method: str, amount, actor, cash_session=None, external_reference="", notes="") -> PosTender:
    """Capture tender for a delivered direct sale as a distinct collection entry."""
    order = PosOrder.objects.select_for_update().get(pk=order.pk)
    if order.mode == PosOrder.Mode.ROOM_SERVICE:
        raise ValidationError("Room-service orders settle through the guest folio, not a direct POS tender.")
    if order.status != PosOrder.Status.DELIVERED or not order.charge_transaction_id:
        raise ValidationError("Only a delivered and charged POS order can accept tender.")
    if method not in PosTender.Method.values:
        raise ValidationError("Invalid POS tender method.")
    amount = _money(amount, positive=True)
    captured = sum(
        PosTender.objects.select_for_update()
        .filter(order=order, status=PosTender.Status.CAPTURED)
        .values_list("amount", flat=True),
        Decimal("0.00"),
    )
    if captured + amount > order.total_amount:
        raise ValidationError("Tender exceeds the order's unpaid balance.")
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
        currency=order.currency,
        external_reference=(external_reference or "")[:120],
        cash_session=session,
        captured_by=actor,
        notes=(notes or "")[:500],
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
        currency=order.currency,
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
            currency=order.currency,
            actor=actor,
            source_reference=tender.reference,
            notes=f"POS order {order.reference}",
        )
        session.expected_cash = (session.expected_cash + amount).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])
    new_captured = captured + amount
    order.settlement_status = (
        PosOrder.SettlementStatus.PAID
        if new_captured == order.total_amount
        else PosOrder.SettlementStatus.PARTIALLY_PAID
    )
    order.save(update_fields=["settlement_status", "updated_at"])
    PosOrderEvent.objects.create(
        order=order,
        type=PosOrderEvent.Type.TENDER_CAPTURED,
        actor=actor,
        details={"tender_reference": tender.reference, "amount": str(amount), "method": method},
    )
    return tender
