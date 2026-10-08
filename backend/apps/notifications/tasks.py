# apps/notifications/tasks.py
"""Synchronous transactional email delivery for the notifications app.

``deliver_email_log`` is the single place transactional email is actually
delivered. It is driven entirely by a database row (``EmailLog``) — it
receives a stable integer id, re-reads everything it needs from the DB, and is
the source of truth for the receipt attachment (regenerated server-side). It
records the real outcome so the dashboard can distinguish SENDING / SENT /
FAILED instead of blindly reporting success.

This module names no vendor. The transport is whatever
``apps.notifications.providers.get_provider()`` resolves from settings — SMTP
through Django's backend, or any of the HTTPS API adapters. Adding a provider
never touches this file.

Delivery is SYNCHRONOUS: this module is invoked directly by
``apps.core.emails`` during the originating HTTP request (Send receipt click,
verified payment, booking events). No Celery task, broker, Redis connection or
worker is required — or used — for mail to leave the system. A row is marked
SENT only after the provider accepted the message; anything else is FAILED
with a safe, credential-free reason.

No SMTP credentials, API keys, or secrets are ever logged or stored.
"""
import logging

from django.utils import timezone

logger = logging.getLogger("apps")


def active_transport() -> str:
    """Name of the provider a delivery attempt will use."""
    from apps.notifications.providers import active_provider_name

    return active_provider_name()


def sender_identity():
    """(name, email) parsed from DEFAULT_FROM_EMAIL."""
    from apps.notifications.providers import sender_identity as _sender_identity

    return _sender_identity()


def _safe_reason(exc) -> str:
    """A short, credential-free description of a delivery failure."""
    from apps.notifications.providers import (EmailConfigurationError,
                                              EmailProviderError)

    if isinstance(exc, EmailProviderError):
        return (
            f"{exc.provider} rejected the request "
            f"(HTTP {exc.http_status}): {exc.safe_message}"
        )[:255]
    if isinstance(exc, EmailConfigurationError):
        return str(exc)[:255]
    mapping = {
        "ValueError": str(exc)[:255] if "DEFAULT_FROM_EMAIL" in str(exc) else "Delivery failed (ValueError).",
        "SMTPAuthenticationError": "SMTP authentication failed (check email user / app password).",
        "SMTPRecipientsRefused": "The recipient address was rejected by the mail server.",
        "SMTPSenderRefused": "The sender address was rejected (check DEFAULT_FROM_EMAIL).",
        "SMTPServerDisconnected": "Could not connect to the SMTP server (check EMAIL_HOST/PORT).",
        "SMTPConnectError": "Could not connect to the SMTP server (check EMAIL_HOST/PORT).",
        "SMTPNotSupportedError": "The SMTP server rejected the requested TLS/SSL mode.",
        "SMTPResponseException": "The SMTP server returned an error response.",
        "TimeoutError": "The connection to the mail server timed out.",
        "Timeout": "The HTTPS request to the email provider timed out.",
        "ConnectionError": "The email provider could not be reached over the network.",
        "OSError": "The network connection to the mail server was refused/unreachable.",
        "RuntimeError": str(exc)[:255],
    }
    return mapping.get(exc.__class__.__name__, f"Delivery failed ({exc.__class__.__name__}).")


def _build_attachment(log):
    """Regenerate the receipt PDF from the DB. Returns (filename, bytes) or None."""
    if not (log.attach_receipt_pdf and log.booking_id):
        return None
    try:
        from apps.bookings.models import Booking
        from apps.bookings.serializers import ReceiptSerializer
        from apps.bookings.services.receipt_pdf import render_receipt_pdf

        booking = Booking.objects.select_related("guest", "room_type", "offer").get(
            pk=log.booking_id
        )
        receipt = ReceiptSerializer().to_representation(booking)
        pdf_bytes = render_receipt_pdf(receipt)
        if pdf_bytes:
            return (f"receipt-{booking.booking_reference}.pdf", pdf_bytes)
    except Exception as exc:
        logger.warning(
            "Receipt PDF generation failed for EmailLog#%s (%s)",
            log.pk, exc.__class__.__name__,
        )
        raise
    return None


def deliver_email_log(log_id, **_ignored):
    """Deliver one EmailLog row synchronously through the active provider.

    Exactly one honest attempt: the row ends SENT (provider accepted) or
    FAILED (with error class, safe reason and failure stage). The final
    status is returned. Never raises for delivery failures — the outcome is
    persisted instead.
    """
    from apps.core.email_assets import inline_logo_attachments
    from apps.notifications.models import EmailLog
    from apps.notifications.providers import OutgoingEmail, get_provider

    log = EmailLog.objects.filter(pk=log_id).first()
    if log is None:
        logger.warning("EmailLog#%s vanished before delivery", log_id)
        return None
    if log.status == EmailLog.Status.SENT:
        logger.info("EmailLog#%s already SENT; skipping duplicate", log_id)
        return EmailLog.Status.SENT

    EmailLog.objects.filter(pk=log.pk).update(status=EmailLog.Status.SENDING)

    failure_stage = EmailLog.FailureStage.ATTACHMENT
    try:
        attachment = _build_attachment(log)
        failure_stage = EmailLog.FailureStage.PROVIDER
        provider = get_provider()
        html_body = (log.html_body or "").strip()
        message = OutgoingEmail(
            subject=log.subject,
            text_body=log.body or "",
            html_body=html_body,
            to_email=log.to_email,
            attachments=(
                [(attachment[0], attachment[1], "application/pdf")] if attachment else []
            ),
            inline_images=inline_logo_attachments(html_body, provider),
        )
        provider_message_id = provider.send(message)
    except Exception as exc:  # noqa: BLE001 - outcome persisted below
        EmailLog.objects.filter(pk=log.pk).update(
            status=EmailLog.Status.FAILED,
            error_class=exc.__class__.__name__,
            error_message=_safe_reason(exc),
            failure_stage=failure_stage,
            failed_at=timezone.now(),
        )
        logger.warning(
            "EmailLog#%s delivery FAILED at %s: %s",
            log.pk, failure_stage, exc.__class__.__name__,
        )
        return EmailLog.Status.FAILED

    EmailLog.objects.filter(pk=log.pk).update(
        status=EmailLog.Status.SENT,
        sent_at=timezone.now(),
        provider_message_id=provider_message_id or "",
        error_class="",
        error_message="",
        failure_stage="",
    )
    logger.info(
        "EmailLog#%s SENT kind=%s booking=%s to=%s provider_id=%s",
        log.pk, log.kind, log.booking_reference or "-", log.to_email,
        provider_message_id or "-",
    )
    return EmailLog.Status.SENT
