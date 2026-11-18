# apps/events/services.py
"""Event registration, availability, payment and finance integration.

All money flows through the existing immutable ledger via
``create_posted_transaction`` — the same engine Mini Mart and POS use.
Charge: Debit ASSET.GUEST_RECEIVABLE / Credit REVENUE.OTHER_OPERATING.
Collection: Debit the tender's clearing asset / Credit ASSET.GUEST_RECEIVABLE.
Unpaid charges therefore stay receivables and never count as collected revenue.
"""
from __future__ import annotations

import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_action
from apps.core.utils import hotel_today
from apps.finance.models import FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.finance.services.references import generate_finance_reference

from .models import EventLocation, EventPayment, HotelEvent

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _money(value, *, field="amount", positive=False) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid money value."}) from exc
    if positive and amount <= ZERO:
        raise ValidationError({field: "Value must be greater than zero."})
    if amount < ZERO:
        raise ValidationError({field: "Value cannot be negative."})
    return amount


def _new_reference(prefix, model, *, field="reference") -> str:
    for _ in range(8):
        reference = generate_finance_reference(prefix)
        if not model.objects.filter(**{field: reference}).exists():
            return reference
    raise RuntimeError(f"Could not allocate a unique {prefix} reference.")


def _collection_asset_account(method: str) -> str:
    if method == EventPayment.Method.CASH:
        return accounting.CASH_ON_HAND
    if method == EventPayment.Method.POS:
        return accounting.CARD_CLEARING
    # Bank transfer, Paystack and other approved methods settle through
    # bank clearing (same rule as payment_collection_service).
    return accounting.BANK_CLEARING


def check_availability(*, location, event_date, start_time, end_time, exclude_id=None):
    """Return conflicting non-cancelled events for the same space/time.

    Different locations may host simultaneous events; only the same
    location with overlapping [start, end) on the same date conflicts.
    """
    if end_time <= start_time:
        raise ValidationError({"end_time": "Event end time must be after the start time."})
    qs = HotelEvent.objects.filter(
        location=location, event_date=event_date,
    ).exclude(status=HotelEvent.Status.CANCELLED)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    conflicts = []
    for other in qs:
        if start_time < other.end_time and other.start_time < end_time:
            conflicts.append(other)
    return conflicts


def _assert_can_override(actor) -> bool:
    from apps.accounts.capabilities import has_capability

    return bool(actor and has_capability(actor, "payment.refund.approve"))


