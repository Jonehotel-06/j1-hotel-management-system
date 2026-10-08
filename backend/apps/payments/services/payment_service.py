# apps/payments/services/payment_service.py
"""Payment orchestration — initialization, verification, webhooks, offline records.

Invariants guarded here:
* amounts always come from the BOOKING, never from the request body
* verification is idempotent (a retry is a read, not a re-credit)
* a booking only becomes CONFIRMED when verified money covers required_payment
* webhook signatures are validated before any processing
"""
import hashlib
import hmac
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import log_action
from apps.bookings.models import Booking
from apps.bookings.services import booking_service
from apps.core.exceptions import (
    BookingExpiredError,
    BookingStateError,
    PaymentAlreadyCompletedError,
    PaymentAmountMismatchError,
    PaymentError,
    PaymentGatewayError,
    PaymentNotConfiguredError,
)
from apps.core.emails import queue_email, send_email_safe
from apps.core.utils import generate_payment_reference, money
from apps.notifications.services import notify_staff, notify_users

from ..models import Payment, Refund
from . import paystack

logger = logging.getLogger("apps")

KOBO_PER_NAIRA = Decimal("100")


def _unique_payment_reference():
    for _ in range(10):
        reference = generate_payment_reference()
        if not Payment.objects.filter(reference=reference).exists():
            return reference
    raise RuntimeError("Could not allocate a unique payment reference.")


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
INITIALIZATION_LEASE = timedelta(minutes=2)
PROCESSING_GATEWAY_STATUSES = {"pending", "ongoing", "processing", "queued"}
FAILED_GATEWAY_STATUSES = {"failed", "abandoned", "reversed"}


def _amount_to_kobo(amount: Decimal) -> int:
    """Convert a two-decimal NGN amount without passing through float."""
    value = Decimal(amount).quantize(Decimal("0.01")) * KOBO_PER_NAIRA
    return int(value.to_integral_exact())


def _initialization_payload(payment):
    return {
        "reference": payment.reference,
        "booking_reference": payment.booking.booking_reference,
        "authorization_url": payment.metadata.get("authorization_url"),
        "amount": money(payment.amount),
        "currency": payment.currency,
        "reused": True,
    }


def _reserve_initialization(*, booking, user):
    """Short DB transaction which allocates (or reuses) one pending attempt.

    The booking row is the serialization point. The external Paystack request
    deliberately happens after this transaction commits.
    """
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update()
            .select_related("guest")
            .get(pk=booking.pk)
        )
        booking_service.refresh_expired_pending(booking)
        booking.refresh_from_db()
        if booking.status == Booking.Status.EXPIRED or booking.is_expired_pending:
            raise BookingExpiredError()
        if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
            raise BookingStateError(
                f"A booking with status {booking.get_status_display()} cannot accept payments."
            )
        if booking.amount_due <= 0:
            raise PaymentAlreadyCompletedError()
        if booking.currency.upper() != "NGN":
            raise PaymentError("Online payment is only available for NGN bookings.")

        charge = (
            booking.required_payment - booking.amount_paid
            if booking.amount_paid < booking.required_payment
            else booking.amount_due
        ).quantize(Decimal("0.01"))

        payment = (
            Payment.objects.select_for_update()
            .filter(
                booking=booking,
                provider=Payment.Provider.PAYSTACK,
                status=Payment.Status.PENDING,
                amount=charge,
                currency=booking.currency,
            )
            .order_by("-created_at")
            .first()
        )
        if payment and payment.metadata.get("authorization_url"):
            return booking, payment, False

        now = timezone.now()
        if payment:
            state = payment.metadata.get("initialization_state")
            started_raw = payment.metadata.get("initialization_started_at")
            try:
                from datetime import datetime
                started = datetime.fromisoformat(started_raw) if started_raw else None
            except (TypeError, ValueError):
                started = None
            if started and timezone.is_naive(started):
                started = timezone.make_aware(started)
            if state == "INITIALIZING" and started and now - started < INITIALIZATION_LEASE:
                raise PaymentError("Payment initialization is already in progress. Please wait a moment.")
        else:
            payment = Payment.objects.create(
                booking=booking,
                user=user,
                reference=_unique_payment_reference(),
                provider=Payment.Provider.PAYSTACK,
                amount=charge,
                currency=booking.currency,
                status=Payment.Status.PENDING,
                metadata={"booking_reference": booking.booking_reference},
            )

        payment.user = user or payment.user
        payment.metadata = {
            **payment.metadata,
            "initialization_state": "INITIALIZING",
            "initialization_started_at": now.isoformat(),
        }
        payment.save(update_fields=["user", "metadata", "updated_at"])
        return booking, payment, True


def initialize_booking_payment(*, booking: Booking, user, request=None):
    """Initialize one idempotent Paystack attempt without a long DB lock."""
    if not getattr(settings, "PAYSTACK_SECRET_KEY", "").strip():
        raise PaymentNotConfiguredError()
    booking, payment, should_call_gateway = _reserve_initialization(booking=booking, user=user)
    if not should_call_gateway:
        logger.info("Reused initialized payment: %s booking=%s", payment.reference, booking.booking_reference)
        return _initialization_payload(payment)

    try:
        paystack_data = paystack.initialize_transaction(
            email=booking.guest.email,
            amount_kobo=_amount_to_kobo(payment.amount),
            reference=payment.reference,
            callback_url=settings.PAYMENT_CALLBACK_URL,
            metadata={
                "booking_reference": booking.booking_reference,
                "payment_reference": payment.reference,
                "room_type_id": booking.room_type_id,
            },
        )
    except (PaymentGatewayError, PaymentNotConfiguredError):
        # Preserve this reference for a safe retry. A timeout is ambiguous: the
        # gateway may have accepted it, so creating a fresh attempt could lead
        # to two payable transactions.
        with transaction.atomic():
            current = Payment.objects.select_for_update().get(pk=payment.pk)
            current.metadata = {
                **current.metadata,
                "initialization_state": "RETRYABLE_ERROR",
                "initialization_failed_at": timezone.now().isoformat(),
            }
            current.save(update_fields=["metadata", "updated_at"])
        logger.exception("Payment initialization failed: reference=%s booking=%s", payment.reference, booking.booking_reference)
        raise

    if (
        not isinstance(paystack_data, dict)
        or not paystack_data.get("authorization_url")
        or not paystack_data.get("access_code")
        or paystack_data.get("reference") != payment.reference
    ):
        logger.error("Invalid Paystack initialization data: reference=%s booking=%s", payment.reference, booking.booking_reference)
        raise PaymentGatewayError("Unable to start payment. Please try again.")

    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related("booking").get(pk=payment.pk)
        payment.metadata = {
            **payment.metadata,
            "initialization_state": "READY",
            "access_code": paystack_data["access_code"],
            "authorization_url": paystack_data["authorization_url"],
            "provider_reference": paystack_data["reference"],
        }
        payment.save(update_fields=["metadata", "updated_at"])

    log_action(
        actor=user, action="PAYMENT_INITIALIZED", instance=payment,
        metadata={"reference": payment.reference, "booking": booking.booking_reference,
                  "amount": str(payment.amount)}, request=request,
    )
    logger.info("Payment initialized: reference=%s booking=%s amount=%s", payment.reference, booking.booking_reference, payment.amount)
    payload = _initialization_payload(payment)
    payload["reused"] = False
    return payload


