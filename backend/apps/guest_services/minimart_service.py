"""Atomic retail checkout. No browser totals, stock writes or paid flags accepted.

Supports Cash, Card/POS, Bank Transfer and Paystack through the existing
POS tender + immutable ledger path. Cash settles through the acting
cashier's open session; electronic methods settle through bank/card
clearing and require a transaction reference. Paystack references are
verified against the gateway before the sale counts as paid.

Room charge is NOT a tender: the sale becomes a receivable on the in-house
guest's existing main folio, using the same folio posting path room service
and laundry already use. It never marks the order paid — the guest still owes
the amount on their folio.
"""
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


def _room_charge_context(*, guest, booking, actor):
    """Resolve the in-house stay and its open main folio for a room charge.

    Mirrors the eligibility rules room service already enforces: the guest must
    be genuinely IN_HOUSE on the selected booking and the folio must be OPEN.
    A folio charge is a receivable, so a guest who has checked out (or a
    walk-in) cannot be charged.
    """
    from apps.finance.models import Folio
    from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
    from apps.stays.models import Stay
    from django.core.exceptions import ObjectDoesNotExist

    if guest is None or booking is None:
        raise ValidationError({"guest_id": "A room charge needs the in-house guest and their booking."})
    try:
        stay = booking.stay
    except ObjectDoesNotExist:
        raise ValidationError({"booking_id": "This guest has not checked in, so there is no folio to charge."})
    stay = Stay.objects.select_for_update().get(pk=stay.pk)
    if stay.status != Stay.Status.IN_HOUSE:
        raise ValidationError({"booking_id": "Only an in-house guest can be charged to a room."})
    folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=actor)
    folio = Folio.objects.select_for_update().get(pk=folio.pk)
    if folio.status != Folio.Status.OPEN:
        raise ValidationError({"booking_id": "The guest's folio is not open, so it cannot take this charge."})
    return stay, folio


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
    method = str(data.get("payment_method") or "CASH").upper()
    if method not in ("CASH", "CARD", "BANK_TRANSFER", "PAYSTACK", "ROOM_CHARGE"):
        raise ValidationError({"payment_method": "Invalid payment method."})
    session = None
    if method == "CASH":
        session = CashSession.objects.select_for_update().filter(
            reference=data.get("cash_session_reference") or "", cashier=actor, status=CashSession.Status.OPEN,
        ).first()
        if session is None:
            raise ValidationError({"cash_session_reference": "Use your own open cashier session."})
    transaction_reference = str(data.get("transaction_reference") or "").strip()[:120]
    if method in ("CARD", "BANK_TRANSFER", "PAYSTACK") and not transaction_reference:
        raise ValidationError({"transaction_reference": "A transaction reference is required for this payment method."})
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
    stay = folio = None
    if method == "ROOM_CHARGE":
        stay, folio = _room_charge_context(guest=guest, booking=booking, actor=actor)
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
        subtotal=total, total_amount=total,
        # A room charge settles through the guest folio, exactly like room
        # service: nothing has been paid, so it must never read UNPAID-as-if-a-
        # tender-is-owed nor PAID.
        settlement_status=(PosOrder.SettlementStatus.NOT_APPLICABLE if method == "ROOM_CHARGE"
                           else PosOrder.SettlementStatus.UNPAID),
        stay=stay, folio=folio,
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
        metadata={"pos_order_id": order.pk, "minimart_sale_id": sale.pk,
                  "folio_reference": folio.reference if folio else ""},
        lines=[{"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT,
                "amount": total, "folio": folio, "description": f"Mini Mart sale {order.reference}"},
               {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT, "amount": total}],
        # The receivable must land on the guest's statement so the folio balance
        # (not a phantom paid sale) reflects what the guest owes.
        postings=([{"line_index": 0, "folio": folio, "kind": "CHARGE", "effect": "DEBIT", "amount": str(total),
                    "description": f"Mini Mart {order.reference}", "source_reference": order.reference}]
                  if folio else []),
    )
    order.charge_transaction = charge
    order.status = PosOrder.Status.DELIVERED
    order.delivered_at = timezone.now()
    order.save(update_fields=["charge_transaction", "status", "delivered_at", "updated_at"])
    PosOrderEvent.objects.create(order=order, type=PosOrderEvent.Type.DELIVERED, actor=actor, details={"minimart_sale_id": sale.pk})
    if method == "ROOM_CHARGE":
        # Charged to the folio — no tender, no cash movement, nothing marked paid.
        return sale, True
    # Amount actually received (defaults to the full total for legacy cash
    # clients). Partial payments are preserved: capture_direct_tender caps
    # the tender at the outstanding balance and marks the order PARTIALLY_PAID.
    tender_amount = Decimal(str(data.get("payment_amount") or total))
    if tender_amount <= Decimal("0.00") or tender_amount > total:
        raise ValidationError({"payment_amount": "Amount received must be greater than zero and no more than the sale total."})
    if method == "PAYSTACK":
        _verify_paystack_reference(transaction_reference, tender_amount)
    capture_direct_tender(
        order=order, method=method, amount=tender_amount, actor=actor,
        cash_session=session, external_reference=transaction_reference,
        idempotency_key=key,
    )
    return sale, True


def _verify_paystack_reference(reference: str, expected_amount: Decimal):
    """Verify a Paystack reference before the sale counts as paid."""
    from apps.payments.services import paystack as paystack_service

    configured, _mode = paystack_service.secret_key_status()
    if not configured:
        raise ValidationError({
            "transaction_reference": "Paystack is not configured on this server, so this reference cannot be verified. Use another payment method or ask an administrator to configure Paystack."
        })
    try:
        data = paystack_service.verify_transaction(reference)
    except Exception as exc:
        raise ValidationError({"transaction_reference": f"Paystack verification failed for {reference}: {exc}"}) from exc
    if str((data or {}).get("status") or "").lower() != "success":
        raise ValidationError({"transaction_reference": "Paystack does not report this transaction as successful."})
    try:
        paid_kobo = int((data or {}).get("amount") or 0)
    except (TypeError, ValueError):
        paid_kobo = 0
    if paid_kobo and Decimal(paid_kobo) / Decimal("100") != expected_amount:
        raise ValidationError({"transaction_reference": "Paystack amount does not match the amount received."})
