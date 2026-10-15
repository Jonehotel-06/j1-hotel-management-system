"""Bridge legacy payment projections into the immutable finance ledger.

``payments.Payment`` remains the public/payment-provider compatibility record.
A successful payment is dual-written transactionally as a collection event; it
is then applied to a folio only when an operational folio exists.  This keeps
cash/bank collection distinct from a guest-balance application.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.payments.models import Payment
from apps.payments.services.payment_service import (
    PaymentAlreadyCompletedError,
    _unique_payment_reference,
)

from ..models import (
    FinancialEvent, FinancialLine, FinancialTransaction, Folio, PaymentAllocation, RefundApplication,
)
from . import accounting
from .ledger_service import LedgerIntegrityError, create_posted_transaction

COLLECTED_PAYMENT_STATUSES = {
    Payment.Status.SUCCESS,
    Payment.Status.PARTIALLY_REFUNDED,
    Payment.Status.REFUNDED,
}


def _collection_asset_account(payment: Payment) -> str:
    if payment.provider == Payment.Provider.CASH:
        return accounting.CASH_ON_HAND
    if payment.provider == Payment.Provider.POS:
        return accounting.CARD_CLEARING
    # Paystack and bank transfers both settle through a traceable bank-clearing
    # account. Merchant fees/settlement entries are separate, never silently
    # netted from the guest's gross collection.
    return accounting.BANK_CLEARING


def _assert_collected(payment: Payment):
    if payment.status not in COLLECTED_PAYMENT_STATUSES:
        raise ValidationError("Only a successfully collected payment can enter the finance ledger.")
    if Decimal(payment.amount or 0) <= Decimal("0.00"):
        raise ValidationError("A successful payment must have a positive amount.")


@transaction.atomic
def ensure_payment_collection(*, payment, actor=None) -> tuple[FinancialTransaction, bool]:
    """Record a successful payment's gross collection exactly once.

    Debit is the actual tender/clearing asset; credit is guest deposits until
    the payment is applied to a stay folio. The source key is stable across
    Paystack webhooks, manual retries, and lazy historic bridges.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("booking", "user")
        .get(pk=payment.pk)
    )
    _assert_collected(payment)
    source_key = f"payment-collection:{payment.reference}"
    return create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYMENT_COLLECTION,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor or payment.user,
        source_reference=payment.booking.booking_reference,
        external_reference=payment.reference,
        narrative=f"Collection {payment.reference} via {payment.get_provider_display()}",
        metadata={
            "payment_id": payment.pk,
            "payment_reference": payment.reference,
            "provider": payment.provider,
            "channel": payment.channel,
            "booking_reference": payment.booking.booking_reference,
        },
        currency=payment.currency,
        business_date=(payment.paid_at.date() if payment.paid_at else None),
        occurred_at=payment.paid_at,
        lines=[
            {
                "account_code": _collection_asset_account(payment),
                "direction": FinancialLine.Direction.DEBIT,
                "amount": payment.amount,
                "description": f"Gross collection {payment.reference}",
            },
            {
                "account_code": accounting.GUEST_DEPOSITS,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": payment.amount,
                "description": f"Guest deposit {payment.booking.booking_reference}",
            },
        ],
    )