# ---------------------------------------------------------------------------
# Verification (idempotent) — shared by browser verification and webhook
# ---------------------------------------------------------------------------
def _verified_receipt_payload(payment: Payment, transaction_status=None):
    booking = payment.booking
    payload = {
        "payment_reference": payment.reference,
        "booking_reference": booking.booking_reference,
        "transaction_status": transaction_status or payment.status,
        "booking_status": booking.status,
        "payment_status": booking.payment_status,
        "amount_paid_this_transaction": money(payment.amount) if payment.status == Payment.Status.SUCCESS else "0.00",
        "booking_amount_paid": money(booking.amount_paid),
        "booking_amount_due": money(booking.amount_due),
        "booking_total": money(booking.total_amount),
        "currency": booking.currency,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    }
    if payment.status == Payment.Status.SUCCESS:
        from apps.bookings.serializers import ReceiptSerializer
        payload["receipt"] = ReceiptSerializer().to_representation(booking)
    return payload


def _parse_paid_at(value):
    if not value:
        return timezone.now()
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
    except (ValueError, TypeError):
        return timezone.now()


def _int_or_none(value):
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _paystack_hotel_amount_kobo(data: dict):
    """Return Paystack's original merchant-requested amount in kobo.

    Some Paystack account/API responses expose ``amount`` as a fee-inclusive
    processed amount even though this application initialized the transaction
    with the exact hotel amount. In that response shape ``requested_amount`` is
    the authoritative original charge request. Fall back to ``amount - fees``
    only for older Paystack responses that omit it. Payment.amount and every
    guest-facing total remain the exact backend-created hotel amount.
    """
    requested = _int_or_none(data.get("requested_amount"))
    if requested is None:
        requested = _int_or_none(data.get("requestedAmount"))
    if requested is not None:
        return requested
    return _int_or_none(data.get("amount"))


