"""Atomic cash retail checkout. No browser totals, stock writes or paid flags accepted."""
import hashlib
import json
import secrets
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import CashSession, FinancialTransaction, FinancialLine
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.inventory.models import StockItem, StockLocation, StockBalance, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement
from apps.pos.models import PosOrder, PosOrderLine, PosOrderEvent
from apps.pos.services.order_service import capture_direct_tender
from .models import MiniMartProduct, MiniMartSale, MiniMartSaleLine


def assert_seller(actor):
    if not (actor and actor.is_active and actor.role in {"ADMIN", "GENERAL_MANAGER", "RECEPTIONIST"}
            and has_capability(actor, "guest_amenity.issue") and has_capability(actor, "payment.capture")):
        raise PermissionDenied("Authorized front-desk sales access is required.")


@transaction.atomic
def complete_sale(*, actor, data):
    assert_seller(actor)
    # Serialize retries per staff before reading the source key, including on MySQL.
    User.objects.select_for_update().get(pk=actor.pk)
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    key = "mm:" + hashlib.sha256(f"{actor.pk}:{data['idempotency_key']}".encode()).hexdigest()
    existing = MiniMartSale.objects.select_for_update().filter(source_key=key).first()
    if existing:
        if existing.payload_fingerprint != fingerprint:
            raise ValidationError("This checkout key was already used for a different cart.")
        return existing, False
    session = CashSession.objects.select_for_update().filter(
        reference=data["cash_session_reference"], cashier=actor, status=CashSession.Status.OPEN,
    ).first()
    if session is None:
        raise ValidationError({"cash_session_reference": "Use your own open cashier session."})
    guest = booking = None
    rooms = ""
    if data.get("guest_id"):
        from apps.bookings.models import Guest, Booking
        guest = Guest.objects.filter(pk=data["guest_id"]).first()
        if guest is None:
            raise ValidationError({"guest_id": "Unknown guest."})
        if data.get("booking_id"):
            booking = Booking.objects.filter(pk=data["booking_id"], guest=guest).first()
            if booking is None:
                raise ValidationError({"booking_id": "This booking does not belong to the selected guest."})
            rooms = ", ".join(booking.room_assignments.select_related("room").values_list("room__room_number", flat=True))
    elif data.get("booking_id"):
        raise ValidationError({"guest_id": "Select the guest for this booking."})
    ids = sorted(line["product_id"] for line in data["lines"])
    products = list(MiniMartProduct.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if len(products) != len(ids):
        raise ValidationError({"lines": "One or more products no longer exist."})
    # Same item -> location -> balance lock order as the authoritative ledger.
    items = {i.pk: i for i in StockItem.objects.select_for_update().filter(pk__in=[p.item_id for p in products]).order_by("pk")}
    locations = {l.pk: l for l in StockLocation.objects.select_for_update().filter(pk__in=[p.location_id for p in products]).order_by("pk")}
    quantities = {line["product_id"]: line["quantity"] for line in data["lines"]}
    for p in products:
        item, loc = items[p.item_id], locations[p.location_id]
        if not (p.is_active and item.is_active and loc.is_active and item.item_type == "SELLABLE_PRODUCT" and item.currency.upper() == "NGN"):
            raise ValidationError({"lines": "A product is not available for retail sale."})
        bal = StockBalance.objects.select_for_update().filter(item=item, location=loc).first()
        if bal is None or bal.quantity_on_hand < quantities[p.pk]:
            raise ValidationError({"lines": f"Insufficient stock for {item.name}. Refresh the catalogue."})
    total = sum((p.selling_price * quantities[p.pk] for p in products), Decimal("0.00"))
    if total != Decimal(str(data["expected_total"])):
        raise ValidationError({"expected_total": "Prices changed. Refresh products and confirm the new payable amount before collecting cash."})
    if total > Decimal("9999999999.99"):
        raise ValidationError("Cart total exceeds the supported checkout limit.")
    order = PosOrder.objects.create(
        reference=f"MM-{timezone.now():%Y%m%d}-{secrets.token_hex(8).upper()}",
        mode=PosOrder.Mode.TAKEAWAY, created_by=actor, guest_name=guest.full_name if guest else "Walk-in customer",
        subtotal=total, total_amount=total, settlement_status=PosOrder.SettlementStatus.UNPAID,
        metadata={"minimart": True},
    )
    sale = MiniMartSale.objects.create(order=order, guest=guest, booking=booking, room_number=rooms,
                                      source_key=key, payload_fingerprint=fingerprint)
    for p in products:
        item, loc, qty = items[p.item_id], locations[p.location_id], quantities[p.pk]
        line = PosOrderLine.objects.create(order=order, item_name=item.name[:180], item_sku=item.sku[:64],
                                          unit_price=p.selling_price, quantity=qty, line_total=p.selling_price * qty)
        movement, _ = record_stock_movement(
            item=item, location=loc, movement_type=StockMovement.Type.ISSUE, quantity_delta=-qty,
            source_key=f"minimart:{sale.pk}:{p.pk}", actor=actor, source_reference=order.reference,
            department="FRONT_DESK", recipient=order.guest_name, automated=True,
            notes=f"Mini Mart retail sale {order.reference}",
            metadata={"minimart_sale_id": sale.pk, "pos_order_id": order.pk, "pos_order_line_id": line.pk},
        )
        MiniMartSaleLine.objects.create(sale=sale, product=p, order_line=line, movement=movement)
    # Reuse the immutable posting engine and existing OTHER_OPERATING revenue,
    # not the restaurant helper which always credits FOOD_BEVERAGE revenue.
    charge, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.POS_CHARGE, source_key=f"pos-charge:{order.reference}",
        idempotency_key=f"pos-charge:{order.reference}", actor=actor, source_reference=order.reference,
        narrative=f"Mini Mart sale {order.reference}", currency="NGN", business_date=hotel_today(),
        metadata={"pos_order_id": order.pk, "minimart_sale_id": sale.pk},
        lines=[{"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT, "amount": total},
               {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT, "amount": total}],
    )
    order.charge_transaction = charge
    order.status = PosOrder.Status.DELIVERED
    order.delivered_at = timezone.now()
    order.save(update_fields=["charge_transaction", "status", "delivered_at", "updated_at"])
    PosOrderEvent.objects.create(order=order, type=PosOrderEvent.Type.DELIVERED, actor=actor, details={"minimart_sale_id": sale.pk})
    capture_direct_tender(order=order, method="CASH", amount=total, actor=actor, cash_session=session, idempotency_key=key)
    return sale, True