@transaction.atomic
def apply_payment_to_folio(*, payment, folio, actor=None, amount=None) -> tuple[PaymentAllocation, bool]:
    """Apply one payment's available deposit to a matching open folio.

    The initial vertical slice deliberately has one immutable application per
    payment/folio pair. Future split-folio UI can issue distinct explicit
    allocation keys without changing the collection event or its audit trail.
    """
    payment = Payment.objects.select_for_update().select_related("booking", "user").get(pk=payment.pk)
    folio = Folio.objects.select_for_update().select_related("booking").get(pk=folio.pk)
    _assert_collected(payment)
    if folio.status != Folio.Status.OPEN:
        raise ValidationError("Payment can only be applied to an open folio.")
    if folio.booking_id != payment.booking_id:
        raise ValidationError("Payment and folio must belong to the same booking in this workflow.")
    if folio.currency.upper() != payment.currency.upper():
        raise ValidationError("Payment and folio currency must match.")

    allocation_key = f"payment-allocation:{payment.reference}:{folio.reference}"
    existing = (
        PaymentAllocation.objects.select_for_update()
        .select_related("transaction")
        .filter(source_key=allocation_key)
        .first()
    )
    if existing:
        return existing, False

    prior_allocations = list(
        PaymentAllocation.objects.select_for_update().filter(payment=payment).only("amount")
    )
    available = Decimal(payment.amount) - sum((item.amount for item in prior_allocations), Decimal("0.00"))
    if available <= Decimal("0.00"):
        raise ValidationError("This payment has no unallocated balance remaining.")
    requested = Decimal(str(amount)) if amount is not None else available
    requested = requested.quantize(Decimal("0.01"))
    if requested <= Decimal("0.00") or requested > available:
        raise ValidationError("Allocation amount must be positive and no greater than the unallocated payment balance.")

    ensure_payment_collection(payment=payment, actor=actor or payment.user)
    financial_transaction, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYMENT_ALLOCATION,
        source_key=allocation_key,
        idempotency_key=allocation_key,
        actor=actor or payment.user,
        source_reference=payment.booking.booking_reference,
        external_reference=payment.reference,
        narrative=f"Apply payment {payment.reference} to folio {folio.reference}",
        metadata={
            "payment_id": payment.pk,
            "payment_reference": payment.reference,
            "folio_reference": folio.reference,
            "booking_reference": payment.booking.booking_reference,
        },
        currency=payment.currency,
        business_date=(payment.paid_at.date() if payment.paid_at else None),
        occurred_at=payment.paid_at,
        lines=[
            {
                "account_code": accounting.GUEST_DEPOSITS,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": requested,
                "description": f"Release guest deposit {payment.reference}",
            },
            {
                "account_code": accounting.ACCOUNTS_RECEIVABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": requested,
                "folio": folio,
                "description": f"Payment applied to {folio.reference}",
            },
        ],
        postings=[
            {
                "line_index": 1,
                "folio": folio,
                "kind": "PAYMENT",
                "effect": "CREDIT",
                "amount": requested,
                "description": f"Payment {payment.reference}",
                "source_reference": payment.reference,
            }
        ],
    )
    allocation = PaymentAllocation.objects.create(
        payment=payment,
        transaction=financial_transaction,
        folio=folio,
        amount=requested,
        currency=payment.currency.upper(),
        source_key=allocation_key,
        allocated_by=actor or payment.user,
    )
    FinancialEvent.objects.create(
        transaction=financial_transaction,
        type=FinancialEvent.Type.ALLOCATED,
        actor=actor or payment.user,
        details={
            "payment_reference": payment.reference,
            "folio_reference": folio.reference,
            "amount": str(requested),
            "payment_allocation_id": allocation.pk,
        },
    )
    return allocation, True


@transaction.atomic
def apply_unallocated_payments_for_booking(*, booking, folio, actor=None) -> list[PaymentAllocation]:
    """Lazily bridge all successful legacy/current payments once a folio opens."""
    if folio.booking_id != booking.pk:
        raise ValidationError("The folio must belong to the supplied booking.")
    payments = list(
        Payment.objects.select_for_update()
        .filter(booking=booking, status__in=COLLECTED_PAYMENT_STATUSES)
        .order_by("paid_at", "created_at", "pk")
    )
    allocations = []
    for payment in payments:
        already_allocated = sum(
            PaymentAllocation.objects.select_for_update()
            .filter(payment=payment)
            .values_list("amount", flat=True),
            Decimal("0.00"),
        )
        if Decimal(payment.amount) - already_allocated <= Decimal("0.00"):
            continue
        allocation, created = apply_payment_to_folio(
            payment=payment,
            folio=folio,
            actor=actor,
        )
        if created:
            allocations.append(allocation)
    return allocations