def process_verification(*, reference, request=None, triggered_by="api"):
    """Verify with Paystack, validate reference/amount/currency and reconcile."""
    payment = (
        Payment.objects.select_related("booking", "booking__guest", "booking__guest__user")
        .filter(reference=reference, provider=Payment.Provider.PAYSTACK)
        .first()
    )
    if payment is None:
        from rest_framework.exceptions import NotFound
        raise NotFound("Payment not found.")
    if payment.status == Payment.Status.SUCCESS:
        # A retry after a deployment/cutover may encounter a successful legacy
        # projection that has not yet been bridged. The finance service is
        # source-key idempotent, so this is a cheap read on ordinary retries.
        from apps.finance.services.payment_collection_service import ensure_payment_collection

        ensure_payment_collection(payment=payment, actor=payment.user)
        return _verified_receipt_payload(payment, "success")

    paystack_payload = paystack.verify_transaction(reference)
    data = paystack_payload["data"]
    gateway_status = str(data.get("status") or "").lower()

    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .select_related("booking", "booking__guest", "booking__guest__user")
            .get(pk=payment.pk)
        )
        if payment.status == Payment.Status.SUCCESS:
            from apps.finance.services.payment_collection_service import ensure_payment_collection

            ensure_payment_collection(payment=payment, actor=payment.user)
            return _verified_receipt_payload(payment, "success")

        provider_reference = str(data.get("reference") or "")
        if provider_reference != payment.reference:
            logger.error("Paystack reference mismatch: expected=%s got=%s", payment.reference, provider_reference)
            raise PaymentAmountMismatchError("The verified payment reference does not match.")

        if gateway_status in PROCESSING_GATEWAY_STATUSES or gateway_status not in FAILED_GATEWAY_STATUSES | {"success"}:
            payment.status = Payment.Status.PENDING
            payment.gateway_response = str(data.get("gateway_response") or paystack_payload.get("message") or "")[:255]
            payment.save(update_fields=["status", "gateway_response", "updated_at"])
            logger.info("Payment remains pending: reference=%s gateway_status=%s", reference, gateway_status)
            return _verified_receipt_payload(payment, gateway_status or "pending")

        if gateway_status in FAILED_GATEWAY_STATUSES:
            payment.status = Payment.Status.FAILED
            payment.gateway_response = str(data.get("gateway_response") or paystack_payload.get("message") or "")[:255]
            payment.save(update_fields=["status", "gateway_response", "updated_at"])
            booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
            if booking.amount_paid == 0 and booking.payment_status != Booking.PaymentStatus.PAID:
                booking.payment_status = Booking.PaymentStatus.FAILED
                booking.save(update_fields=["payment_status", "updated_at"])
            payment.booking = booking
            logger.warning("Payment terminal failure: reference=%s gateway_status=%s", reference, gateway_status)
            return _verified_receipt_payload(payment, gateway_status)

        paid_kobo = _int_or_none(data.get("amount"))
        paystack_fees_kobo = _int_or_none(data.get("fees"))
        provider_hotel_amount_kobo = _paystack_hotel_amount_kobo(data)
        expected_kobo = _amount_to_kobo(payment.amount)
        paid_currency = str(data.get("currency") or "").upper()
        # Accept the exact processed amount, Paystack's explicit original
        # requested amount, or the legacy fee-inclusive response shape. At
        # least one provider-derived value must exactly match our immutable
        # server amount; browser values are never consulted.
        legacy_net_kobo = (
            paid_kobo - paystack_fees_kobo
            if paid_kobo is not None and paystack_fees_kobo is not None
            and paystack_fees_kobo > 0 and paid_kobo > paystack_fees_kobo
            else None
        )
        amount_matches = expected_kobo in {paid_kobo, provider_hotel_amount_kobo, legacy_net_kobo}
        if not amount_matches or paid_currency != payment.currency.upper():
            logger.error(
                "Payment amount/currency mismatch: reference=%s expected=%s/%s got=%s/%s (provider_hotel_amount=%s fees=%s)",
                reference, expected_kobo, payment.currency, paid_kobo, paid_currency,
                provider_hotel_amount_kobo, paystack_fees_kobo,
            )
            payment.metadata = {
                **payment.metadata,
                "verification_mismatch": {
                    "expected_amount_kobo": expected_kobo,
                    "received_amount_kobo": paid_kobo,
                    "received_requested_amount_kobo": provider_hotel_amount_kobo,
                    "received_fees_kobo": paystack_fees_kobo,
                    "received_currency": paid_currency,
                },
            }
            payment.save(update_fields=["metadata", "updated_at"])
            raise PaymentAmountMismatchError()

        booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
        provider_paid_at = _parse_paid_at(data.get("paid_at") or data.get("paidAt"))
        if booking.status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED):
            logger.error("Successful payment for non-payable booking: reference=%s booking_status=%s", reference, booking.status)
            raise BookingStateError("This booking is no longer able to accept payment. Please contact the hotel.")
        if booking.is_expired_pending:
            # Hold deadline passed but the periodic sweep has not flipped this
            # booking yet. The provider's authoritative charge timestamp
            # decides, under the same row lock the sweep takes:
            #   * charged within the hold window → the guest beat the deadline
            #     (the room hold still blocked inventory until expires_at, so
            #     no double-booking is possible) — confirm normally;
            #   * charged after the window → the hold is already released; a
            #     late charge must never resurrect an expired hold whose room
            #     may have been resold. The booking stays expired (the lazy
            #     flip/beat sweep persists the EXPIRED state) and staff review
            #     the settled charge.
            if not (booking.expires_at and provider_paid_at and provider_paid_at <= booking.expires_at):
                logger.error(
                    "Successful payment charged after hold expiry: reference=%s expires_at=%s paid_at=%s",
                    reference, booking.expires_at, provider_paid_at,
                )
                raise BookingStateError("This booking is no longer able to accept payment. Please contact the hotel.")

        payment.status = Payment.Status.SUCCESS
        payment.paid_at = provider_paid_at
        payment.channel = str(data.get("channel") or "")[:40]
        payment.gateway_response = str(data.get("gateway_response") or "")[:255]
        payment.transaction_id = str(data.get("id") or "")[:60]
        payment.metadata = {
            **payment.metadata,
            "paystack_id": data.get("id"),
            "channel": payment.channel,
            "paystack_amount_kobo": paid_kobo,
            "paystack_hotel_amount_kobo": expected_kobo,
            "paystack_requested_amount_kobo": provider_hotel_amount_kobo,
            # Stored only for settlement diagnostics. Neither field changes
            # Payment.amount, the booking balance, emails, or receipts.
            "paystack_fees_kobo": paystack_fees_kobo,
            "paystack_reported_amount_includes_fee": bool(
                paid_kobo is not None and provider_hotel_amount_kobo is not None
                and paid_kobo > provider_hotel_amount_kobo
            ),
        }
        payment.save()
        booking = booking_service.register_successful_payment(booking, payment.amount, request=request)
        payment.booking = booking
        # Ledger collection is part of the same durable state transition as the
        # legacy payment/booking projections. If it cannot balance/post, neither
        # projection is allowed to claim success.
        from apps.finance.services.payment_collection_service import ensure_payment_collection

        ensure_payment_collection(payment=payment, actor=payment.user)

    # Only the transaction winner reaches these side effects. Delivery helpers
    # log/swallow their own errors, so payment truth is never rolled back.
    log_action(
        actor=payment.user, action="PAYMENT_VERIFIED", instance=payment,
        metadata={"reference": reference, "booking": booking.booking_reference,
                  "amount": str(payment.amount), "channel": payment.channel,
                  "triggered_by": triggered_by}, request=request,
    )
    notify_staff(
        type="PAYMENT_SUCCESS", title=f"Payment received: {booking.booking_reference}",
        message=f"{booking.currency} {money(payment.amount)} via {payment.channel or 'Paystack'}.",
        link=booking_service.staff_booking_link(booking),
    )
    if booking.guest.user_id:
        notify_users(
            [booking.guest.user], type="PAYMENT_SUCCESS",
            title=f"Payment confirmed for {booking.booking_reference}",
            message=f"We received {booking.currency} {money(payment.amount)}.",
            link=booking_service.guest_booking_link(booking),
        )
    if booking.status == Booking.Status.CONFIRMED:
        # queue_email defers to the payment transaction's commit internally.
        booking_service._send_confirmation_email(booking)
    logger.info("Payment verified: reference=%s booking=%s amount=%s status=%s", reference, booking.booking_reference, payment.amount, booking.payment_status)
    payment.refresh_from_db()
    return _verified_receipt_payload(payment, "success")


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
REFUND_EVENT_TO_STATUS = {
    "refund.pending": Refund.Status.PENDING,
    "refund.processing": Refund.Status.PROCESSING,
    "refund.processed": Refund.Status.PROCESSED,
    "refund.failed": Refund.Status.FAILED,
    "refund.needs-attention": Refund.Status.NEEDS_ATTENTION,
}
REFUND_ACTIVE_STATUSES = [
    Refund.Status.PENDING,
    Refund.Status.PROCESSING,
    Refund.Status.NEEDS_ATTENTION,
]


