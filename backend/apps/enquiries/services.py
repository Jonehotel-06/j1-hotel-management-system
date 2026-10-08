# apps/enquiries/services.py
"""Structured enquiry and cancellation/refund workflow services.

The public Contact form remains the only guest-facing cancellation entry point.
A cancellation enquiry is an identifier-rich request for staff review — it does
not itself cancel a booking and does not imply that any money has been refunded.
"""
import logging
import secrets
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.audit.services import log_action
from apps.bookings.models import Booking
from apps.bookings.services import booking_service
from apps.core.emails import queue_email
from apps.core.utils import generate_cancellation_reference, money
from apps.hotel.models import HotelSettings
from apps.notifications.services import notify_staff, notify_users
from apps.payments.models import Payment, Refund

from .models import Enquiry

logger = logging.getLogger("apps")

CANCELLATION_SUBJECT = "Cancellation / Refund Request"


def is_cancellation_subject(subject: str) -> bool:
    value = (subject or "").strip().lower()
    return "cancellation" in value or "refund" in value


def _unique_cancellation_reference():
    for _ in range(10):
        reference = generate_cancellation_reference()
        if not Enquiry.objects.filter(cancellation_reference=reference).exists():
            return reference
    raise RuntimeError("Could not allocate a unique cancellation reference.")


def _normalize_reference(value):
    return (value or "").strip()[:80]


def _find_booking(reference):
    ref = _normalize_reference(reference)
    if not ref:
        return None
    return Booking.objects.select_related("guest", "room_type").filter(booking_reference__iexact=ref).first()


def _find_paystack_payment(reference, *, booking=None):
    ref = _normalize_reference(reference)
    qs = Payment.objects.select_related("booking", "booking__guest").filter(provider=Payment.Provider.PAYSTACK)
    if ref:
        payment = qs.filter(Q(reference__iexact=ref) | Q(transaction_id__iexact=ref)).first()
        if payment and (booking is None or payment.booking_id == booking.pk):
            return payment
        return None
    if booking is not None:
        return (
            qs.filter(booking=booking, status__in=(Payment.Status.SUCCESS, Payment.Status.PARTIALLY_REFUNDED))
            .order_by("-paid_at", "-created_at")
            .first()
        )
    return None


def resolve_cancellation_links(enquiry: Enquiry):
    """Best-effort linkage of guest-supplied identifiers to local rows.

    Unknown or mismatched references are recorded for staff visibility but are
    not revealed to the guest as valid/invalid, avoiding booking/payment
    reference enumeration.
    """
    if enquiry.enquiry_type != Enquiry.EnquiryType.CANCELLATION:
        return enquiry

    metadata = dict(enquiry.metadata or {})
    issues = []

    booking = _find_booking(enquiry.booking_reference)
    if booking is None and enquiry.related_booking_id:
        booking = enquiry.related_booking
    if booking is None and enquiry.payment_reference:
        payment_by_ref = _find_paystack_payment(enquiry.payment_reference)
        if payment_by_ref:
            booking = payment_by_ref.booking
            if not enquiry.booking_reference:
                enquiry.booking_reference = booking.booking_reference

    payment = _find_paystack_payment(enquiry.payment_reference, booking=booking)
    if payment is None and booking is not None:
        payment = _find_paystack_payment("", booking=booking)

    if enquiry.booking_reference and booking is None:
        issues.append("booking_reference_not_found")
    if enquiry.payment_reference and payment is None:
        issues.append("payment_reference_not_found_or_mismatched")
    if payment is not None and booking is not None and payment.booking_id != booking.pk:
        issues.append("payment_booking_mismatch")
        payment = None

    enquiry.related_booking = booking
    enquiry.related_payment = payment
    metadata["lookup_issues"] = issues
    enquiry.metadata = metadata
    return enquiry


def cancellation_status_link(enquiry: Enquiry, token=None):
    if enquiry.enquiry_type != Enquiry.EnquiryType.CANCELLATION or not enquiry.cancellation_reference:
        return ""
    token = token or getattr(enquiry, "raw_public_access_token", "")
    query = {"ref": enquiry.cancellation_reference}
    if token:
        query["token"] = token
    return f"{settings.FRONTEND_URL}/cancellation-result.html?{urlencode(query)}"


def _admin_enquiry_link(enquiry):
    return f"/dashboard/enquiries.html?id={enquiry.pk}"