@transaction.atomic
def ensure_processed_refund(*, refund, actor=None) -> tuple[FinancialTransaction, bool]:
    """Record a provider-confirmed refund as a distinct immutable event.

    A refund reverses cash/bank collection rather than mutating the original
    payment. Per-portion ``RefundApplication`` evidence prevents multiple
    partial refunds from ever consuming the same deposit or folio allocation.
    """
    from apps.payments.models import Refund

    refund = (
        Refund.objects.select_for_update()
        .select_related("payment", "payment__booking", "payment__user", "booking")
        .get(pk=refund.pk)
    )
    if refund.status != Refund.Status.PROCESSED:
        raise ValidationError("Only a provider-confirmed processed refund can post to finance.")
    if Decimal(refund.amount or 0) <= Decimal("0.00"):
        raise ValidationError("A processed refund must have a positive amount.")

    payment = refund.payment
    ensure_payment_collection(payment=payment, actor=actor or refund.requested_by)
    source_key = f"refund:{refund.pk}"
    existing = FinancialTransaction.objects.select_for_update().filter(source_key=source_key).first()
    if existing:
        return existing, False

    prior_applications = list(
        RefundApplication.objects.select_for_update()
        .filter(payment=payment)
        .only("payment_allocation_id", "amount")
    )
    applied_by_allocation: dict[int, Decimal] = {}
    unapplied_refunded = Decimal("0.00")
    for application in prior_applications:
        if application.payment_allocation_id:
            applied_by_allocation[application.payment_allocation_id] = (
                applied_by_allocation.get(application.payment_allocation_id, Decimal("0.00"))
                + application.amount
            )
        else:
            unapplied_refunded += application.amount

    remaining = Decimal(refund.amount).quantize(Decimal("0.01"))
    lines = []
    postings = []
    application_specs = []
    allocations = list(
        PaymentAllocation.objects.select_for_update()
        .select_related("folio")
        .filter(payment=payment)
        .order_by("created_at", "pk")
    )
    original_allocated = sum((allocation.amount for allocation in allocations), Decimal("0.00"))
    for allocation in allocations:
        if remaining <= Decimal("0.00"):
            break
        available = allocation.amount - applied_by_allocation.get(allocation.pk, Decimal("0.00"))
        portion = min(remaining, available)
        if portion <= Decimal("0.00"):
            continue
        line_index = len(lines)
        lines.append(
            {
                "account_code": accounting.ACCOUNTS_RECEIVABLE,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": portion,
                "folio": allocation.folio,
                "description": f"Refund against payment {payment.reference}",
                "metadata": {"payment_allocation_id": allocation.pk, "refund_id": refund.pk},
            }
        )
        postings.append(
            {
                "line_index": line_index,
                "folio": allocation.folio,
                "kind": "REFUND",
                "effect": "DEBIT",
                "amount": portion,
                "description": f"Refund {payment.reference}",
                "source_reference": source_key,
            }
        )
        application_specs.append((allocation, portion))
        remaining -= portion

    if remaining > Decimal("0.00"):
        deposit_available = Decimal(payment.amount) - original_allocated - unapplied_refunded
        if remaining > deposit_available:
            raise LedgerIntegrityError(
                "Refund exceeds the unrefunded balance of the payment's deposits and folio applications."
            )
        lines.append(
            {
                "account_code": accounting.GUEST_DEPOSITS,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": remaining,
                "description": f"Return unapplied guest deposit {payment.reference}",
            }
        )
        application_specs.append((None, remaining))

    lines.append(
        {
            "account_code": _collection_asset_account(payment),
            "direction": FinancialLine.Direction.CREDIT,
            "amount": refund.amount,
            "description": f"Refund issued for {payment.reference}",
        }
    )
    financial_transaction, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.REFUND,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor or refund.requested_by,
        source_reference=refund.booking.booking_reference,
        external_reference=refund.paystack_refund_id or refund.paystack_refund_reference or payment.reference,
        narrative=f"Refund for payment {payment.reference}",
        metadata={
            "refund_id": refund.pk,
            "payment_id": payment.pk,
            "payment_reference": payment.reference,
            "booking_reference": refund.booking.booking_reference,
        },
        currency=refund.currency,
        business_date=(refund.processed_at.date() if refund.processed_at else None),
        occurred_at=refund.processed_at,
        lines=lines,
        postings=postings,
    )
    if created:
        for allocation, portion in application_specs:
            suffix = allocation.pk if allocation else "deposit"
            RefundApplication.objects.create(
                refund=refund,
                payment=payment,
                payment_allocation=allocation,
                transaction=financial_transaction,
                amount=portion,
                source_key=f"refund-application:{refund.pk}:{suffix}",
            )
    return financial_transaction, created