def _decimal_from_kobo(value):
    try:
        return (Decimal(int(value)) / KOBO_PER_NAIRA).quantize(Decimal("0.01"))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _normalise_refund_status(value, event=None):
    if event in REFUND_EVENT_TO_STATUS:
        return REFUND_EVENT_TO_STATUS[event]
    status_value = str(value or "").replace("-", "_").upper()
    aliases = {
        "PENDING": Refund.Status.PENDING,
        "PROCESSING": Refund.Status.PROCESSING,
        "PROCESSED": Refund.Status.PROCESSED,
        "SUCCESS": Refund.Status.PROCESSED,
        "SUCCESSFUL": Refund.Status.PROCESSED,
        "FAILED": Refund.Status.FAILED,
        "NEEDS_ATTENTION": Refund.Status.NEEDS_ATTENTION,
        "NEEDS-ATTENTION": Refund.Status.NEEDS_ATTENTION,
    }
    return aliases.get(status_value, Refund.Status.PENDING)


def _refund_transaction_reference(data):
    transaction_data = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    return (
        transaction_data.get("reference")
        or data.get("transaction_reference")
        or data.get("transactionReference")
        or data.get("payment_reference")
    )


def _refund_transaction_id(data):
    transaction_data = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    value = transaction_data.get("id") or data.get("transaction_id") or data.get("transactionId")
    return str(value or "")[:60]


def _refund_provider_reference(data):
    # Paystack primarily identifies refunds by id; some API versions include a
    # refund reference. Never use this as the original payment reference.
    return str(
        data.get("refund_reference")
        or data.get("refundReference")
        or data.get("reference")
        or ""
    )[:120]


def _refund_provider_id(data):
    value = data.get("id") or data.get("refund_id") or data.get("refundId")
    return str(value or "")[:80]


def _provider_amount(data):
    if "amount" in data:
        return _decimal_from_kobo(data.get("amount"))
    return None


def _active_refund_total(payment, *, exclude_pk=None):
    qs = Refund.objects.filter(payment=payment, status__in=REFUND_ACTIVE_STATUSES)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return (qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")).quantize(Decimal("0.01"))


def _processed_refund_total(payment):
    return (
        Refund.objects.filter(payment=payment, status=Refund.Status.PROCESSED)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))


def _reconcile_payment_and_booking_refunds(payment, *, request=None):
    """Reflect Paystack-confirmed refunds without mutating gross amount_paid."""
    payment = Payment.objects.select_related("booking").get(pk=payment.pk)
    processed_for_payment = _processed_refund_total(payment)
    if processed_for_payment > 0:
        new_payment_status = (
            Payment.Status.REFUNDED
            if processed_for_payment >= Decimal(payment.amount or 0).quantize(Decimal("0.01"))
            else Payment.Status.PARTIALLY_REFUNDED
        )
        if payment.status != new_payment_status:
            payment.status = new_payment_status
            payment.save(update_fields=["status", "updated_at"])

    booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
    processed_for_booking = (
        Refund.objects.filter(booking=booking, status=Refund.Status.PROCESSED)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    ).quantize(Decimal("0.01"))
    update_fields = []
    if Decimal(booking.refund_amount or 0).quantize(Decimal("0.01")) != processed_for_booking:
        booking.refund_amount = processed_for_booking
        update_fields.append("refund_amount")
    if processed_for_booking > 0:
        new_booking_payment_status = (
            Booking.PaymentStatus.REFUNDED
            if processed_for_booking >= Decimal(booking.amount_paid or 0).quantize(Decimal("0.01"))
            else Booking.PaymentStatus.PARTIALLY_REFUNDED
        )
        if booking.payment_status != new_booking_payment_status:
            booking.payment_status = new_booking_payment_status
            update_fields.append("payment_status")
    if update_fields:
        update_fields.append("updated_at")
        booking.save(update_fields=update_fields)

    return processed_for_payment, processed_for_booking


@transaction.atomic
def _mark_refund_status(refund, new_status, *, provider_data=None, failure_reason="", request=None, event_name="", force_notify=False):
    """Apply a status transition idempotently and send side effects once."""
    previous_status = refund.status
    now = timezone.now()
    provider_data = provider_data or {}
    metadata = {**(refund.metadata or {})}
    if event_name:
        metadata["latest_paystack_event"] = event_name
    metadata["latest_provider_status"] = provider_data.get("status", "")
    metadata["latest_provider_seen_at"] = now.isoformat()
    provider_amount = _provider_amount(provider_data)
    if provider_amount is not None and provider_amount != Decimal(refund.amount or 0).quantize(Decimal("0.01")):
        metadata["provider_amount_mismatch"] = {
            "local_amount": str(refund.amount),
            "provider_amount": str(provider_amount),
        }

    refund.status = new_status
    refund.metadata = metadata
    if _refund_provider_id(provider_data):
        refund.paystack_refund_id = _refund_provider_id(provider_data)
    if _refund_provider_reference(provider_data):
        refund.paystack_refund_reference = _refund_provider_reference(provider_data)
    if _refund_transaction_id(provider_data):
        refund.paystack_transaction_id = _refund_transaction_id(provider_data)
    if _refund_transaction_reference(provider_data):
        refund.paystack_transaction_reference = str(_refund_transaction_reference(provider_data))[:120]
    if new_status in (Refund.Status.PENDING, Refund.Status.PROCESSING) and not refund.submitted_at:
        refund.submitted_at = now
    if new_status == Refund.Status.PROCESSED and not refund.processed_at:
        refund.processed_at = now
        if provider_data.get("refunded_at"):
            refund.metadata = {**refund.metadata, "paystack_refunded_at": str(provider_data.get("refunded_at"))}
    if new_status == Refund.Status.FAILED:
        refund.failed_at = refund.failed_at or now
        refund.failure_reason = (failure_reason or provider_data.get("failure_reason") or provider_data.get("gateway_response") or "Paystack marked this refund failed.")[:500]
    refund.save()
    if new_status == Refund.Status.PROCESSED:
        # A processed provider refund is financial truth. Post its distinct,
        # source-key-idempotent ledger reversal before changing aggregate
        # projection fields or sending any notification.
        from apps.finance.services.payment_collection_service import ensure_processed_refund

        ensure_processed_refund(refund=refund, actor=refund.requested_by)

    with transaction.atomic():
        _reconcile_payment_and_booking_refunds(refund.payment, request=request)
        # Keep the cancellation request summary in sync, but avoid a module-level
        # import to prevent a service cycle at import time.
        if refund.cancellation_request_id:
            try:
                from apps.enquiries import services as enquiry_services

                cancellation_request = refund.cancellation_request
                enquiry_services.sync_refund_summary(cancellation_request)
            except Exception as exc:  # pragma: no cover - defensive logging only
                logger.warning("Could not sync cancellation refund summary (%s)", exc.__class__.__name__)

    if previous_status != new_status or force_notify:
        _notify_refund_status(refund.pk, previous_status, new_status)
    return refund