def _notification_recipients():
    configured = [email for email in getattr(settings, "HOTEL_NOTIFICATION_EMAILS", []) if email]
    if configured:
        return configured
    try:
        hotel_email = HotelSettings.get_settings().email
    except Exception:
        hotel_email = ""
    return [hotel_email] if hotel_email else []




def _notify_staff_safe(**kwargs):
    try:
        notify_staff(**kwargs)
    except Exception as exc:  # defensive; notifications must not roll back enquiries
        logger.warning("Could not create staff notification (%s)", exc.__class__.__name__)

def _mark_email_queued(enquiry: Enquiry, key: str) -> bool:
    events = dict(enquiry.email_events or {})
    if events.get(key):
        return False
    events[key] = timezone.now().isoformat()
    enquiry.email_events = events
    enquiry.save(update_fields=["email_events", "updated_at"])
    return True


def queue_enquiry_email(enquiry: Enquiry, key: str, *, subject: str, message: str,
                        recipients, html_message="", kind="ENQUIRY"):
    """Send an idempotent enquiry/cancellation email (once per event key).

    The state flag is committed with the business transition; the actual
    delivery runs synchronously right after the surrounding transaction
    commits (``queue_email`` → ``transaction.on_commit`` → direct provider
    call) and can fail without rolling back the booking/payment/refund state.
    """
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return False
    if not _mark_email_queued(enquiry, key):
        return False
    queue_email(
        subject, message, recipients,
        html_message=html_message,
        kind=kind,
        booking_reference=enquiry.booking_reference or "",
    )
    return True


def _queue_cancellation_received_emails(enquiry: Enquiry, token: str):
    from apps.core.email_design import render_notice_email

    hotel = HotelSettings.get_settings()
    status_url = cancellation_status_link(enquiry, token)
    guest_text, guest_html = render_notice_email(
        category="Cancellation request",
        title="We received your cancellation request",
        greeting=f"Hello {enquiry.name},",
        paragraphs=[
            f"We received your cancellation/refund request for booking "
            f"{enquiry.booking_reference or 'the reference you provided'}.",
            "Your booking has NOT been cancelled yet. Our team will verify the "
            "booking and payment details, then contact you with the outcome.",
        ],
        details=[
            {"label": "Request reference", "value": enquiry.cancellation_reference},
            {"label": "Booking reference", "value": enquiry.booking_reference or "—"},
            {"label": "Current status", "value": "Under hotel review"},
        ],
        cta_label="Check Request Status",
        cta_url=status_url,
        preheader=f"Cancellation request {enquiry.cancellation_reference} is under review.",
    )
    queue_enquiry_email(
        enquiry, "cancellation_received_guest",
        subject=f"Cancellation request received: {enquiry.cancellation_reference} — {hotel.hotel_name}",
        message=guest_text,
        html_message=guest_html,
        recipients=[enquiry.email],
    )

    admin_message = (
        f"Cancellation/refund request received.\n\n"
        f"Request reference: {enquiry.cancellation_reference}\n"
        f"Guest: {enquiry.name}\nEmail: {enquiry.email}\nPhone: {enquiry.phone or '—'}\n"
        f"Booking reference: {enquiry.booking_reference or '—'}\n"
        f"Paystack/payment reference: {enquiry.payment_reference or '—'}\n"
        f"Receipt/reference supplied: {enquiry.receipt_reference or '—'}\n"
        f"Refund requested: {'Yes' if enquiry.refund_requested else 'No / not specified'}\n\n"
        f"Reason:\n{enquiry.cancellation_reason or enquiry.message}\n\n"
        "Review it in the staff Enquiries dashboard before cancelling any booking or initiating a refund."
    )
    queue_enquiry_email(
        enquiry, "cancellation_received_admin",
        subject=f"Cancellation request received: {enquiry.booking_reference or enquiry.cancellation_reference}",
        message=admin_message,
        recipients=_notification_recipients(),
    )


