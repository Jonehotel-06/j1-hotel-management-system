# apps/payments/models.py
"""Payment and refund records.

Card data never touches this system: Paystack hosts the card capture. We only
store transaction and refund metadata sent back by Paystack that is safe to
keep. Paystack transaction references are the ``Payment.reference`` values for
online payments; hotel booking references remain on ``Booking.booking_reference``.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.bookings.models import Booking
from apps.core.models import TimeStampedModel


class Payment(TimeStampedModel):
    class Provider(models.TextChoices):
        PAYSTACK = "PAYSTACK", "Paystack (online)"
        CASH = "CASH", "Cash"
        POS = "POS", "POS Terminal"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        REFUNDED = "REFUNDED", "Refunded"

    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="payments_made",
        help_text="Payer (online) or staff member who recorded the payment (offline).",
    )
    reference = models.CharField(
        max_length=60, unique=True, db_index=True,
        help_text="Internal payment reference. For Paystack payments, this is also the Paystack transaction reference.",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.PAYSTACK)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="NGN")
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    channel = models.CharField(max_length=40, blank=True, default="")  # card / bank / ussd / cash...
    gateway_response = models.CharField(max_length=255, blank=True, default="")
    transaction_id = models.CharField(max_length=60, blank=True, default="", db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Whitelisted gateway metadata only — never raw authorization/card data.
    metadata = models.JSONField(default=dict, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["provider", "status"]),
            # Revenue reports filter successful collections by paid timestamp.
            models.Index(fields=["status", "paid_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.amount} {self.currency} · {self.status}"

    @property
    def paystack_reference(self):
        return self.reference if self.provider == self.Provider.PAYSTACK else ""


class Refund(TimeStampedModel):
    """Local lifecycle record for a Paystack or manual refund attempt.

    A refund is intentionally separate from both Booking and Payment state:
    a booking may be CANCELLED while the payment remains SUCCESS and the refund
    is PENDING/PROCESSING. Only a provider-confirmed processed refund updates
    payment/booking refund totals.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        NEEDS_ATTENTION = "NEEDS_ATTENTION", "Needs attention"

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="refunds")
    # Added in migration 0003 after enquiries. String reference avoids import cycles.
    cancellation_request = models.ForeignKey(
        "enquiries.Enquiry", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="refunds",
        help_text="Cancellation/refund enquiry that authorized this refund, when applicable.",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="NGN")
    paystack_transaction_reference = models.CharField(max_length=60, db_index=True)
    paystack_transaction_id = models.CharField(max_length=60, blank=True, default="", db_index=True)
    paystack_refund_id = models.CharField(max_length=60, blank=True, default="", db_index=True)
    paystack_refund_reference = models.CharField(max_length=100, blank=True, default="", db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    customer_note = models.TextField(blank=True, default="")
    merchant_note = models.TextField(blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="refunds_requested",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["payment", "status"]),
            models.Index(fields=["paystack_refund_reference"]),
        ]
        constraints = [
            # One live refund attempt per payment prevents double-clicks/browser
            # retries from creating duplicate Paystack refunds. Failed attempts
            # are retried by reusing/updating the existing cancellation-request row.
            models.UniqueConstraint(
                fields=["payment"],
                condition=Q(status__in=["PENDING", "PROCESSING", "NEEDS_ATTENTION"]),
                name="unique_active_refund_per_payment",
            ),
            models.UniqueConstraint(
                fields=["cancellation_request"],
                condition=Q(cancellation_request__isnull=False),
                name="unique_refund_per_cancellation_request",
            ),
            models.UniqueConstraint(
                fields=["paystack_refund_id"],
                condition=~Q(paystack_refund_id=""),
                name="unique_paystack_refund_id_when_present",
            ),
            models.UniqueConstraint(
                fields=["paystack_refund_reference"],
                condition=~Q(paystack_refund_reference=""),
                name="unique_paystack_refund_ref_when_present",
            ),
        ]

    def __str__(self):
        return f"Refund {self.amount} {self.currency} for {self.paystack_transaction_reference} · {self.status}"