def _notify_refund_status(refund_pk, previous_status, new_status):
    try:
        refund = Refund.objects.select_related("booking", "booking__guest", "booking__guest__user", "cancellation_request").get(pk=refund_pk)
    except Refund.DoesNotExist:
        return

    status_titles = {
        Refund.Status.PENDING: "Refund request submitted to Paystack",
        Refund.Status.PROCESSING: "Refund is processing at Paystack",
        Refund.Status.PROCESSED: "Refund processed by Paystack",
        Refund.Status.FAILED: "Refund failed at Paystack",
        Refund.Status.NEEDS_ATTENTION: "Refund needs attention in Paystack",
    }
    notification_type = {
        Refund.Status.PENDING: "REFUND_PENDING",
        Refund.Status.PROCESSING: "REFUND_PROCESSING",
        Refund.Status.PROCESSED: "REFUND_PROCESSED",
        Refund.Status.FAILED: "REFUND_FAILED",
        Refund.Status.NEEDS_ATTENTION: "REFUND_NEEDS_ATTENTION",
    }.get(new_status, "REFUND_PROCESSING")
    title = f"{status_titles.get(new_status, 'Refund updated')}: {refund.booking.booking_reference}"
    message = (
        f"Refund {refund.pk} for {refund.booking.booking_reference} is {refund.get_status_display()} "
        f"({refund.currency} {money(refund.amount)})."
    )
    notify_staff(type=notification_type, title=title, message=message, link=booking_service.staff_booking_link(refund.booking))

    if refund.booking.guest.user_id:
        notify_users(
            [refund.booking.guest.user],
            type=notification_type,
            title=status_titles.get(new_status, "Refund updated"),
            message=(
                f"Refund status for booking {refund.booking.booking_reference}: {refund.get_status_display()}."
            ),
            link=booking_service.guest_booking_link(refund.booking),
        )

    if not refund.booking.guest.email:
        return

    # One guest email per status per refund, even if webhooks are redelivered.
    events = refund.metadata.get("guest_email_events", []) if isinstance(refund.metadata, dict) else []
    event_key = f"guest:{new_status}"
    if event_key in events:
        return

    from apps.core.email_design import render_notice_email
    from apps.core.formatting import format_money
    from apps.hotel.models import HotelSettings

    if new_status in (Refund.Status.PENDING, Refund.Status.PROCESSING):
        title = "Your refund is being processed"
        status_value = "Submitted to Paystack — in progress"
        paragraphs = [
            f"A refund of {format_money(refund.amount, refund.currency)} for booking "
            f"{refund.booking.booking_reference} has been submitted to Paystack and is not complete yet. "
            "We will notify you when Paystack confirms the final result.",
        ]
    elif new_status == Refund.Status.PROCESSED:
        title = "Your refund has been processed"
        status_value = "Processed by Paystack"
        paragraphs = [
            f"Paystack has confirmed that your refund of {format_money(refund.amount, refund.currency)} "
            f"for booking {refund.booking.booking_reference} has been processed.",
            "Your bank or card issuer may take additional time to reflect the amount in your account.",
        ]
    elif new_status == Refund.Status.FAILED:
        title = "Refund update — action in progress"
        status_value = "Could not be completed automatically"
        paragraphs = [
            f"Paystack could not complete the refund request for booking "
            f"{refund.booking.booking_reference}. The hotel team will review it and "
            "contact you with the next step.",
        ]
    else:
        title = "Refund update — under review"
        status_value = "Needs additional review"
        paragraphs = [
            f"Your refund request for booking {refund.booking.booking_reference} needs "
            "additional review. The hotel team will contact you with the next step.",
        ]
    hotel = HotelSettings.get_settings()
    text_body, html_body = render_notice_email(
        category="Refund update",
        title=title,
        greeting=f"Hello {refund.booking.guest.first_name},",
        paragraphs=paragraphs,
        details=[
            {"label": "Booking reference", "value": refund.booking.booking_reference},
            {"label": "Refund amount", "value": format_money(refund.amount, refund.currency)},
            {"label": "Status", "value": status_value},
        ],
        footnote=(
            "This message is about the hotel booking refund. Paystack may also send "
            "its own separate provider notification."
        ),
        preheader=f"Refund update for booking {refund.booking.booking_reference}.",
    )
    queue_email(
        subject=f"Refund update for {refund.booking.booking_reference} — {hotel.hotel_name}",
        message=text_body,
        html_message=html_body,
        recipients=[refund.booking.guest.email],
        kind="REFUND",
        booking_reference=refund.booking.booking_reference,
    )
    try:
        events.append(event_key)
        refund.metadata = {**refund.metadata, "guest_email_events": events}
        refund.save(update_fields=["metadata", "updated_at"])
    except Exception as exc:  # pragma: no cover - email already sent; log only
        logger.warning("Could not record refund email event (%s)", exc.__class__.__name__)


def _find_payment_for_refund_payload(data):
    reference = _refund_transaction_reference(data)
    transaction_id = _refund_transaction_id(data)
    qs = Payment.objects.select_related("booking", "booking__guest")
    if reference:
        payment = qs.filter(reference=reference, provider=Payment.Provider.PAYSTACK).first()
        if payment:
            return payment
    if transaction_id:
        payment = qs.filter(transaction_id=transaction_id, provider=Payment.Provider.PAYSTACK).first()
        if payment:
            return payment
    return None