def create_enquiry(validated_data, *, request=None):
    """Create a generic enquiry or a structured cancellation request."""
    validated_data = dict(validated_data)
    validated_data.pop("website", None)
    subject = validated_data.get("subject") or ""
    explicit_type = validated_data.get("enquiry_type") or ""
    is_cancel = explicit_type == Enquiry.EnquiryType.CANCELLATION or is_cancellation_subject(subject)

    with transaction.atomic():
        token = ""
        enquiry = Enquiry(**validated_data)
        enquiry.ip_address = request.META.get("REMOTE_ADDR") if request is not None else None
        enquiry.enquiry_type = Enquiry.EnquiryType.CANCELLATION if is_cancel else Enquiry.EnquiryType.GENERAL
        if enquiry.enquiry_type == Enquiry.EnquiryType.CANCELLATION:
            enquiry.subject = CANCELLATION_SUBJECT
            enquiry.cancellation_reference = _unique_cancellation_reference()
            if not enquiry.cancellation_reason:
                enquiry.cancellation_reason = enquiry.message
            token = secrets.token_urlsafe(32)
            enquiry.public_access_token_hash = Enquiry.hash_public_access_token(token)
            from datetime import timedelta
            enquiry.public_access_expires_at = timezone.now() + timedelta(days=90)
            enquiry.refund_status = Enquiry.RefundStatus.NONE
            enquiry = resolve_cancellation_links(enquiry)
        enquiry.save()
        if enquiry.enquiry_type == Enquiry.EnquiryType.CANCELLATION:
            enquiry.raw_public_access_token = token
            log_action(
                actor=None,
                action="CANCELLATION_REQUEST_CREATED",
                instance=enquiry,
                metadata={
                    "cancellation_reference": enquiry.cancellation_reference,
                    "booking_reference": enquiry.booking_reference,
                    "payment_reference": enquiry.payment_reference,
                    "related_booking_id": enquiry.related_booking_id,
                    "related_payment_id": enquiry.related_payment_id,
                },
                request=request,
                summary=f"Cancellation request {enquiry.cancellation_reference} created",
            )
            _notify_staff_safe(
                type="CANCELLATION_REQUEST_CREATED",
                title=f"Cancellation request: {enquiry.booking_reference or enquiry.cancellation_reference}",
                message=f"{enquiry.name} requested cancellation/refund review.",
                link=_admin_enquiry_link(enquiry),
            )
            _queue_cancellation_received_emails(enquiry, token)
        else:
            _notify_staff_safe(
                type="ENQUIRY_NEW",
                title=f"New enquiry: {enquiry.subject}",
                message=f"{enquiry.name} ({enquiry.email}): {enquiry.message[:140]}",
                link="/dashboard/enquiries.html",
            )
    logger.info("New enquiry #%s type=%s from %s", enquiry.pk, enquiry.enquiry_type, enquiry.email)
    return enquiry


def get_public_cancellation_status(reference, token):
    enquiry = Enquiry.objects.filter(
        cancellation_reference=reference,
        enquiry_type=Enquiry.EnquiryType.CANCELLATION,
    ).select_related("related_booking", "related_payment").prefetch_related("refunds").first()
    if enquiry is None or not enquiry.public_token_matches(token):
        raise NotFound("Cancellation request not found.")
    return enquiry


def _require_cancellation_enquiry(enquiry_id):
    enquiry = (
        Enquiry.objects.select_for_update()
        .select_related("related_booking", "related_booking__guest", "related_booking__room_type", "related_payment")
        .get(pk=enquiry_id)
    )
    if enquiry.enquiry_type != Enquiry.EnquiryType.CANCELLATION:
        raise ValidationError({"enquiry_type": ["This action is only available for cancellation/refund requests."]})
    return resolve_cancellation_links(enquiry)


def _append_note(existing, note, staff_user):
    note = (note or "").strip()
    if not note:
        return existing or ""
    who = getattr(staff_user, "email", "staff")
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    return ((existing or "").rstrip() + f"\n[{stamp} {who}] {note}\n").lstrip()


@transaction.atomic
def mark_under_review(enquiry_id, *, staff_user, notes="", request=None):
    enquiry = _require_cancellation_enquiry(enquiry_id)
    previous = enquiry.cancellation_status
    if enquiry.cancellation_status == Enquiry.CancellationStatus.NEW:
        enquiry.cancellation_status = Enquiry.CancellationStatus.UNDER_REVIEW
    enquiry.status = Enquiry.Status.IN_PROGRESS
    enquiry.internal_notes = _append_note(enquiry.internal_notes, notes, staff_user)
    enquiry.save(update_fields=["cancellation_status", "status", "internal_notes", "related_booking", "related_payment", "metadata", "updated_at"])
    log_action(
        actor=staff_user,
        action="CANCELLATION_REQUEST_REVIEWED",
        instance=enquiry,
        changes={"cancellation_status": [previous, enquiry.cancellation_status]} if previous != enquiry.cancellation_status else {},
        metadata={"cancellation_reference": enquiry.cancellation_reference, "booking_reference": enquiry.booking_reference},
        request=request,
        summary=f"Cancellation request {enquiry.cancellation_reference} marked under review",
    )
    _notify_staff_safe(
        type="CANCELLATION_REQUEST_REVIEWED",
        title=f"Cancellation under review: {enquiry.booking_reference or enquiry.cancellation_reference}",
        message=f"{staff_user.email} is reviewing the cancellation request.",
        link=_admin_enquiry_link(enquiry),
    )
    return enquiry