@transaction.atomic
def create_event(
    *, guest, location, event_type="", event_date, start_time, end_time,
    expected_guests=1, total_charged, notes="", actor=None,
    idempotency_key="", override=False, override_reason="",
    initial_payment=None, request=None,
):
    """Register an event and post its charge to the ledger exactly once."""
    if not location.is_active:
        raise ValidationError({"location_id": "That event location is inactive."})
    if expected_guests is None or int(expected_guests) < 1:
        raise ValidationError({"expected_guests": "Expected guests must be at least 1."})
    total = _money(total_charged, field="total_charged", positive=True)

    idempotency_key = str(idempotency_key or "").strip()[:96] or None
    if idempotency_key:
        existing = HotelEvent.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing, False

    conflicts = check_availability(
        location=location, event_date=event_date, start_time=start_time, end_time=end_time,
    )
    if conflicts:
        if not override:
            names = ", ".join(c.reference for c in conflicts)
            raise ValidationError({
                "event_date": (
                    f"{location.name} is already booked on {event_date} "
                    f"({names}). Choose another time or location."
                )
            })
        if not _assert_can_override(actor):
            raise ValidationError({
                "override": "Only an authorized administrator may override a booking conflict."
            })
        if not str(override_reason or "").strip():
            raise ValidationError({"override_reason": "A reason is required to override a conflict."})

    reference = _new_reference("EVT", HotelEvent)
    event = HotelEvent.objects.create(
        reference=reference, guest=guest, location=location,
        event_type=str(event_type or "").strip()[:120],
        event_date=event_date, start_time=start_time, end_time=end_time,
        expected_guests=int(expected_guests), total_charged=total,
        amount_paid=ZERO, payment_status=HotelEvent.PaymentStatus.UNPAID,
        status=HotelEvent.Status.UPCOMING, notes=str(notes or "")[:2000],
        created_by=actor, idempotency_key=idempotency_key,
        override_reason=str(override_reason or "").strip()[:2000] if conflicts else "",
    )
    # Recognise the charge as an event receivable + other-operating revenue.
    # The charge is NOT collected revenue until a payment posts.
    charge, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.OTHER_CHARGE,
        source_key=f"event-charge:{event.reference}",
        idempotency_key=f"event-charge:{event.reference}",
        actor=actor, source_reference=event.reference,
        narrative=f"Event charge {event.reference} · {location.name} · {event_date}",
        currency="NGN", business_date=hotel_today(),
        metadata={"event_id": event.pk, "event_reference": event.reference,
                  "location_id": location.pk, "guest_id": guest.pk},
        lines=[
            {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.DEBIT,
             "amount": total, "description": f"Event receivable {event.reference}"},
            {"account_code": accounting.OTHER_OPERATING_REVENUE, "direction": FinancialLine.Direction.CREDIT,
             "amount": total, "description": f"Event revenue {event.reference}"},
        ],
    )
    event.charge_transaction = charge
    event.save(update_fields=["charge_transaction", "updated_at"])

    log_action(actor=actor, action="EVENT_CREATED", instance=event, request=request,
               metadata={"location": location.name, "total_charged": str(total),
                         "override": bool(conflicts)})

    if initial_payment:
        record_event_payment(event=event, actor=actor, request=request, **initial_payment)
        event.refresh_from_db()
    return event, True


@transaction.atomic
def record_event_payment(
    *, event, amount, method, actor=None, external_reference="", notes="",
    idempotency_key="", request=None, verify_paystack=True,
):
    """Record one collection against an event. Idempotent; never over-collects."""
    event = HotelEvent.objects.select_for_update().get(pk=event.pk)
    if event.status == HotelEvent.Status.CANCELLED:
        raise ValidationError("A cancelled event cannot accept new payments. Follow the refund process for money already received.")
    amount = _money(amount, positive=True)
    method = str(method or EventPayment.Method.CASH).upper()
    if method not in EventPayment.Method.values:
        raise ValidationError({"method": "Invalid payment method."})
    external_reference = str(external_reference or "").strip()[:120]
    if method in (EventPayment.Method.BANK_TRANSFER, EventPayment.Method.POS, EventPayment.Method.PAYSTACK) and not external_reference:
        raise ValidationError({"external_reference": "A transaction reference is required for this payment method."})

    idempotency_key = str(idempotency_key or "").strip()[:96] or None
    if idempotency_key:
        existing = EventPayment.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.event_id != event.pk or Decimal(existing.amount) != amount:
                raise ValidationError({"idempotency_key": "This key was already used for a different event payment."})
            return existing, False

    outstanding = event.outstanding_balance
    if outstanding <= ZERO:
        raise ValidationError(f"Event {event.reference} is already fully paid; there is no balance to collect.")
    if amount > outstanding:
        raise ValidationError(
            f"Payment exceeds the outstanding balance: {amount} presented but only {outstanding} is still owed."
        )

    # Electronic payments are never trusted on the reference alone.
    if method == EventPayment.Method.PAYSTACK and verify_paystack:
        _verify_paystack_reference(external_reference, amount)

    reference = _new_reference("EVTPAY", EventPayment)
    payment = EventPayment.objects.create(
        event=event, amount=amount, currency="NGN", method=method,
        reference=reference, external_reference=external_reference,
        status=EventPayment.Status.SUCCESS, received_by=actor,
        paid_at=timezone.now(), idempotency_key=idempotency_key,
        notes=str(notes or "")[:500],
        metadata={"event_reference": event.reference, "guest_id": event.guest_id},
    )
    collection, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
        source_key=f"event-collection:{payment.reference}",
        idempotency_key=f"event-collection:{payment.reference}",
        actor=actor, source_reference=event.reference,
        external_reference=external_reference or payment.reference,
        narrative=f"Event collection {payment.reference} for {event.reference} via {method}",
        currency="NGN", business_date=hotel_today(),
        metadata={"event_id": event.pk, "event_payment_id": payment.pk, "method": method},
        lines=[
            {"account_code": _collection_asset_account(method), "direction": FinancialLine.Direction.DEBIT,
             "amount": amount, "description": f"Event collection {payment.reference}"},
            {"account_code": accounting.ACCOUNTS_RECEIVABLE, "direction": FinancialLine.Direction.CREDIT,
             "amount": amount, "description": f"Settle event {event.reference}"},
        ],
    )
    payment.collection_transaction = collection
    payment.save(update_fields=["collection_transaction", "updated_at"])

    event.amount_paid = (Decimal(event.amount_paid) + amount).quantize(CENT)
    event.refresh_payment_status()
    event.save(update_fields=["amount_paid", "payment_status", "updated_at"])

    log_action(actor=actor, action="EVENT_PAYMENT_RECORDED", instance=payment, request=request,
               metadata={"event_reference": event.reference, "amount": str(amount), "method": method})
    return payment, True