def _find_or_create_refund_from_provider(payment, data, *, event_status):
    provider_id = _refund_provider_id(data)
    provider_reference = _refund_provider_reference(data)
    amount = _provider_amount(data) or Decimal("0.00")

    qs = Refund.objects.select_for_update().select_related("payment", "booking", "cancellation_request")
    refund = None
    if provider_id:
        refund = qs.filter(paystack_refund_id=provider_id).first()
    if refund is None and provider_reference:
        refund = qs.filter(paystack_refund_reference=provider_reference).first()
    if refund is None:
        possible = qs.filter(payment=payment)
        if amount > 0:
            possible = possible.filter(amount=amount)
        fallback_statuses = REFUND_ACTIVE_STATUSES + [Refund.Status.FAILED]
        if not provider_id and not provider_reference:
            # If Paystack ever redelivers a sparse test/sandbox-style payload
            # without a refund id/reference, the safest idempotency key left is
            # payment + amount + latest processed row. Real Paystack payloads
            # include provider identifiers and are matched above.
            fallback_statuses = fallback_statuses + [Refund.Status.PROCESSED]
        refund = possible.filter(status__in=fallback_statuses).order_by("-created_at").first()
    if refund is None:
        refund = Refund.objects.create(
            booking=payment.booking,
            payment=payment,
            amount=amount or Decimal("0.00"),
            currency=str(data.get("currency") or payment.currency or "NGN")[:3].upper(),
            status=Refund.Status.PENDING,
            paystack_transaction_id=payment.transaction_id or _refund_transaction_id(data),
            paystack_transaction_reference=payment.reference,
            paystack_refund_id=provider_id,
            paystack_refund_reference=provider_reference,
            metadata={"created_from_paystack_webhook": True},
        )
    return refund


