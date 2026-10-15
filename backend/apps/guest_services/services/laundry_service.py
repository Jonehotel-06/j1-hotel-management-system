"""Laundry workflow: receive → process → ready → deliver, billed once.

Pricing lives on the item lines collected at reception. Billing posts a
single folio charge through the existing Finance ledger (guest receivable
against other operating revenue, with the folio posting) the first time it
is requested — retries return the original transaction instead of charging
twice. Nothing else in this module moves money or stock.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_today

from ..models import LaundryItem, LaundryOrder

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

FLOW = {
    LaundryOrder.Status.RECEIVED: {LaundryOrder.Status.IN_PROGRESS, LaundryOrder.Status.CANCELLED},
    LaundryOrder.Status.IN_PROGRESS: {LaundryOrder.Status.READY, LaundryOrder.Status.CANCELLED},
    LaundryOrder.Status.READY: {LaundryOrder.Status.DELIVERED},
    LaundryOrder.Status.DELIVERED: set(),
    LaundryOrder.Status.CANCELLED: set(),
}


def _reference() -> str:
    from ..models import LaundryOrder as Order

    from apps.finance.services.references import generate_finance_reference

    return generate_finance_reference("LND")


def _money(value, field) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid amount."}) from exc
    if amount < ZERO:
        raise ValidationError({field: "Amount cannot be negative."})
    return amount


@transaction.atomic
def create_laundry_order(*, actor, stay, items, room=None, notes="", idempotency_key=""):
    """Receive one laundry collection with priced item lines."""
    if not has_capability(actor, "guest_request.manage"):
        raise PermissionDenied("This user cannot receive laundry.")
    key = str(idempotency_key or "").strip()
    if key:
        existing = LaundryOrder.objects.filter(idempotency_key=key).first()
        if existing:
            return existing, False
    from apps.stays.models import Stay

    stay = Stay.objects.select_related("guest", "booking").get(pk=stay.pk)
    lines = []
    total = ZERO
    for index, raw in enumerate(items or []):
        description = str(raw.get("description") or "").strip()[:160]
        if not description:
            raise ValidationError({"items": f"Item {index + 1} needs a description."})
        try:
            quantity = int(raw.get("quantity") or 1)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"items": f"Item {index + 1} quantity must be a whole number."}) from exc
        if quantity <= 0:
            raise ValidationError({"items": f"Item {index + 1} quantity must be positive."})
        service_type = str(raw.get("service_type") or LaundryItem.ServiceType.WASH).upper()
        if service_type not in LaundryItem.ServiceType.values:
            raise ValidationError({"items": f"Item {index + 1} has an unknown service type."})
        unit_price = _money(raw.get("unit_price") or "0.00", f"items[{index}].unit_price")
        lines.append((description, quantity, service_type, unit_price))
        total += (Decimal(quantity) * unit_price).quantize(CENT)
    if not lines:
        raise ValidationError({"items": "Add at least one laundry item."})
    order = LaundryOrder.objects.create(
        reference=_reference(),
        idempotency_key=key or None,
        stay=stay, guest=stay.guest, room=room,
        received_by=actor, notes=str(notes or "")[:2000],
        total_amount=total.quantize(CENT), currency="NGN",
    )
    LaundryItem.objects.bulk_create([
        LaundryItem(order=order, description=d, quantity=q, service_type=s, unit_price=u)
        for d, q, s, u in lines
    ])
    return order, True


@transaction.atomic
def assign_laundry_order(*, order, actor, assignee):
    if not has_capability(actor, "guest_request.assign"):
        raise PermissionDenied("This user cannot assign laundry work.")
    order = LaundryOrder.objects.select_for_update().get(pk=order.pk)
    if order.status in {LaundryOrder.Status.DELIVERED, LaundryOrder.Status.CANCELLED}:
        raise ValidationError("Finished laundry cannot be reassigned.")
    order.assigned_to = assignee
    order.save(update_fields=["assigned_to", "updated_at"])
    return order


@transaction.atomic
def advance_laundry_order(*, order, actor, new_status, delivered_to=""):
    """Move an order along the processing flow; DELIVERED confirms handover."""
    if not has_capability(actor, "guest_request.manage"):
        raise PermissionDenied("This user cannot process laundry.")
    order = LaundryOrder.objects.select_for_update().get(pk=order.pk)
    new_status = str(new_status or "").upper()
    if new_status not in FLOW.get(order.status, set()):
        raise ValidationError(f"Laundry cannot move from {order.status} to {new_status or '—'}.")
    order.status = new_status
    updates = ["status", "updated_at"]
    if new_status == LaundryOrder.Status.READY:
        order.ready_at = timezone.now()
        updates.append("ready_at")
    if new_status == LaundryOrder.Status.DELIVERED:
        order.delivered_at = timezone.now()
        order.delivered_by = actor
        order.delivered_to = str(delivered_to or "")[:160]
        updates += ["delivered_at", "delivered_by", "delivered_to"]
    order.save(update_fields=updates)
    return order


@transaction.atomic
def bill_laundry_order(*, order, actor):
    """Charge the order total to the guest's folio, exactly once."""
    if not has_capability(actor, "guest_request.manage"):
        raise PermissionDenied("This user cannot bill laundry.")
    order = LaundryOrder.objects.select_for_update().select_related("stay", "guest").get(pk=order.pk)
    if order.financial_transaction_id:
        return order, False
    if order.status == LaundryOrder.Status.CANCELLED:
        raise ValidationError("Cancelled laundry cannot be billed.")
    if Decimal(order.total_amount) <= ZERO:
        raise ValidationError("This laundry has no priced items to bill.")

    from apps.finance.models import FinancialLine, FinancialTransaction
    from apps.finance.services import accounting
    from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
    from apps.finance.services.ledger_service import create_posted_transaction

    folio, _ = get_or_create_main_folio_for_stay(stay=order.stay, actor=actor)
    transaction_row, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
        source_key=f"laundry:{order.reference}",
        idempotency_key=f"laundry:{order.reference}",
        actor=actor,
        source_reference=order.reference,
        narrative=f"Laundry {order.reference} ({order.items.count()} item lines)",
        currency=order.currency,
        business_date=hotel_today(),
        metadata={"laundry_reference": order.reference, "stay_reference": order.stay.reference},
        lines=[
            {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT,
             "amount": order.total_amount, "folio": folio, "description": f"Laundry {order.reference}"},
            {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT,
             "amount": order.total_amount, "description": f"Laundry revenue {order.reference}"},
        ],
        postings=[{"line_index": 0, "folio": folio, "kind": "CHARGE", "effect": "DEBIT", "amount": str(order.total_amount)}],
    )
    LaundryOrder.objects.filter(pk=order.pk, financial_transaction__isnull=True).update(
        folio=folio, financial_transaction=transaction_row, billed_at=timezone.now(),
    )
    order.refresh_from_db()
    return order, created
