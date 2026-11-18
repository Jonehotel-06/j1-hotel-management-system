# apps/events/models.py
"""Event Management for Reception.

Events let Reception register a guest's use of a hotel event space
(Inside Bar, Outside Bar, Hotel Compound — configurable via EventLocation)
and record the charge and collections through the existing immutable
finance ledger. No separate accounting system is introduced:

* the event charge posts as OTHER_CHARGE (Debit AR / Credit OTHER_OPERATING_REVENUE)
  so event revenue appears in the existing revenue reports;
* each successful collection posts as PAYMENT_COLLECTION (Debit the tender's
  clearing asset / Credit AR) so collections appear in collection reports;
* unpaid charges are receivables only — never collected revenue.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel

ZERO = Decimal("0.00")


class EventLocation(TimeStampedModel):
    """A bookable hotel event space. Configurable via admin/API, never hard-coded."""

    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.code = str(self.code or "").strip().upper()
        self.name = str(self.name or "").strip()
        return super().save(*args, **kwargs)


class HotelEvent(TimeStampedModel):
    class Status(models.TextChoices):
        UPCOMING = "UPCOMING", "Upcoming"
        ONGOING = "ONGOING", "Ongoing"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    class PaymentStatus(models.TextChoices):
        UNPAID = "UNPAID", "Unpaid"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        PAID = "PAID", "Paid"

    reference = models.CharField(max_length=40, unique=True, db_index=True)
    guest = models.ForeignKey(
        "bookings.Guest", on_delete=models.PROTECT, related_name="hotel_events"
    )
    location = models.ForeignKey(
        EventLocation, on_delete=models.PROTECT, related_name="events"
    )
    event_type = models.CharField(max_length=120, blank=True, default="")
    event_date = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    expected_guests = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default="NGN")
    total_charged = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)])
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID, db_index=True
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPCOMING, db_index=True)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="events_created",
    )
    charge_transaction = models.OneToOneField(
        "finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="event_charge",
    )
    idempotency_key = models.CharField(max_length=96, null=True, blank=True, unique=True, db_index=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True, default="")
    override_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-event_date", "-start_time", "-created_at"]
        indexes = [
            models.Index(fields=["location", "event_date", "status"]),
            models.Index(fields=["status", "event_date"]),
            models.Index(fields=["payment_status", "event_date"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.location.name} · {self.event_date}"

    @property
    def outstanding_balance(self) -> Decimal:
        due = (self.total_charged or ZERO) - (self.amount_paid or ZERO)
        return max(due, ZERO)

    def refresh_payment_status(self):
        if (self.amount_paid or ZERO) <= ZERO:
            self.payment_status = self.PaymentStatus.UNPAID
        elif (self.amount_paid or ZERO) >= (self.total_charged or ZERO):
            self.payment_status = self.PaymentStatus.PAID
        else:
            self.payment_status = self.PaymentStatus.PARTIALLY_PAID


class EventPayment(TimeStampedModel):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
        POS = "POS", "POS / Card"
        PAYSTACK = "PAYSTACK", "Paystack"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    event = models.ForeignKey(HotelEvent, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    currency = models.CharField(max_length=3, default="NGN")
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=64, unique=True, db_index=True)
    external_reference = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="event_payments_received",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=96, null=True, blank=True, unique=True, db_index=True)
    collection_transaction = models.OneToOneField(
        "finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="event_collection",
    )
    notes = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["event", "status"]),
            models.Index(fields=["method", "status"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.amount} {self.currency} · {self.status}"