def initiate_cancellation_refund(*, enquiry, staff_user, customer_note="", merchant_note="", request=None):
    """Submit an approved cancellation refund to Paystack from backend only."""
    from apps.enquiries.models import Enquiry
    from apps.enquiries import services as enquiry_services

    with transaction.atomic():
        enquiry = (
            Enquiry.objects.select_for_update()
            .select_related("related_booking", "related_payment", "related_booking__guest")
            .get(pk=enquiry.pk)
        )
        if enquiry.enquiry_type != Enquiry.EnquiryType.CANCELLATION:
            raise PaymentError("Refunds can only be processed from cancellation/refund requests.")
        if enquiry.cancellation_status not in (
            Enquiry.CancellationStatus.APPROVED,
            Enquiry.CancellationStatus.CANCELLED,
            Enquiry.CancellationStatus.REFUND_FAILED,
        ):
            raise PaymentError("This cancellation request must be approved before a refund is processed.")
        booking = enquiry.related_booking
        payment = enquiry.related_payment
        if booking is None:
            raise PaymentError("No booking is linked to this cancellation request.")
        if booking.status != Booking.Status.CANCELLED:
            raise PaymentError("The booking must be cancelled by staff before a refund is processed.")
        if payment is None:
            payment = (
                Payment.objects.select_for_update()
                .filter(booking=booking, provider=Payment.Provider.PAYSTACK, status__in=[Payment.Status.SUCCESS, Payment.Status.PARTIALLY_REFUNDED])
                .order_by("-paid_at", "-created_at")
                .first()
            )
        else:
            payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment is None or payment.provider != Payment.Provider.PAYSTACK:
            raise PaymentError("Only Paystack payments can be refunded through this action. Record offline refunds manually outside Paystack.")
        if payment.status not in (Payment.Status.SUCCESS, Payment.Status.PARTIALLY_REFUNDED):
            raise PaymentError("This payment is not in a refundable Paystack state.")
        if payment.currency.upper() != "NGN":
            raise PaymentError("Paystack refunds are only supported for NGN payments.")

        existing = (
            Refund.objects.select_for_update()
            .filter(cancellation_request=enquiry)
            .order_by("-created_at")
            .first()
        )
        if existing and existing.status in REFUND_ACTIVE_STATUSES:
            return existing
        if existing and existing.status == Refund.Status.PROCESSED:
            return existing

        # AUTHORITATIVE refund amount = eligible refundable amount − cancellation
        # fee, recomputed HERE from the existing policy engine at submission
        # time. The enquiry snapshot taken at approval is only a review figure:
        # if it is missing, stale (settings changed, a later payment landed) or
        # was never net of the fee, it must not be what Paystack receives.
        # The policy rule itself is unchanged — HotelSettings.cancellation_fee_percent
        # applied by booking_service.calculate_cancellation_policy().
        policy = booking_service.calculate_cancellation_policy(booking)
        policy_refund = Decimal(policy["refund_amount"] or 0).quantize(Decimal("0.01"))
        cancellation_fee = Decimal(policy["cancellation_fee"] or 0).quantize(Decimal("0.01"))
        snapshot_amount = Decimal(enquiry.calculated_refund_amount or 0).quantize(Decimal("0.01"))

        # Never refund more than the policy allows. A snapshot larger than the
        # freshly computed net refund means the fee was not deducted (or the
        # figure is stale) — the policy figure wins, so the fee is applied
        # exactly once and can never be double-deducted.
        requested_amount = policy_refund if snapshot_amount <= 0 else min(snapshot_amount, policy_refund)
        if requested_amount < 0:
            requested_amount = Decimal("0.00")

        # Keep the reviewed figure honest for staff/guest-facing surfaces.
        if snapshot_amount != requested_amount or Decimal(enquiry.calculated_cancellation_fee or 0) != cancellation_fee:
            enquiry.calculated_refund_amount = requested_amount
            enquiry.calculated_cancellation_fee = cancellation_fee
            enquiry.save(update_fields=["calculated_refund_amount",
                                        "calculated_cancellation_fee", "updated_at"])

        processed_total = _processed_refund_total(payment)
        active_total = _active_refund_total(payment, exclude_pk=getattr(existing, "pk", None))
        available = (Decimal(payment.amount or 0).quantize(Decimal("0.01")) - processed_total - active_total).quantize(Decimal("0.01"))
        # Also bounded by what is genuinely still refundable on this payment
        # (never more than paid, never more than the remaining balance).
        amount = min(requested_amount, available)
        if amount <= 0:
            if cancellation_fee > 0 and policy_refund <= 0:
                raise PaymentError(
                    "No refund is due: the cancellation fee covers the full amount paid."
                )
            raise PaymentError("There is no remaining Paystack amount available to refund.")

        # The existing endpoint is manager/admin-only for compatibility. Make
        # the finance policy explicit here as well: a threshold-triggered
        # refund must be authorized by a user with the persisted approval
        # capability, and its effective policy is snapshotted with the request.
        from apps.accounts.capabilities import has_capability
        from apps.finance.services.approval_service import active_control_policy, requires_manager_approval

        finance_policy = active_control_policy()
        manager_approval_required = requires_manager_approval(
            request_type="REFUND", amount=amount, policy=finance_policy
        )
        if manager_approval_required and not has_capability(staff_user, "payment.refund.approve"):
            raise PaymentError("This refund amount requires manager approval.")

        refund_defaults = {
            "booking": booking,
            "payment": payment,
            "cancellation_request": enquiry,
            "amount": amount,
            "currency": payment.currency,
            "status": Refund.Status.PENDING,
            "requested_by": staff_user,
            "paystack_transaction_id": payment.transaction_id,
            "paystack_transaction_reference": payment.reference,
            "paystack_refund_id": "",
            "paystack_refund_reference": "",
            "customer_note": (customer_note or "")[:500],
            "merchant_note": (merchant_note or f"Cancellation request {enquiry.cancellation_reference}")[:500],
            "failure_reason": "",
            "failed_at": None,
            "metadata": {
                "submission_state": "SUBMITTING",
                "cancellation_reference": enquiry.cancellation_reference,
                "requested_amount_from_policy": str(requested_amount),
                "available_amount_at_submission": str(available),
                "retry_of_refund_id": existing.pk if existing else None,
                "finance_control_policy": {
                    "id": finance_policy.pk,
                    "name": finance_policy.name,
                    "effective_from": finance_policy.effective_from.isoformat(),
                    "refund_manager_approval_threshold": str(finance_policy.refund_manager_approval_threshold),
                    "manager_approval_required": manager_approval_required,
                    "authorized_by": staff_user.pk,
                },
            },
        }
        if existing and existing.status == Refund.Status.FAILED:
            refund = existing
            for field, value in refund_defaults.items():
                setattr(refund, field, value)
            refund.requested_at = timezone.now()
            refund.submitted_at = None
            refund.processed_at = None
            refund.save()
        else:
            refund = Refund.objects.create(**refund_defaults)
        enquiry.refund_status = Enquiry.RefundStatus.PENDING
        enquiry.related_payment = payment
        enquiry.paystack_refund_reference = ""
        enquiry.save(update_fields=["refund_status", "related_payment", "paystack_refund_reference", "updated_at"])

    try:
        paystack_data = paystack.create_refund(
            transaction=payment.transaction_id or payment.reference,
            amount_kobo=_amount_to_kobo(refund.amount),
            currency=refund.currency,
            customer_note=refund.customer_note,
            merchant_note=refund.merchant_note,
        )
    except (PaymentGatewayError, PaymentNotConfiguredError) as exc:
        with transaction.atomic():
            refund = Refund.objects.select_for_update().get(pk=refund.pk)
            refund.status = Refund.Status.FAILED
            refund.failed_at = timezone.now()
            refund.failure_reason = "Paystack refund submission failed before confirmation."
            refund.metadata = {**refund.metadata, "submission_state": "FAILED", "submission_error": exc.__class__.__name__}
            refund.save(update_fields=["status", "failed_at", "failure_reason", "metadata", "updated_at"])
            enquiry_services.sync_refund_summary(enquiry)
        log_action(
            actor=staff_user,
            action="REFUND_SUBMISSION_FAILED",
            instance=refund,
            metadata={"booking": booking.booking_reference, "payment_reference": payment.reference, "amount": str(refund.amount)},
            request=request,
            summary=f"Refund submission failed for {booking.booking_reference}",
        )
        raise

    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related("booking", "payment", "cancellation_request").get(pk=refund.pk)
        status_value = _normalise_refund_status(paystack_data.get("status"))
        refund.metadata = {**refund.metadata, "submission_state": "SUBMITTED", "paystack_create_response_status": str(paystack_data.get("status", ""))}
        refund = _mark_refund_status(refund, status_value, provider_data=paystack_data, request=request, event_name="refund.create", force_notify=True)
        if refund.cancellation_request_id:
            enquiry_services.sync_refund_summary(refund.cancellation_request)

    log_action(
        actor=staff_user,
        action="REFUND_SUBMITTED",
        instance=refund,
        metadata={
            "booking": refund.booking.booking_reference,
            "payment_reference": refund.payment.reference,
            "paystack_transaction_id": refund.paystack_transaction_id,
            "paystack_refund_id": refund.paystack_refund_id,
            "amount": str(refund.amount),
            "status": refund.status,
        },
        request=request,
        summary=f"Refund submitted to Paystack for {refund.booking.booking_reference}",
    )
    return refund


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret or not signature:
        return False
    calculated = hmac.new(secret.encode(), msg=raw_body, digestmod=hashlib.sha512).hexdigest()
    return hmac.compare_digest(calculated, signature)


