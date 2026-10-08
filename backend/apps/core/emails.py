# apps/core/emails.py
"""Synchronous transactional email delivery with a real, tracked lifecycle.

The rules this module enforces:

1. Email is delivered DIRECTLY, during the request that triggered it. There
   is no Celery task, no broker, no Redis and no background worker anywhere
   in the email path. The outcome recorded on the persistent ``EmailLog`` row
   (PENDING → SENDING → SENT / FAILED) is the outcome the provider actually
   reported — never "we created a task".
2. An API response may only claim "sent" AFTER the provider (Brevo HTTPS API
   in production, Django's configured backend in development/tests) accepted
   the message. A failure is recorded as FAILED with a safe, credential-free
   reason and surfaced truthfully.
3. Booking/payment request paths use :func:`queue_email`, which defers the
   send until the surrounding database transaction COMMITS
   (``transaction.on_commit``) — a rolled-back booking never emails "your
   booking is confirmed". The send still happens synchronously, inside the
   same request, immediately after the commit.

Application operational emails (booking confirmations, cancellation requests,
refund lifecycle notices, staff alerts, password resets, payment receipts) all
go through here so the behaviour is central, not receipt-only.

Security: SMTP credentials, API keys, tokens and passwords are NEVER logged or
stored on the EmailLog. Only the exception class and a scrubbed, human-readable
reason are persisted.
"""
import logging
from email.utils import parseaddr

from django.db import transaction

logger = logging.getLogger("apps")


def clean_recipients(recipients):
    """Validate + de-duplicate recipient addresses (header-injection safe)."""
    cleaned = []
    for recipient in recipients or []:
        value = str(recipient or "").strip()
        if not value or "\n" in value or "\r" in value:
            continue
        _, addr = parseaddr(value)
        if addr and "@" in addr and "." in addr.split("@")[-1]:
            cleaned.append(addr)
    return list(dict.fromkeys(cleaned))


# Back-compat alias (older imports).
_clean_recipients = clean_recipients


def send_email_safe(
    subject,
    message,
    recipients,
    *,
    html_message="",
    kind="GENERIC",
    booking_reference="",
    payment_reference="",
    booking_id=None,
    attach_receipt_pdf=False,
    created_by=None,
):
    """Record + deliver a transactional email NOW; returns its EmailLog row.

    ``message`` is the required plain-text body (also the fallback for clients
    that cannot render HTML). ``html_message`` is an optional styled HTML
    alternative — when supplied the delivery builds a proper
    ``multipart/alternative`` message (Django backend) or an ``htmlContent`` +
    ``textContent`` pair (Brevo API), never HTML-as-escaped-text.

    Delivery is SYNCHRONOUS: :func:`apps.notifications.tasks.deliver_email_log`
    runs in this thread, talks to the provider over a bounded timeout, and
    records the REAL outcome (SENT with the provider's message id, or FAILED
    with a safe reason) before this function returns. The returned row always
    reflects that final state.

    Errors never propagate to the API caller — but they are never hidden
    either: every attempt is tracked on the returned EmailLog row.
    """
    from apps.notifications.models import EmailLog

    recipients = clean_recipients(recipients)
    if not recipients:
        logger.warning("Email '%s' skipped: no valid recipient", str(subject)[:80])
        return None

    subject = str(subject or "").replace("\r", " ").replace("\n", " ")[:255]

    # One EmailLog row per recipient so status is meaningful per mailbox.
    log = None
    for addr in recipients:
        row = EmailLog.objects.create(
            to_email=addr,
            subject=subject,
            body=message or "",
            html_body=html_message or "",
            kind=kind if kind in EmailLog.Kind.values else EmailLog.Kind.GENERIC,
            booking_reference=booking_reference or "",
            payment_reference=payment_reference or "",
            booking_id=booking_id,
            attach_receipt_pdf=bool(attach_receipt_pdf),
            created_by=created_by if getattr(created_by, "pk", None) else None,
            status=EmailLog.Status.PENDING,
        )
        _deliver(row)
        log = log or row
    return log


def _deliver(log):
    """Deliver a single EmailLog row synchronously and refresh its state.

    ``deliver_email_log`` records its own outcome on the row; this wrapper
    only guarantees that an unexpected exception can never escape into the
    calling request, and that the in-memory instance reflects the database.
    """
    from apps.notifications.tasks import deliver_email_log

    try:
        deliver_email_log(log.pk)
    except Exception as exc:  # noqa: BLE001 - outcome is persisted by the task
        logger.exception(
            "Unexpected error delivering EmailLog#%s (%s)",
            log.pk, exc.__class__.__name__,
        )
    log.refresh_from_db(
        fields=[
            "status", "sent_at", "failed_at", "error_class",
            "error_message", "failure_stage", "provider_message_id",
        ]
    )


def queue_email(subject, message, recipients, **kwargs):
    """Deliver an email AFTER the current transaction commits.

    This is the variant request paths (booking creation, payment confirmation,
    …) must use: the email is only sent if the surrounding transaction
    actually COMMITS (``transaction.on_commit``) — a rolled-back booking never
    sends "your booking is confirmed".

    Despite the historical name, nothing is queued: the callback runs in the
    SAME request, immediately after the commit (Django runs ``on_commit``
    callbacks inline when the connection is in autocommit), and the send is
    fully synchronous. There is no Celery task, no broker and no thread.
    """

    def _after_commit(subject=subject, message=message, recipients=recipients, kwargs=kwargs):
        send_email_safe(subject, message, recipients, **kwargs)

    transaction.on_commit(_after_commit)