def _latest_refundable_payment(booking, requested_reference=""):
    if requested_reference:
        payment = _find_paystack_payment(requested_reference, booking=booking)
        if payment:
            return payment
    return (
        Payment.objects.select_for_update()
        .filter(booking=booking, provider=Payment.Provider.PAYSTACK, status__in=(Payment.Status.SUCCESS, Payment.Status.PARTIALLY_REFUNDED))
        .order_by("-paid_at", "-created_at")
        .first()
    )


@transaction.atomic
def approve_cancellation(enquiry_id, *, staff_user, notes="", resolution="", request=None):
    enquiry = _require_cancellation_enquiry(enquiry_id)
    booking = enquiry.related_booking
    if booking is None:
        raise ValidationError({"booking_reference": ["No local booking could be resolved for this request."]})

    # Lock the booking row before deriving policy/refund information.
    booking = Booking.objects.select_for_update().select_related("guest", "room_type").get(pk=booking.pk)
    previous_status = enquiry.cancellation_status
    policy = booking_service.calculate_cancellation_policy(booking)
    payment = enquiry.related_payment
    if payment is None:
        payment = _latest_refundable_payment(booking, enquiry.payment_reference)

    refund_status = Enquiry.RefundStatus.NOT_REQUIRED
    if policy["refund_amount"] > 0:
        refund_status = Enquiry.RefundStatus.DUE if payment else Enquiry.RefundStatus.MANUAL_REQUIRED

    if booking.status != Booking.Status.CANCELLED:
        booking = booking_service.cancel_booking(
            booking,
            reason=enquiry.cancellation_reason or resolution or notes or "Cancellation request approved by hotel",
            by_user=staff_user,
            staff=True,
            request=request,
            send_guest_email=False,
            cancellation_request=enquiry,
        )

    enquiry.related_booking = booking
    enquiry.related_payment = payment
    enquiry.calculated_cancellation_fee = policy["cancellation_fee"]
    enquiry.calculated_refund_amount = policy["refund_amount"]
    enquiry.refund_status = refund_status
    enquiry.cancellation_status = Enquiry.CancellationStatus.CANCELLED
    enquiry.status = Enquiry.Status.IN_PROGRESS if refund_status in (Enquiry.RefundStatus.DUE, Enquiry.RefundStatus.MANUAL_REQUIRED) else Enquiry.Status.RESOLVED
    enquiry.internal_notes = _append_note(enquiry.internal_notes, notes, staff_user)
    enquiry.resolution = (resolution or enquiry.resolution or "Cancellation approved and booking cancelled.")[:5000]
    enquiry.processed_by = staff_user
    enquiry.processed_at = timezone.now()
    enquiry.save(update_fields=[
        "related_booking", "related_payment", "calculated_cancellation_fee", "calculated_refund_amount",
        "refund_status", "cancellation_status", "status", "internal_notes", "resolution", "processed_by",
        "processed_at", "metadata", "updated_at",
    ])

    log_action(
        actor=staff_user,
        action="CANCELLATION_REQUEST_APPROVED",
        instance=enquiry,
        changes={"cancellation_status": [previous_status, enquiry.cancellation_status]},
        metadata={
            "cancellation_reference": enquiry.cancellation_reference,
            "booking_reference": booking.booking_reference,
            "payment_reference": payment.reference if payment else "",
            "refund_due": str(policy["refund_amount"]),
            "cancellation_fee": str(policy["cancellation_fee"]),
        },
        request=request,
        summary=f"Cancellation request {enquiry.cancellation_reference} approved",
    )
    _notify_staff_safe(
        type="CANCELLATION_REQUEST_APPROVED",
        title=f"Cancellation approved: {booking.booking_reference}",
        message=(
            f"Booking cancelled. Refund due: {booking.currency} {money(policy['refund_amount'])}."
            if policy["refund_amount"] > 0 else "Booking cancelled. No refund due."
        ),
        link=_admin_enquiry_link(enquiry),
    )
    _queue_cancellation_approved_email(enquiry, booking, policy, payment)
    return enquiry