def _verify_paystack_reference(reference: str, expected_amount: Decimal):
    """Verify a Paystack reference against the gateway before counting it."""
    from apps.payments.services import paystack as paystack_service

    configured, _mode = paystack_service.secret_key_status()
    if not configured:
        raise ValidationError({
            "external_reference": "Paystack is not configured on this server, so this reference cannot be verified. Record the payment by another method or ask an administrator to configure Paystack."
        })
    try:
        data = paystack_service.verify_transaction(reference)
    except Exception as exc:  # gateway errors must never count as paid
        raise ValidationError({
            "external_reference": f"Paystack verification failed for {reference}: {exc}"
        }) from exc
    status = str((data or {}).get("status") or "").lower()
    if status != "success":
        raise ValidationError({"external_reference": f"Paystack reports this transaction as {status or 'unknown'}; it is not a successful payment."})
    try:
        paid_kobo = int((data or {}).get("amount") or 0)
    except (TypeError, ValueError):
        paid_kobo = 0
    if paid_kobo and Decimal(paid_kobo) / Decimal("100") != expected_amount:
        raise ValidationError({
            "external_reference": f"Paystack amount {(Decimal(paid_kobo) / Decimal('100'))} does not match the recorded {expected_amount}."
        })


@transaction.atomic
def cancel_event(*, event, actor=None, reason="", request=None):
    """Cancel an event without erasing its financial records.

    Money already received stays on the ledger; any refund follows the
    existing finance refund/adjustment process (payment.refund.approve).
    """
    event = HotelEvent.objects.select_for_update().get(pk=event.pk)
    if event.status == HotelEvent.Status.CANCELLED:
        return event, False
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A cancellation reason is required."})
    event.status = HotelEvent.Status.CANCELLED
    event.cancelled_at = timezone.now()
    event.cancellation_reason = reason[:2000]
    event.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])
    log_action(actor=actor, action="EVENT_CANCELLED", instance=event, request=request,
               metadata={"reason": reason, "amount_paid": str(event.amount_paid)})
    return event, True


def update_event_status(*, event, actor=None, request=None):
    """Derive Upcoming/Ongoing/Completed from the clock (never overwrites Cancelled)."""
    if event.status == HotelEvent.Status.CANCELLED:
        return event
    today = hotel_today()
    now = timezone.localtime().time()
    if event.event_date < today:
        new = HotelEvent.Status.COMPLETED
    elif event.event_date == today:
        if now >= event.end_time:
            new = HotelEvent.Status.COMPLETED
        elif now >= event.start_time:
            new = HotelEvent.Status.ONGOING
        else:
            new = HotelEvent.Status.UPCOMING
    else:
        new = HotelEvent.Status.UPCOMING
    if new != event.status:
        event.status = new
        event.save(update_fields=["status", "updated_at"])
        log_action(actor=actor, action="EVENT_STATUS_UPDATED", instance=event, request=request,
                   metadata={"status": new})
    return event