def process_webhook(event_payload: dict, *, request=None):
    """Handle a signature-verified Paystack payload idempotently."""
    event = event_payload.get("event")
    data = event_payload.get("data") or {}
    reference = data.get("reference")

    if event == "charge.success":
        logger.info("Paystack webhook received: event=%s reference=%s", event, reference)
        if not reference:
            return {"handled": False, "reason": "unsupported_event"}
        if not Payment.objects.filter(reference=reference).exists():
            logger.info("Webhook for unknown reference %s ignored", reference)
            return {"handled": False, "reason": "unknown_reference"}
        try:
            result = process_verification(reference=reference, request=request, triggered_by="webhook")
            return {"handled": True, "booking_reference": result["booking_reference"]}
        except PaymentAmountMismatchError:
            logger.error("Webhook verification amount mismatch (%s)", reference)
            return {"handled": False, "reason": "amount_mismatch"}
        except (PaymentError, PaymentGatewayError, BookingStateError) as exc:
            logger.warning("Webhook verification failed for %s: %s", reference, getattr(exc, "detail", str(exc)))
            return {"handled": False, "reason": "verification_failed"}

    if event in REFUND_EVENT_TO_STATUS:
        return _handle_refund_event(event, data, request=request)

    if event == "charge.dispute.create":
        return _handle_dispute_created(data, request=request)

    logger.info("Paystack webhook received: event=%s reference=%s", event, reference)
    return {"handled": False, "reason": "unsupported_event"}


def _handle_refund_event(event, data, *, request=None):
    event_status = _normalise_refund_status(data.get("status"), event=event)
    payment = _find_payment_for_refund_payload(data)
    if payment is None:
        logger.info("Refund webhook for unknown transaction ignored: event=%s", event)
        return {"handled": False, "reason": "unknown_reference"}
    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related("booking", "booking__guest").get(pk=payment.pk)
        refund = _find_or_create_refund_from_provider(payment, data, event_status=event_status)
        refund = _mark_refund_status(refund, event_status, provider_data=data, request=request, event_name=event)

    log_action(
        actor=None,
        action="REFUND_WEBHOOK_RECONCILED",
        instance=refund,
        metadata={
            "event": event,
            "refund_status": refund.status,
            "payment_reference": payment.reference,
            "booking": payment.booking.booking_reference,
            "amount": str(refund.amount),
            "paystack_refund_id": refund.paystack_refund_id,
        },
        request=request,
        summary=f"Refund webhook {event} reconciled for {payment.booking.booking_reference}",
    )
    logger.info("Refund webhook reconciled: event=%s payment=%s refund=%s status=%s", event, payment.reference, refund.pk, refund.status)
    return {"handled": True, "reason": "refund_reconciled", "payment_reference": payment.reference, "refund_id": refund.pk, "status": refund.status}


def _handle_dispute_created(data, *, request=None):
    """A chargeback/dispute was opened on a charge — alert staff for human review.

    No payment or booking status is changed automatically.
    """
    reference = data.get("reference") or (data.get("transaction") or {}).get("reference")
    if not reference:
        return {"handled": False, "reason": "no_reference"}
    payment = (
        Payment.objects.select_related("booking")
        .filter(reference=reference)
        .first()
    )
    if payment is None:
        logger.info("Dispute webhook for unknown reference %s ignored", reference)
        return {"handled": False, "reason": "unknown_reference"}

    log_action(
        actor=None, action="PAYMENT_DISPUTED", instance=payment,
        metadata={"reference": reference, "booking": payment.booking.booking_reference,
                  "dispute_status": str(data.get("status", ""))},
        request=request,
        summary=f"Chargeback opened on {reference}",
    )
    notify_staff(
        type="PAYMENT_DISPUTED",
        title=f"Chargeback alert: {payment.booking.booking_reference}",
        message=(
            f"A dispute was opened against payment {reference} "
            f"({payment.currency} {money(payment.amount)}) for booking "
            f"{payment.booking.booking_reference}. Evidence may be required — review in Paystack."
        ),
        link=booking_service.staff_booking_link(payment.booking),
    )
    logger.warning("Dispute opened: payment=%s booking=%s", reference, payment.booking.booking_reference)
    return {"handled": True, "reason": "dispute_notified", "payment_reference": reference}


# ---------------------------------------------------------------------------
# Staff-recorded offline payments (cash / POS / transfer at the front desk)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_offline_payment(*, booking: Booking, staff_user, amount, provider, notes="", request=None):
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    booking_service.refresh_expired_pending(booking)
    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN):
        raise BookingStateError(
            f"A booking with status {booking.get_status_display()} cannot accept payments."
        )
    # The dashboard hides "Record payment" once a booking is settled, but the
    # endpoint is the authority: a settled or refunded payment record must not
    # be able to take another offline payment even if a stale tab posts one.
    if booking.payment_status in (
        Booking.PaymentStatus.PAID,
        Booking.PaymentStatus.REFUNDED,
        Booking.PaymentStatus.PARTIALLY_REFUNDED,
    ):
        raise PaymentAlreadyCompletedError(
            f"This booking is already marked {booking.get_payment_status_display().lower()}; "
            "no further payment can be recorded against it."
        )
    if booking.amount_due <= 0:
        raise PaymentAlreadyCompletedError()
    if amount > booking.amount_due:
        raise PaymentError(
            f"Amount exceeds the outstanding balance of {booking.currency} {money(booking.amount_due)}."
        )

    payment = Payment.objects.create(
        booking=booking,
        user=staff_user,
        reference=_unique_payment_reference(),
        provider=provider,
        amount=amount,
        currency=booking.currency,
        status=Payment.Status.SUCCESS,
        channel=provider.lower(),
        gateway_response=f"Recorded at front desk by {staff_user.email}",
        paid_at=timezone.now(),
        notes=notes,
        metadata={"booking_reference": booking.booking_reference, "recorded_by": staff_user.email},
    )
    booking = booking_service.register_successful_payment(booking, amount, request=request)
    # Dual-write the gross tender collection before recording best-effort audit
    # delivery. This stays inside the existing atomic offline-payment flow.
    from apps.finance.services.payment_collection_service import ensure_payment_collection

    ensure_payment_collection(payment=payment, actor=staff_user)
    log_action(
        actor=staff_user, action="PAYMENT_RECORDED", instance=payment,
        metadata={
            "reference": payment.reference,
            "booking": booking.booking_reference,
            "amount": str(amount),
            "provider": provider,
        },
        request=request,
        summary=f"Offline payment {payment.reference} recorded ({money(amount)} {booking.currency})",
    )
    logger.info("Offline payment recorded: %s booking=%s amount=%s by=%s",
                payment.reference, booking.booking_reference, amount, staff_user.id)
    return payment
