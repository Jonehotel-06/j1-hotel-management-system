# apps/bookings/services/receipt_email.py
"""Build the guest-facing payment-receipt email (subject + text + HTML).

This module owns *presentation only*. It consumes the already-authoritative
receipt payload produced by :class:`~apps.bookings.serializers.ReceiptSerializer`
(the same source of truth used by the on-screen receipt and the PDF) and turns
it into:

* a professional subject line
* a plain-text body (the required fallback / text-only clients)
* a styled, email-safe HTML body (rendered from Django templates)

No amounts, dates or references are recomputed here — everything is read from
the receipt payload and merely *formatted* (Naira, human-readable dates). All
dynamic values are auto-escaped by the Django template engine, so guest names,
notes and references can never inject markup.

Keeping this separate from the sending pipeline (``core.emails`` /
``notifications.tasks``) and from booking business logic means every receipt
email path renders one consistent, maintainable template.
"""
from django.template.loader import render_to_string

from apps.core.formatting import format_date, format_datetime, format_money

# Logo reference strategy (cid: vs hosted URL) depends on the active provider;
# apps.core.email_assets owns that decision. Re-exported for older imports.
from apps.core.email_assets import LOGO_CID, logo_context  # noqa: E402,F401


# Human-readable status presentation. Each entry carries an accessible label, a
# short glyph (so status is never conveyed by colour alone), and a semantic tone
# the template maps to an accessible badge style. Keys cover both the booking
# payment_status values and the terminal booking status (cancelled).
_STATUS_PRESENTATION = {
    "PAID": ("Paid in full", "PAID", "✓", "paid"),
    "PARTIALLY_PAID": ("Partially paid", "PARTIALLY PAID", "½", "partial"),
    "UNPAID": ("Awaiting payment", "PENDING", "•", "pending"),
    "PENDING": ("Awaiting payment", "PENDING", "•", "pending"),
    "PARTIALLY_REFUNDED": ("Partially refunded", "PARTIALLY REFUNDED", "←", "refunded"),
    "REFUNDED": ("Refunded", "REFUNDED", "←", "refunded"),
    "FAILED": ("Payment failed", "FAILED", "✕", "failed"),
    "CANCELLED": ("Booking cancelled", "CANCELLED", "✕", "cancelled"),
}
_DEFAULT_STATUS = ("Payment status", "STATUS", "•", "pending")


def _status_presentation(receipt):
    """Pick the status shown to the guest.

    A cancelled booking is surfaced as CANCELLED even if a prior payment status
    lingers; otherwise the booking's payment_status drives the badge.
    """
    booking_status = str(receipt.get("booking_status") or "").upper()
    payment_status = str(receipt.get("payment_status") or "").upper()
    key = "CANCELLED" if booking_status == "CANCELLED" else payment_status
    label, badge, glyph, tone = _STATUS_PRESENTATION.get(key, _DEFAULT_STATUS)
    return {"label": label, "badge": badge, "glyph": glyph, "tone": tone}


# Payment-record status → guest-facing label + glyph for the history table.
_PAYMENT_STATUS_LABELS = {
    "SUCCESS": ("Successful", "✓"),
    "PENDING": ("Pending", "•"),
    "FAILED": ("Failed", "✕"),
    "REFUNDED": ("Refunded", "←"),
    "PARTIALLY_REFUNDED": ("Partially refunded", "←"),
}


def _payment_method(payment):
    """Best human-readable payment method from provider/channel."""
    label = (payment.get("provider_label") or payment.get("provider") or "").strip()
    channel = (payment.get("channel") or "").strip()
    if channel and channel.lower() not in label.lower():
        pretty_channel = channel.replace("_", " ").title()
        return f"{label} · {pretty_channel}" if label else pretty_channel
    return label or "—"


def _hotel_location(hotel):
    """One-line address from the parts that are actually populated."""
    parts = [
        hotel.get("address"),
        hotel.get("city"),
        hotel.get("state"),
        hotel.get("country"),
    ]
    return ", ".join(p for p in (str(x or "").strip() for x in parts) if p)


def build_receipt_context(receipt):
    """Turn a ReceiptSerializer payload into a template-ready context.

    Returns a plain dict of already-formatted, display-safe values. No business
    logic — pure presentation.
    """
    hotel = receipt.get("hotel") or {}
    guest = receipt.get("guest") or {}
    currency = receipt.get("currency") or "NGN"

    status = _status_presentation(receipt)

    # De-duplicate the payment history by payment reference so a booking that
    # was verified twice (webhook + browser) never shows the same line twice.
    seen = set()
    payments = []
    for payment in receipt.get("payments") or []:
        reference = payment.get("reference") or ""
        dedup_key = reference or (payment.get("transaction_id"), payment.get("paid_at"))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        status_code = str(payment.get("status") or "").upper()
        status_label, status_glyph = _PAYMENT_STATUS_LABELS.get(
            status_code, (status_code.title() or "—", "•")
        )
        payments.append({
            "reference": reference or "—",
            "amount": format_money(payment.get("amount"), currency),
            "status_label": status_label,
            "status_glyph": status_glyph,
            "paid_at": format_datetime(payment.get("paid_at")) or "Date unavailable",
            "method": _payment_method(payment),
        })

    room_numbers = receipt.get("room_numbers") or []
    rooms_count = receipt.get("rooms") or 0

    return {
        "hotel_name": hotel.get("name") or "J-ONE HOTEL & LODGE",
        "hotel_location": _hotel_location(hotel),
        "hotel_phone": hotel.get("phone") or "",
        "hotel_email": hotel.get("email") or "",
        **logo_context(),

        "booking_reference": receipt.get("booking_reference") or "",
        "receipt_reference": receipt.get("receipt_reference") or receipt.get("booking_reference") or "",
        "issued_at": format_datetime(receipt.get("issued_at")),

        "guest_name": guest.get("name") or "",
        "guest_email": guest.get("email") or "",

        "room_type": receipt.get("room_type") or "",
        "room_numbers": ", ".join(str(r) for r in room_numbers),
        "rooms_count": rooms_count,
        "nights": receipt.get("nights") or 0,
        "check_in": format_date(receipt.get("check_in")),
        "check_out": format_date(receipt.get("check_out")),
        "number_of_guests": receipt.get("number_of_guests") or 0,

        "status_label": status["label"],
        "status_badge": status["badge"],
        "status_glyph": status["glyph"],
        "status_tone": status["tone"],

        "total": format_money(receipt.get("total"), currency),
        "amount_paid": format_money(receipt.get("amount_paid"), currency),
        "outstanding": format_money(receipt.get("amount_due"), currency),
        "has_outstanding": _is_positive(receipt.get("amount_due")),

        "payments": payments,
        "has_payments": bool(payments),
        "multiple_payments": len(payments) > 1,

        "currency": currency,
    }


def _is_positive(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value if value not in (None, "") else "0")) > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def render_receipt_email(receipt):
    """Return ``(subject, text_body, html_body)`` for a receipt payload.

    Subject example: ``Payment Receipt — J1-20260912-02E927D9`` (the booking
    reference is always taken dynamically from the payload).
    """
    context = build_receipt_context(receipt)
    subject = f"Payment Receipt — {context['booking_reference']}"
    text_body = render_to_string("emails/receipt.txt", context)
    html_body = render_to_string("emails/receipt.html", context)
    return subject, text_body, html_body
