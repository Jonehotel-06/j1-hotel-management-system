# apps/enquiries/models.py
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class Enquiry(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    class EnquiryType(models.TextChoices):
        GENERAL = "GENERAL", "General enquiry"
        CANCELLATION = "CANCELLATION", "Cancellation / refund request"

    class CancellationStatus(models.TextChoices):
        NEW = "NEW", "New"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Booking cancelled"
        REFUND_PENDING = "REFUND_PENDING", "Refund pending"
        REFUND_PROCESSING = "REFUND_PROCESSING", "Refund processing"
        REFUNDED = "REFUNDED", "Refunded"
        REFUND_FAILED = "REFUND_FAILED", "Refund failed"
        CLOSED = "CLOSED", "Closed"

    class RefundStatus(models.TextChoices):
        NONE = "NONE", "No refund reviewed"
        NOT_REQUIRED = "NOT_REQUIRED", "No refund required"
        DUE = "DUE", "Refund due"
        MANUAL_REQUIRED = "MANUAL_REQUIRED", "Manual refund required"
        PENDING = "PENDING", "Refund pending"
        PROCESSING = "PROCESSING", "Refund processing"
        PROCESSED = "PROCESSED", "Refund processed"
        FAILED = "FAILED", "Refund failed"
        NEEDS_ATTENTION = "NEEDS_ATTENTION", "Needs attention"

    class PreferredContactMethod(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        PHONE = "PHONE", "Phone"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    name = models.CharField(max_length=120)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=20, blank=True, default="")
    subject = models.CharField(max_length=150)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.NEW, db_index=True)
    internal_notes = models.TextField(blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    enquiry_type = models.CharField(
        max_length=20, choices=EnquiryType.choices, default=EnquiryType.GENERAL, db_index=True
    )
    cancellation_reference = models.CharField(max_length=60, unique=True, null=True, blank=True, db_index=True)
    public_access_token_hash = models.CharField(max_length=128, blank=True, default="", db_index=True)
    public_access_expires_at = models.DateTimeField(null=True, blank=True)
    booking_reference = models.CharField(max_length=60, blank=True, default="", db_index=True)
    payment_reference = models.CharField(
        max_length=80, blank=True, default="", db_index=True,
        help_text="Guest-supplied Paystack transaction/payment reference, if available.",
    )
    receipt_reference = models.CharField(max_length=80, blank=True, default="")
    cancellation_reason = models.TextField(blank=True, default="")
    preferred_contact_method = models.CharField(
        max_length=20, choices=PreferredContactMethod.choices, blank=True, default=""
    )
    refund_requested = models.BooleanField(default=False)
    related_booking = models.ForeignKey(
        "bookings.Booking", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cancellation_enquiries",
    )
    related_payment = models.ForeignKey(
        "payments.Payment", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cancellation_enquiries",
    )
    cancellation_status = models.CharField(
        max_length=30, choices=CancellationStatus.choices, default=CancellationStatus.NEW, db_index=True
    )
    refund_status = models.CharField(
        max_length=30, choices=RefundStatus.choices, default=RefundStatus.NONE, db_index=True
    )
    calculated_cancellation_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    calculated_refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paystack_refund_reference = models.CharField(max_length=100, blank=True, default="", db_index=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="processed_enquiries",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    email_events = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Enquiries"
        indexes = [
            models.Index(fields=["enquiry_type", "status"]),
            models.Index(fields=["cancellation_status", "refund_status"]),
            models.Index(fields=["booking_reference"]),
            models.Index(fields=["payment_reference"]),
        ]

    def __str__(self):
        return f"{self.subject} — {self.name}"

    @staticmethod
    def hash_public_access_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue_public_access_token(self, *, days=90, save=True):
        token = secrets.token_urlsafe(32)
        self.public_access_token_hash = self.hash_public_access_token(token)
        self.public_access_expires_at = timezone.now() + timedelta(days=days)
        if save:
            self.save(update_fields=["public_access_token_hash", "public_access_expires_at", "updated_at"])
        return token

    def public_token_matches(self, token):
        return bool(
            token
            and self.public_access_token_hash
            and self.public_access_expires_at
            and self.public_access_expires_at > timezone.now()
            and secrets.compare_digest(self.public_access_token_hash, self.hash_public_access_token(token))
        )