def _queue_cancellation_approved_email(enquiry, booking, policy, payment):
    hotel = HotelSettings.get_settings()
    refund_line = "No refund is due based on the payment record and cancellation policy."
    if policy["refund_amount"] > 0:
        if payment and payment.provider == Payment.Provider.PAYSTACK:
            refund_line = (
                f"A refund amount of {booking.currency} {money(policy['refund_amount'])} is eligible for review. "
                "If approved for Paystack processing, we will send another update when the refund is submitted and again when Paystack confirms completion."
            )
        else:
            refund_line = (
                f"A refund amount of {booking.currency} {money(policy['refund_amount'])} requires manual handling by the hotel team. "
                "We will contact you with the next steps."
            )
    from apps.core.email_design import render_notice_email
    from apps.core.formatting import format_money

    guest_text, guest_html = render_notice_email(
        category="Cancellation approved",
        title="Your booking has been cancelled",
        greeting=f"Hello {enquiry.name},",
        paragraphs=[
            f"Your cancellation request {enquiry.cancellation_reference} has been approved "
            f"and booking {booking.booking_reference} has been cancelled.",
            refund_line,
        ],
        details=[
            {"label": "Request reference", "value": enquiry.cancellation_reference},
            {"label": "Booking reference", "value": booking.booking_reference},
            {"label": "Cancellation fee", "value": format_money(policy["cancellation_fee"], booking.currency)},
            {"label": "Calculated refund", "value": format_money(policy["refund_amount"], booking.currency)},
            {"label": "Refund status", "value": enquiry.get_refund_status_display()},
        ],
        footnote=(
            "This message is about the hotel booking cancellation. Paystack payment "
            "receipts/refund notices are separate provider communications."
        ),
        preheader=f"Booking {booking.booking_reference} has been cancelled.",
    )
    queue_enquiry_email(
        enquiry,
        "cancellation_approved_guest",
        subject=f"Booking cancelled: {booking.booking_reference} — {hotel.hotel_name}",
        message=guest_text,
        html_message=guest_html,
        kind="CANCELLATION",
        recipients=[enquiry.email],
    )


@transaction.atomic
def reject_cancellation(enquiry_id, *, staff_user, notes="", resolution="", request=None):
    enquiry = _require_cancellation_enquiry(enquiry_id)
    previous = enquiry.cancellation_status
    enquiry.cancellation_status = Enquiry.CancellationStatus.REJECTED
    enquiry.status = Enquiry.Status.RESOLVED
    enquiry.refund_status = Enquiry.RefundStatus.NOT_REQUIRED
    enquiry.internal_notes = _append_note(enquiry.internal_notes, notes, staff_user)
    enquiry.resolution = (resolution or notes or "Cancellation request rejected after review.")[:5000]
    enquiry.processed_by = staff_user
    enquiry.processed_at = timezone.now()
    enquiry.save(update_fields=[
        "cancellation_status", "status", "refund_status", "internal_notes", "resolution",
        "processed_by", "processed_at", "related_booking", "related_payment", "metadata", "updated_at",
    ])
    log_action(
        actor=staff_user,
        action="CANCELLATION_REJECTED",
        instance=enquiry,
        changes={"cancellation_status": [previous, enquiry.cancellation_status]},
        metadata={"cancellation_reference": enquiry.cancellation_reference, "booking_reference": enquiry.booking_reference},
        request=request,
        summary=f"Cancellation request {enquiry.cancellation_reference} rejected",
    )
    _notify_staff_safe(
        type="CANCELLATION_REQUEST_REJECTED",
        title=f"Cancellation rejected: {enquiry.booking_reference or enquiry.cancellation_reference}",
        message=f"{staff_user.email} rejected the cancellation request.",
        link=_admin_enquiry_link(enquiry),
    )
    hotel = HotelSettings.get_settings()
    from apps.core.email_design import render_notice_email

    guest_text, guest_html = render_notice_email(
        category="Cancellation request",
        title="Update on your cancellation request",
        greeting=f"Hello {enquiry.name},",
        paragraphs=[
            f"We reviewed your cancellation request {enquiry.cancellation_reference}. "
            "It has not been approved at this time.",
            f"Reason/update: {enquiry.resolution}",
            "Your booking has not been cancelled through this request.",
        ],
        details=[
            {"label": "Request reference", "value": enquiry.cancellation_reference},
            {"label": "Booking reference", "value": enquiry.booking_reference or "—"},
            {"label": "Status", "value": "Not approved"},
        ],
        footnote=(
            f"If you have questions, please contact {hotel.phone or hotel.email}."
        ),
        preheader=f"Update on cancellation request {enquiry.cancellation_reference}.",
    )
    queue_enquiry_email(
        enquiry,
        "cancellation_rejected_guest",
        subject=f"Cancellation request update: {enquiry.cancellation_reference} — {hotel.hotel_name}",
        message=guest_text,
        html_message=guest_html,
        recipients=[enquiry.email],
    )
    return enquiry


