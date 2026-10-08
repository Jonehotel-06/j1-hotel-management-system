"""Authoritative, append-only financial records and operational cash controls.

``Booking`` and legacy ``Payment`` projections remain available while this app is
introduced.  The records here are deliberately distinct from those projections:
posted financial transactions and their lines are immutable evidence, while a
reversal is a new balanced transaction rather than an update or delete.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.utils import hotel_today


ZERO = Decimal("0.00")
POSITIVE_MONEY = MinValueValidator(Decimal("0.01"))


class ImmutableFinancialModel(models.Model):
    """Base for records which form permanent financial/audit evidence."""

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ValidationError("Financial evidence is append-only and cannot be deleted.")


class FinancialControlPolicy(TimeStampedModel):
    """Effective-dated configurable controls; values are snapshotted on requests."""

    name = models.CharField(max_length=120, unique=True)
    effective_from = models.DateField(default=hotel_today, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    require_separate_approver = models.BooleanField(default=True)
    refund_manager_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    discount_manager_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    cash_variance_manager_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("1000.00"), validators=[MinValueValidator(ZERO)]
    )
    expense_manager_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-effective_from", "-pk"]
        indexes = [models.Index(fields=["is_active", "effective_from"])]

    def __str__(self):
        return f"{self.name} ({self.effective_from})"


class Folio(TimeStampedModel):
    """Guest-facing balance container, normally opened once a Stay begins."""

    class Kind(models.TextChoices):
        MAIN = "MAIN", "Main guest folio"
        INCIDENTAL = "INCIDENTAL", "Incidental folio"
        GROUP = "GROUP", "Group/master folio"
        HOUSE = "HOUSE", "House account"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ON_HOLD = "ON_HOLD", "On hold"
        CLOSED = "CLOSED", "Closed"
        VOIDED = "VOIDED", "Voided"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    stay = models.ForeignKey(
        "stays.Stay", null=True, blank=True, on_delete=models.PROTECT, related_name="folios"
    )
    booking = models.ForeignKey(
        "bookings.Booking", null=True, blank=True, on_delete=models.PROTECT, related_name="folios"
    )
    guest = models.ForeignKey("bookings.Guest", on_delete=models.PROTECT, related_name="folios")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.MAIN, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    currency = models.CharField(max_length=3, default="NGN")
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="folios_opened"
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="folios_closed"
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-opened_at", "-pk"]
        indexes = [
            models.Index(fields=["stay", "status"]),
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["guest", "status"]),
            models.Index(fields=["status", "opened_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(stay__isnull=False) | Q(booking__isnull=False),
                name="folio_requires_stay_or_booking",
            ),
            models.UniqueConstraint(fields=["stay", "kind"], name="one_folio_kind_per_stay"),
        ]

    def __str__(self):
        return f"{self.reference} · {self.get_status_display()}"


class FinancialTransaction(TimeStampedModel, ImmutableFinancialModel):
    """Balanced journal header. Posted transactions must never be edited."""

    class Type(models.TextChoices):
        ACCOMMODATION_CHARGE = "ACCOMMODATION_CHARGE", "Accommodation charge"
        POS_CHARGE = "POS_CHARGE", "POS / room-service charge"
        OTHER_CHARGE = "OTHER_CHARGE", "Other guest charge"
        PAYMENT_COLLECTION = "PAYMENT_COLLECTION", "Payment collection"
        PAYMENT_ALLOCATION = "PAYMENT_ALLOCATION", "Apply payment to folio"
        REFUND = "REFUND", "Refund"
        DISCOUNT = "DISCOUNT", "Discount"
        VOID = "VOID", "Void / reversal"
        EXPENSE = "EXPENSE", "Expense"
        CASH_OPEN = "CASH_OPEN", "Cash drawer opening"
        CASH_CLOSE = "CASH_CLOSE", "Cash drawer closing"
        CASH_MOVEMENT = "CASH_MOVEMENT", "Cash movement"
        ADJUSTMENT = "ADJUSTMENT", "Controlled adjustment"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        VOIDED = "VOIDED", "Voided by reversal"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    type = models.CharField(max_length=30, choices=Type.choices, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    business_date = models.DateField(default=hotel_today, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    posted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    currency = models.CharField(max_length=3, default="NGN")
    # Stable source/idempotency keys make retried provider webhooks and local
    # double-clicks converge to one ledger event.
    source_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    idempotency_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    source_reference = models.CharField(max_length=120, blank=True, default="", db_index=True)
    external_reference = models.CharField(max_length=160, blank=True, default="", db_index=True)
    narrative = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="financial_transactions_initiated",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="financial_transactions_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    reversal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals"
    )

    class Meta:
        ordering = ["-business_date", "-created_at", "-pk"]
        indexes = [
            models.Index(fields=["type", "business_date", "status"]),
            models.Index(fields=["status", "currency", "business_date"]),
            models.Index(fields=["status", "posted_at"]),
            models.Index(fields=["source_reference", "type"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).values("status").first()
            if prior and prior["status"] in {self.Status.POSTED, self.Status.VOIDED}:
                raise ValidationError("Posted financial transactions are immutable; post a reversal instead.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} · {self.type} · {self.status}"


class FinancialLine(ImmutableFinancialModel):
    """A debit or credit within one balanced financial transaction."""

    class Direction(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    transaction = models.ForeignKey(FinancialTransaction, on_delete=models.PROTECT, related_name="lines")
    folio = models.ForeignKey(
        Folio, null=True, blank=True, on_delete=models.PROTECT, related_name="financial_lines"
    )
    account_code = models.CharField(max_length=64, db_index=True)
    direction = models.CharField(max_length=6, choices=Direction.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    description = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["folio", "created_at"]),
            models.Index(fields=["account_code", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Financial lines are append-only; create a reversal instead.")
        if self.transaction_id and FinancialTransaction.objects.filter(
            pk=self.transaction_id,
            status__in=[FinancialTransaction.Status.POSTED, FinancialTransaction.Status.VOIDED],
        ).exists():
            raise ValidationError("Cannot add a line to a posted financial transaction.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.transaction.reference} · {self.direction} {self.amount} {self.account_code}"


class FolioPosting(ImmutableFinancialModel):
    """Immutable statement item derived from a balanced financial line."""

    class Kind(models.TextChoices):
        CHARGE = "CHARGE", "Charge"
        PAYMENT = "PAYMENT", "Payment"
        REFUND = "REFUND", "Refund"
        DISCOUNT = "DISCOUNT", "Discount"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        TRANSFER = "TRANSFER", "Transfer"

    class Effect(models.TextChoices):
        DEBIT = "DEBIT", "Increases guest balance"
        CREDIT = "CREDIT", "Reduces guest balance"

    folio = models.ForeignKey(Folio, on_delete=models.PROTECT, related_name="postings")
    transaction = models.ForeignKey(FinancialTransaction, on_delete=models.PROTECT, related_name="folio_postings")
    line = models.OneToOneField(FinancialLine, on_delete=models.PROTECT, related_name="folio_posting")
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    effect = models.CharField(max_length=6, choices=Effect.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    description = models.CharField(max_length=500, blank=True, default="")
    business_date = models.DateField(default=hotel_today, db_index=True)
    source_reference = models.CharField(max_length=120, blank=True, default="", db_index=True)
    reversal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal_postings"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["business_date", "created_at", "pk"]
        indexes = [
            models.Index(fields=["folio", "business_date", "created_at"]),
            models.Index(fields=["transaction", "kind"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Folio postings are immutable; create an offsetting posting instead.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.folio.reference} · {self.kind} · {self.amount}"


class PaymentAllocation(ImmutableFinancialModel):
    """Immutable application of a legacy/new payment collection to one folio."""

    payment = models.ForeignKey(
        "payments.Payment", on_delete=models.PROTECT, related_name="folio_allocations"
    )
    transaction = models.ForeignKey(
        FinancialTransaction, on_delete=models.PROTECT, related_name="payment_allocations"
    )
    folio = models.ForeignKey(Folio, on_delete=models.PROTECT, related_name="payment_allocations")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    source_key = models.CharField(max_length=160, unique=True)
    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="payment_allocations_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["folio", "created_at"]),
            models.Index(fields=["payment", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Payment allocations are immutable; create a correction entry instead.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.payment.reference} → {self.folio.reference} · {self.amount}"


class RefundApplication(ImmutableFinancialModel):
    """Immutable portion of a refund applied against a payment/folio application."""

    refund = models.ForeignKey("payments.Refund", on_delete=models.PROTECT, related_name="finance_applications")
    payment = models.ForeignKey("payments.Payment", on_delete=models.PROTECT, related_name="refund_applications")
    payment_allocation = models.ForeignKey(
        PaymentAllocation, null=True, blank=True, on_delete=models.PROTECT, related_name="refund_applications"
    )
    transaction = models.ForeignKey(
        FinancialTransaction, on_delete=models.PROTECT, related_name="refund_applications"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    source_key = models.CharField(max_length=180, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["payment", "created_at"]),
            models.Index(fields=["refund", "created_at"]),
            models.Index(fields=["payment_allocation", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Refund applications are immutable; create a correction entry instead.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Refund {self.refund_id} · {self.amount}"


class CashSession(TimeStampedModel):
    """A controlled cashier drawer session; movements remain separate evidence."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        PENDING_REVIEW = "PENDING_REVIEW", "Pending variance review"
        CLOSED = "CLOSED", "Closed"
        VOIDED = "VOIDED", "Voided"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cash_sessions")
    business_date = models.DateField(default=hotel_today, db_index=True)
    location = models.CharField(max_length=120, blank=True, default="")
    terminal = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="cash_sessions_opened"
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="cash_sessions_closed"
    )
    review_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cash_session_variances_approved",
    )
    review_approved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-opened_at", "-pk"]
        indexes = [
            models.Index(fields=["cashier", "status"]),
            models.Index(fields=["business_date", "status"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.cashier} · {self.status}"


class CashMovement(ImmutableFinancialModel):
    """Append-only cash-drawer movement tied to a finance transaction when applicable."""

    class Type(models.TextChoices):
        OPENING_FLOAT = "OPENING_FLOAT", "Opening float"
        COLLECTION = "COLLECTION", "Cash collection"
        REFUND = "REFUND", "Cash refund"
        PAID_OUT = "PAID_OUT", "Paid out"
        SAFE_DROP = "SAFE_DROP", "Safe drop"
        CASH_IN = "CASH_IN", "Cash in"
        CASH_OUT = "CASH_OUT", "Cash out"
        CLOSING_COUNT = "CLOSING_COUNT", "Closing count"

    cash_session = models.ForeignKey(CashSession, on_delete=models.PROTECT, related_name="movements")
    transaction = models.ForeignKey(
        FinancialTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="cash_movements"
    )
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="cash_movements"
    )
    source_reference = models.CharField(max_length=120, blank=True, default="", db_index=True)
    notes = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        indexes = [models.Index(fields=["cash_session", "occurred_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Cash movements are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.cash_session.reference} · {self.type} · {self.amount}"


class ApprovalRequest(TimeStampedModel):
    """Maker-checker evidence for sensitive monetary actions."""

    class Type(models.TextChoices):
        REFUND = "REFUND", "Refund"
        DISCOUNT = "DISCOUNT", "Discount"
        VOID = "VOID", "Void"
        CASH_VARIANCE = "CASH_VARIANCE", "Cash variance"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        EXPENSE = "EXPENSE", "Expense"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    request_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    policy = models.ForeignKey(
        FinancialControlPolicy, null=True, blank=True, on_delete=models.SET_NULL, related_name="approval_requests"
    )
    policy_snapshot = models.JSONField(default=dict, blank=True)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="finance_approval_requests"
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="finance_approval_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_reference = models.CharField(max_length=120, blank=True, default="", db_index=True)
    financial_transaction = models.ForeignKey(
        FinancialTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="approval_requests"
    )
    reason = models.TextField(blank=True, default="")
    review_note = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "type", "created_at"]),
            models.Index(fields=["requester", "status"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.type} · {self.status}"


class Expense(TimeStampedModel):
    """Controlled expense workflow; posting creates immutable finance evidence."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        POSTED = "POSTED", "Posted"
        VOIDED = "VOIDED", "Voided"

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash paid out"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CARD = "CARD", "Card / terminal"
        PAYABLE = "PAYABLE", "Accounts payable"

    class AccountingClass(models.TextChoices):
        OPERATING = "OPERATING", "Operating expense"
        INVENTORY = "INVENTORY", "Inventory asset"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=180, null=True, blank=True, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    category = models.CharField(max_length=120, db_index=True)
    accounting_class = models.CharField(max_length=16, choices=AccountingClass.choices, default=AccountingClass.OPERATING)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    incurred_on = models.DateField(default=hotel_today, db_index=True)
    payee = models.CharField(max_length=255, blank=True, default="")
    supplier_reference = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")
    receipt_url = models.URLField(blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expenses_requested"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses_approved"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_request = models.OneToOneField(
        "ApprovalRequest", null=True, blank=True, on_delete=models.PROTECT, related_name="expense_request"
    )
    cash_session = models.ForeignKey(
        CashSession, null=True, blank=True, on_delete=models.PROTECT, related_name="expenses"
    )
    financial_transaction = models.OneToOneField(
        FinancialTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="expense"
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses_posted"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-incurred_on", "-created_at"]
        indexes = [
            models.Index(fields=["status", "incurred_on"]),
            models.Index(fields=["category", "incurred_on"]),
            models.Index(fields=["requested_by", "status", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("Posted expenses are immutable; record a separate correction or reversal rather than editing history.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.POSTED).exists():
            raise ValidationError("Posted expenses cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} · {self.amount} {self.currency} · {self.status}"


class ExpenseEvent(models.Model):
    """Append-only evidence for expense workflow transitions."""

    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        POSTED = "POSTED", "Posted"

    expense = models.ForeignKey(Expense, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="expense_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["expense", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Expense events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Expense events are append-only and cannot be deleted.")


class FinancialEvent(ImmutableFinancialModel):
    """Mandatory append-only audit trail for financial state transitions."""

    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        POSTED = "POSTED", "Posted"
        APPROVED = "APPROVED", "Approved"
        REVIEWED = "REVIEWED", "Reviewed"
        REVERSED = "REVERSED", "Reversed"
        ALLOCATED = "ALLOCATED", "Allocated"
        CASH_SESSION_OPENED = "CASH_SESSION_OPENED", "Cash session opened"
        CASH_SESSION_CLOSED = "CASH_SESSION_CLOSED", "Cash session closed"
        CASH_VARIANCE_REVIEWED = "CASH_VARIANCE_REVIEWED", "Cash variance reviewed"

    transaction = models.ForeignKey(
        FinancialTransaction, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    approval_request = models.ForeignKey(
        ApprovalRequest, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    cash_session = models.ForeignKey(
        CashSession, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    type = models.CharField(max_length=30, choices=Type.choices, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="financial_events"
    )
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        indexes = [
            models.Index(fields=["transaction", "occurred_at"]),
            models.Index(fields=["approval_request", "occurred_at"]),
            models.Index(fields=["cash_session", "occurred_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Financial events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.type} · {self.occurred_at:%Y-%m-%d %H:%M}"