# ---------------------------------------------------------------------------
# Desk settlement of the WHOLE guest bill (stay balance + folio charges)
# ---------------------------------------------------------------------------
def _validate_cash_session(reference, actor):
    """A cash collection's drawer must be the acting cashier's own open one."""
    from apps.finance.models import CashSession

    session = CashSession.objects.select_for_update().filter(reference=reference).first()
    if session is None:
        raise ValidationError({"cash_session_reference": "No cash session has that reference."})
    if session.status != CashSession.Status.OPEN:
        raise ValidationError({"cash_session_reference": "The cash session is not open."})
    if actor is not None and session.cashier_id != actor.pk:
        raise PermissionDenied("Cash must be taken into the acting cashier's own open drawer.")
    return session


@transaction.atomic
def settle_outstanding_balance(
    *,
    booking,
    actor,
    amount,
    provider,
    notes="",
    request=None,
    include_unbilled=True,
    cash_session_reference="",
):
    """Collect money at the desk for the guest's FULL bill and show it on the folio.

    ``payments.record_offline_payment`` settles the STAY projection only. Once a
    booking is paid it refuses further money (``PaymentAlreadyCompletedError``)
    and it caps the amount at ``booking.amount_due`` — so a guest who had paid
    for the room but still owed restaurant, bar, room-service or laundry money
    posted to their folio could not pay it at all, and the folio never moved.
    That is the gap this closes, in one transaction:

      * any unbilled in-house request on the stay (laundry, and any room-service
        order that was somehow delivered without its folio charge) is billed to
        the folio first, so no liability is left unstated and nothing is charged
        twice (both billing paths are idempotent),
      * the folio-charge part of the money is APPLIED TO THE FOLIO, so the
        folio's balance drops the moment the desk records it,
      * only the remainder is credited to the stay projection.

    The ``Payment`` row is written against the acting staff member, so the money
    is attributed to the person who received and recorded it — never to a
    derived or client-supplied identity. The caller re-reads the folio from the
    same response, so what the desk sees is what the ledger holds.
    """
    from apps.accounts.capabilities import has_capability
    from apps.bookings.models import Booking
    from apps.bookings.services import booking_service
    from apps.guest_services.models import LaundryOrder
    from apps.guest_services.services.laundry_service import bill_laundry_order
    from apps.pos.models import PosOrder
    from apps.pos.services.order_service import bill_unbilled_order_charge
    from apps.finance.services.folio_service import (
        booking_folio_charge_balance,
        get_or_create_main_folio_for_stay,
    )

    booking = (
        Booking.objects.select_for_update().select_related("guest", "room_type").get(pk=booking.pk)
    )
    booking_service.refresh_expired_pending(booking)
    if booking.status not in (
        Booking.Status.PENDING,
        Booking.Status.CONFIRMED,
        Booking.Status.CHECKED_IN,
    ):
        raise ValidationError(
            f"A booking with status {booking.get_status_display()} cannot take a desk payment."
        )

    provider = str(provider or "").upper()
    if provider not in Payment.Provider.values:
        raise ValidationError({"provider": "Choose how the guest paid."})
    # Cash needs the acting cashier's own open drawer (same rule as every other
    # cash collection in the system), so the count reconciles at close.
    session = _validate_cash_session(cash_session_reference, actor) if cash_session_reference else None

    try:
        stay = booking.stay
    except Exception:  # noqa: BLE001 - a booking without an operational stay has no folio
        stay = None

    billed_requests: list[str] = []
    if include_unbilled and stay is not None:
        # Billing is idempotent (``bill once``) and refuses cancelled orders.
        unbilled_orders = list(
            LaundryOrder.objects.select_for_update()
            .filter(stay=stay, financial_transaction__isnull=True)
            .exclude(status=LaundryOrder.Status.CANCELLED)
            .order_by("created_at")
        )
        for order in unbilled_orders:
            if not has_capability(actor, "guest_request.manage"):
                raise PermissionDenied(
                    "Billing this guest's unbilled requests to the folio requires the "
                    "guest-request permission. Bill them from the Laundry page first."
                )
            bill_laundry_order(order=order, actor=actor)
            billed_requests.append(order.reference)
        unbilled_pos_orders = list(
            PosOrder.objects.select_for_update()
            .filter(stay=stay, charge_transaction__isnull=True)
            .exclude(
                status__in=[
                    PosOrder.Status.CANCELLED,
                    PosOrder.Status.VOIDED,
                    PosOrder.Status.DRAFT,
                ]
            )
            .order_by("created_at")
        )
        for order in unbilled_pos_orders:
            bill_unbilled_order_charge(order=order, actor=actor)
            billed_requests.append(order.reference)

    folio = None
    if stay is not None:
        folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=actor)

    stay_due = max(Decimal(booking.amount_due or Decimal("0.00")), Decimal("0.00"))
    folio_due = max(Decimal(booking_folio_charge_balance(booking=booking)), Decimal("0.00"))
    bill_total = (stay_due + folio_due).quantize(Decimal("0.01"))
    received = Decimal(str(amount)).quantize(Decimal("0.01"))
    if received <= Decimal("0.00"):
        raise ValidationError({"amount": "Enter the amount the guest actually handed over."})
    if bill_total <= Decimal("0.00"):
        raise PaymentAlreadyCompletedError()
    if received > bill_total:
        raise ValidationError(
            {
                "amount": (
                    f"Amount exceeds the bill of {booking.currency} {bill_total:.2f}. "
                    "Record what was received, not more."
                )
            }
        )

    payment = Payment.objects.create(
        booking=booking,
        user=actor,
        reference=_unique_payment_reference(),
        provider=provider,
        amount=received,
        currency=booking.currency,
        status=Payment.Status.SUCCESS,
        channel=provider.lower(),
        gateway_response=f"Collected at the front desk by {getattr(actor, 'email', '')}",
        paid_at=timezone.now(),
        notes=notes or "",
        metadata={
            "booking_reference": booking.booking_reference,
            "recorded_by": getattr(actor, "email", ""),
            "settled_via": "checkout_folio",
            # The bill as it stood the moment before this money arrived
            # (unbilled requests already included by the billing above).
            "bill_before_payment": money_plain(bill_total),
            "folio_balance_before_payment": money_plain(folio_due),
            "stay_balance_before_payment": money_plain(stay_due),
            "billed_requests": billed_requests,
            "cash_session_reference": getattr(session, "reference", ""),
        },
    )
    # Gross tender collection first (Dr cash/bank/card clearing, Cr guest deposits).
    ensure_payment_collection(payment=payment, actor=actor)

    # The folio part lands on the folio immediately — this is the money the
    # guest disputed at the gate, so it must stop being "outstanding" the second
    # the staff member records it.
    folio_part = min(received, folio_due).quantize(Decimal("0.01"))
    if folio_part > Decimal("0.00"):
        if folio is None:
            raise ValidationError("This guest has no folio to apply the payment to.")
        apply_payment_to_folio(payment=payment, folio=folio, actor=actor, amount=folio_part)

    stay_part = (received - folio_part).quantize(Decimal("0.01"))
    if stay_part > Decimal("0.00"):
        booking_service.register_successful_payment(booking, stay_part, request=request)

    from apps.audit.services import log_action

    log_action(
        actor=actor,
        action="PAYMENT_RECORDED",
        instance=payment,
        metadata={
            "reference": payment.reference,
            "booking": booking.booking_reference,
            "amount": money_plain(received),
            "provider": provider,
            "folio_portion": money_plain(folio_part),
            "stay_portion": money_plain(stay_part),
            "billed_requests": billed_requests,
        },
        request=request,
        summary=(
            f"Check-out settlement {payment.reference} recorded "
            f"({received:.2f} {booking.currency}; folio {folio_part:.2f})"
        ),
    )
    return payment, {
        "stay_balance_before": money_plain(stay_due),
        "folio_balance_before": money_plain(folio_due),
        "bill_total_before": money_plain(bill_total),
        "folio_portion": money_plain(folio_part),
        "stay_portion": money_plain(stay_part),
        "billed_requests": billed_requests,
    }


def money_plain(value) -> str:
    """Two-decimal string for a Decimal (service-layer twin of core.utils.money)."""
    return f"{Decimal(value).quantize(Decimal('0.01'))}"
