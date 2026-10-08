# apps/notifications/email_models.py
"""Delivery-tracked email log.

Every transactional email (receipts, booking confirmations, cancellations,
password resets, enquiry/staff alerts) is recorded here BEFORE it is handed to
the provider, and the row is updated as the SYNCHRONOUS delivery progresses.
This is what makes the difference between "we created a row" and "the email
provider accepted the message" visible to staff — the bug this model exists to
kill was the dashboard reporting success while delivery silently failed.

Lifecycle (synchronous — no queue, no worker):

    PENDING  → row created, delivery about to start in the same request
    SENDING  → the provider request is in flight right now
    SENT     → the provider (Brevo / SMTP backend) ACCEPTED the message;
               ``provider_message_id`` holds Brevo's message id
    FAILED   → the provider rejected it or the attempt could not complete;
               ``error_class`` / ``error_message`` / ``failure_stage`` say why

The legacy queue-era statuses (QUEUED, RETRYING) and fields (``task_id``,
``queued_at``, ``retry_count``, ``max_retries``) were removed when email
delivery became synchronous; historical rows were migrated to terminal states
so the record of past sends is preserved.

No secrets are ever stored here: SMTP credentials, API keys, tokens and
passwords must never be written to ``error_message`` or any other field.
"""
from datetime import timedelta

from django.conf import settings
from django.db import models


class EmailLog(models.Model):
    """One transactional email and its real synchronous delivery lifecycle."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"        # row created, delivery imminent
        SENDING = "SENDING", "Sending"        # provider request in flight
        SENT = "SENT", "Sent"                 # provider accepted the message
        FAILED = "FAILED", "Failed"           # provider rejected / unreachable

    class FailureStage(models.TextChoices):
        """WHERE in the pipeline a FAILED email broke.

        This makes an admin able to tell, at a glance, whether the receipt
        never rendered (a code/data bug), the PDF could not be generated, or
        the email provider rejected the message (a delivery/config problem) —
        instead of guessing from a generic error string.
        """
        NONE = "", "—"
        RENDER = "RENDER", "Rendering the receipt"
        ATTACHMENT = "ATTACHMENT", "Generating the PDF attachment"
        PROVIDER = "SMTP", "Submitting to the email provider"

    # Back-compat alias: older code/tests refer to FailureStage.SMTP. The
    # stored value is identical ("SMTP"), only the label changed.
    # (Kept as a classmethod-style attribute below after the TextChoices is
    # materialised — see module bottom.)

    class Kind(models.TextChoices):
        RECEIPT = "RECEIPT", "Payment receipt"
        BOOKING_CONFIRMATION = "BOOKING_CONFIRMATION", "Booking confirmation"
        BOOKING_PENDING = "BOOKING_PENDING", "Booking pending / complete payment"
        CANCELLATION = "CANCELLATION", "Cancellation"
        REFUND = "REFUND", "Refund"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset"
        ENQUIRY = "ENQUIRY", "Enquiry / staff alert"
        REVIEW_INVITE = "REVIEW_INVITE", "Review invitation"
        GENERIC = "GENERIC", "Generic"

    # --- Envelope ----------------------------------------------------------
    to_email = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="", db_default="")
    # Optional rich HTML alternative. When present the delivery sends a proper
    # multipart/alternative message (text/plain + text/html); when blank the
    # message stays a single text/plain part exactly as before. Storing it here
    # keeps the delivery service the single source of truth for what gets sent.
    html_body = models.TextField(blank=True, default="", db_default="")
    kind = models.CharField(
        max_length=32, choices=Kind.choices, default=Kind.GENERIC, db_index=True
    )

    # --- Context (stable identifiers, never large serialized objects) ------
    booking_reference = models.CharField(max_length=80, blank=True, default="", db_index=True)
    payment_reference = models.CharField(max_length=120, blank=True, default="")
    # For attachment regeneration by the delivery service (source of truth is
    # the DB row, never a file the browser produced).
    booking_id = models.PositiveBigIntegerField(null=True, blank=True)
    attach_receipt_pdf = models.BooleanField(default=False)

    # --- Lifecycle ---------------------------------------------------------
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    # The provider's message identifier (Brevo messageId) — recorded only
    # after the provider actually accepted the message.
    provider_message_id = models.CharField(
        max_length=255, blank=True, default="", db_default=""
    )
    error_class = models.CharField(max_length=120, blank=True, default="")
    # Human-readable, credential-free reason (safe to surface to admins).
    error_message = models.TextField(blank=True, default="")
    # WHERE the failure happened (render / attachment / provider). Empty when
    # the email has not failed. Lets staff distinguish a receipt that never
    # rendered from one the email provider rejected.
    failure_stage = models.CharField(
        max_length=16, choices=FailureStage.choices, blank=True, default="", db_default=""
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_emails",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["kind", "status"]),
            models.Index(fields=["booking_reference", "status"]),
        ]

    def __str__(self):
        return f"[{self.status}] {self.kind} -> {self.to_email}"

    @property
    def is_terminal(self):
        return self.status in (self.Status.SENT, self.Status.FAILED)

    # Delivery is synchronous: a row can only legitimately sit in PENDING or
    # SENDING for the duration of one in-flight HTTP request to the provider.
    # Anything older was interrupted (deploy/restart/worker kill mid-send) and,
    # because no background worker exists, would otherwise stay non-terminal
    # forever — silently blocking the idempotency guards that treat
    # PENDING/SENDING as "an email is already on its way" (the automatic
    # receipt after payment and the staff send-receipt endpoint both do).
    STALE_AFTER = timedelta(minutes=15)

    @classmethod
    def resolve_stale(cls, queryset=None):
        """Mark interrupted PENDING/SENDING rows as FAILED (truthfully).

        Returns the number of rows resolved. Safe to call from any send path:
        rows younger than ``STALE_AFTER`` (a genuinely in-flight synchronous
        send) and terminal rows are never touched.
        """
        from django.utils import timezone

        qs = queryset if queryset is not None else cls.objects.all()
        return qs.filter(
            status__in=[cls.Status.PENDING, cls.Status.SENDING],
            created_at__lt=timezone.now() - cls.STALE_AFTER,
        ).update(
            status=cls.Status.FAILED,
            error_class="StaleDelivery",
            error_message=(
                "This delivery attempt was interrupted before the provider "
                "answered (deploy/restart mid-send). Re-send it if it is "
                "still needed."
            ),
            failure_stage=cls.FailureStage.PROVIDER,
            failed_at=timezone.now(),
        )


# Back-compat: older call sites/tests use EmailLog.FailureStage.SMTP; keep the
# name pointing at the same stored value as PROVIDER ("SMTP").
EmailLog.FailureStage.SMTP = EmailLog.FailureStage.PROVIDER