@transaction.atomic
def close_request(enquiry_id, *, staff_user, notes="", request=None):
    enquiry = _require_cancellation_enquiry(enquiry_id)
    previous = enquiry.cancellation_status
    enquiry.cancellation_status = Enquiry.CancellationStatus.CLOSED
    enquiry.status = Enquiry.Status.CLOSED
    enquiry.internal_notes = _append_note(enquiry.internal_notes, notes, staff_user)
    enquiry.processed_by = staff_user
    enquiry.processed_at = timezone.now()
    enquiry.save(update_fields=["cancellation_status", "status", "internal_notes", "processed_by", "processed_at", "updated_at"])
    log_action(
        actor=staff_user,
        action="CANCELLATION_REQUEST_CLOSED",
        instance=enquiry,
        changes={"cancellation_status": [previous, enquiry.cancellation_status]},
        metadata={"cancellation_reference": enquiry.cancellation_reference, "booking_reference": enquiry.booking_reference},
        request=request,
        summary=f"Cancellation request {enquiry.cancellation_reference} closed",
    )
    return enquiry


def refresh_refund_summary(enquiry: Enquiry):
    """Sync cancellation request summary fields from linked Refund rows."""
    refunds = list(enquiry.refunds.select_related("payment", "booking").order_by("-created_at"))
    if not refunds:
        return enquiry
    latest = refunds[0]
    status_map = {
        Refund.Status.PENDING: (Enquiry.RefundStatus.PENDING, Enquiry.CancellationStatus.REFUND_PENDING),
        Refund.Status.PROCESSING: (Enquiry.RefundStatus.PROCESSING, Enquiry.CancellationStatus.REFUND_PROCESSING),
        Refund.Status.PROCESSED: (Enquiry.RefundStatus.PROCESSED, Enquiry.CancellationStatus.REFUNDED),
        Refund.Status.FAILED: (Enquiry.RefundStatus.FAILED, Enquiry.CancellationStatus.REFUND_FAILED),
        Refund.Status.NEEDS_ATTENTION: (Enquiry.RefundStatus.NEEDS_ATTENTION, Enquiry.CancellationStatus.REFUND_FAILED),
    }
    refund_status, cancellation_status = status_map.get(latest.status, (enquiry.refund_status, enquiry.cancellation_status))
    updates = []
    if enquiry.refund_status != refund_status:
        enquiry.refund_status = refund_status
        updates.append("refund_status")
    if enquiry.cancellation_status != cancellation_status:
        enquiry.cancellation_status = cancellation_status
        updates.append("cancellation_status")
    if latest.paystack_refund_reference and enquiry.paystack_refund_reference != latest.paystack_refund_reference:
        enquiry.paystack_refund_reference = latest.paystack_refund_reference
        updates.append("paystack_refund_reference")
    if latest.amount and enquiry.calculated_refund_amount <= 0:
        enquiry.calculated_refund_amount = latest.amount
        updates.append("calculated_refund_amount")
    if latest.status == Refund.Status.PROCESSED:
        enquiry.status = Enquiry.Status.RESOLVED
        updates.append("status")
    elif latest.status in (Refund.Status.FAILED, Refund.Status.NEEDS_ATTENTION):
        enquiry.status = Enquiry.Status.IN_PROGRESS
        updates.append("status")
    if updates:
        updates.append("updated_at")
        enquiry.save(update_fields=list(dict.fromkeys(updates)))
    return enquiry


def processed_refund_total_for_booking(booking):
    total = booking.refunds.filter(status=Refund.Status.PROCESSED).aggregate(total=Sum("amount"))["total"]
    return Decimal(total or 0).quantize(Decimal("0.01"))

# Backwards/semantic alias used by the payments service.
def sync_refund_summary(enquiry: Enquiry):
    return refresh_refund_summary(enquiry)
