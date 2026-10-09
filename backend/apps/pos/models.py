"""POS catalog, kitchen workflow, and tender evidence.

Order values are price snapshots. Menu edits cannot rewrite the amount that was
shown to staff/guests or posted to the immutable financial ledger.
"""
from decimal import Decimal
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel

ZERO = Decimal("0.00")
POSITIVE_MONEY = MinValueValidator(Decimal("0.01"))


class MenuCategory(TimeStampedModel):
    class ServiceArea(models.TextChoices):
        RESTAURANT = "RESTAURANT", "Restaurant"
        BAR = "BAR", "Bar"

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    service_area = models.CharField(max_length=16, choices=ServiceArea.choices, default=ServiceArea.RESTAURANT, db_index=True)
    description = models.TextField(blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [models.Index(fields=["is_active", "sort_order"])]

    def __str__(self):
        return self.name


class MenuItem(TimeStampedModel):
    category = models.ForeignKey(MenuCategory, on_delete=models.PROTECT, related_name="items")
    name = models.CharField(max_length=180)
    sku = models.CharField(max_length=64, unique=True, db_index=True)
    description = models.TextField(blank=True, default="")
    base_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    tax_rate_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    preparation_minutes = models.PositiveSmallIntegerField(default=15)
    is_available = models.BooleanField(default=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["category__sort_order", "sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["category", "name"], name="unique_menu_item_name_per_category")]
        indexes = [
            models.Index(fields=["category", "is_active", "is_available"]),
            models.Index(fields=["is_active", "is_available", "sort_order"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"


class MenuModifier(TimeStampedModel):
    """Optional priced choice, e.g. extra cheese or a protein upgrade."""

    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name="modifiers")
    name = models.CharField(max_length=120)
    price_delta = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["menu_item", "name"], name="unique_modifier_name_per_item")]
        indexes = [models.Index(fields=["menu_item", "is_active"])]

    def __str__(self):
        return f"{self.menu_item.name} · {self.name}"


def _new_restaurant_table_session_reference():
    return f"RTS-{timezone.now():%Y%m%d}-{secrets.token_hex(8).upper()}"


class RestaurantTable(TimeStampedModel):
    """Configured dining table; occupancy is derived from its open session."""

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=100, blank=True, default="")
    section = models.CharField(max_length=80, blank=True, default="")
    seats = models.PositiveSmallIntegerField(default=2, validators=[MinValueValidator(1), MaxValueValidator(60)])
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="restaurant_tables_created"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="restaurant_tables_updated"
    )

    class Meta:
        ordering = ["section", "code", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(seats__gte=1) & Q(seats__lte=60), name="restaurant_table_seats_range"),
        ]
        indexes = [models.Index(fields=["is_active", "section", "code"])]

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Restaurant table history is retained; deactivate the table instead.")

    def __str__(self):
        return f"{self.code} · {self.name}" if self.name else self.code


class RestaurantTableSession(TimeStampedModel):
    """One open service occasion at one configured restaurant table.

    Separate POS orders can be linked to one session for split checks. A
    nullable unique ``active_table`` is portable across MySQL and SQLite and
    enforces at most one open session per table under concurrent requests.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    reference = models.CharField(max_length=64, unique=True, default=_new_restaurant_table_session_reference, db_index=True)
    table = models.ForeignKey(RestaurantTable, on_delete=models.PROTECT, related_name="sessions")
    active_table = models.OneToOneField(
        RestaurantTable, null=True, blank=True, on_delete=models.PROTECT, related_name="open_session"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True)
    covers = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(60)])
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="restaurant_table_sessions_opened"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="restaurant_table_sessions_closed"
    )
    idempotency_key = models.CharField(max_length=96, unique=True, null=True, blank=True)
    idempotency_fingerprint = models.CharField(max_length=64, blank=True, default="")
    notes = models.CharField(max_length=500, blank=True, default="")
    close_note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-opened_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(status="OPEN", active_table__isnull=False, closed_at__isnull=True)
                    | Q(status="CLOSED", active_table__isnull=True, closed_at__isnull=False)
                ),
                name="restaurant_table_session_state_fields",
            ),
            models.CheckConstraint(condition=Q(covers__gte=1) & Q(covers__lte=60), name="restaurant_table_session_covers_range"),
        ]
        indexes = [models.Index(fields=["status", "opened_at"]), models.Index(fields=["table", "status"])]

    def save(self, *args, **kwargs):
        if self.status == self.Status.OPEN:
            if self.active_table_id != self.table_id or self.closed_at is not None:
                raise ValidationError("An open restaurant table session must own its active table and have no close time.")
        elif self.status == self.Status.CLOSED:
            if self.active_table_id is not None or self.closed_at is None:
                raise ValidationError("A closed restaurant table session cannot hold a table and needs a close time.")
        else:
            raise ValidationError("Invalid restaurant table session state.")
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior:
                changed = {
                    field.attname for field in self._meta.concrete_fields
                    if getattr(prior, field.attname) != getattr(self, field.attname)
                }
                allowed = {"status", "active_table_id", "closed_at", "closed_by_id", "close_note", "updated_at"}
                if prior.status == self.Status.CLOSED or not changed.issubset(allowed):
                    raise ValidationError("Restaurant table session history cannot be rewritten or reopened.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Restaurant table session history cannot be deleted.")

    def __str__(self):
        return f"{self.reference} · {self.table.code} · {self.status}"


class PosOrder(TimeStampedModel):
    class Mode(models.TextChoices):
        ROOM_SERVICE = "ROOM_SERVICE", "Room service"
        RESTAURANT = "RESTAURANT", "Restaurant / dine in"
        BAR = "BAR", "Bar"
        TAKEAWAY = "TAKEAWAY", "Takeaway"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted to kitchen"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        DELIVERED = "DELIVERED", "Delivered / served"
        CANCELLED = "CANCELLED", "Cancelled before delivery"
        VOIDED = "VOIDED", "Voided by controlled reversal"

    class SettlementStatus(models.TextChoices):
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"
        UNPAID = "UNPAID", "Unpaid"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        PAID = "PAID", "Paid"
        REFUNDED = "REFUNDED", "Refunded"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=96, null=True, blank=True, unique=True)
    mode = models.CharField(max_length=20, choices=Mode.choices, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    settlement_status = models.CharField(
        max_length=20, choices=SettlementStatus.choices, default=SettlementStatus.NOT_APPLICABLE, db_index=True
    )
    stay = models.ForeignKey(
        "stays.Stay", null=True, blank=True, on_delete=models.PROTECT, related_name="pos_orders"
    )
    folio = models.ForeignKey(
        "finance.Folio", null=True, blank=True, on_delete=models.PROTECT, related_name="pos_orders"
    )
    guest_name = models.CharField(max_length=200, blank=True, default="")
    table_number = models.CharField(max_length=40, blank=True, default="")
    table_session = models.ForeignKey(
        "RestaurantTableSession", null=True, blank=True, on_delete=models.PROTECT, related_name="orders"
    )
    service_request = models.OneToOneField(
        "guest_services.ServiceRequest", null=True, blank=True, on_delete=models.PROTECT, related_name="pos_order"
    )
    delivery_location = models.CharField(max_length=160, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    currency = models.CharField(max_length=3, default="NGN")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="pos_orders_created"
    )
    charge_transaction = models.OneToOneField(
        "finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="pos_order_charge",
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["mode", "status", "-created_at"]),
            models.Index(fields=["stay", "status"]),
            models.Index(fields=["folio", "status"]),
            models.Index(fields=["table_session", "status"], name="pos_ord_tblsess_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(mode="ROOM_SERVICE")
                    | (Q(stay__isnull=False) & Q(folio__isnull=False))
                ),
                name="room_service_order_requires_stay_folio",
            ),
        ]

    def __str__(self):
        return f"{self.reference} · {self.mode} · {self.status}"


class PosOrderLine(models.Model):
    """Immutable price/name snapshot once an order is submitted."""

    order = models.ForeignKey(PosOrder, on_delete=models.PROTECT, related_name="lines")
    menu_item = models.ForeignKey(MenuItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="order_lines")
    item_name = models.CharField(max_length=180)
    item_sku = models.CharField(max_length=64, blank=True, default="")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    modifiers_snapshot = models.JSONField(default=list, blank=True)
    modifier_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    notes = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["order", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).values("order__status").first()
            if prior and prior["order__status"] != PosOrder.Status.DRAFT:
                raise ValidationError("Submitted POS order lines are immutable.")
        if self.order_id and PosOrder.objects.filter(
            pk=self.order_id
        ).exclude(status=PosOrder.Status.DRAFT).exists():
            raise ValidationError("Lines can only be added while a POS order is a draft.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.order_id and PosOrder.objects.exclude(status=PosOrder.Status.DRAFT).filter(pk=self.order_id).exists():
            raise ValidationError("Submitted POS order lines cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity} × {self.item_name}"


class KitchenTicket(TimeStampedModel):
    class Station(models.TextChoices):
        KITCHEN = "KITCHEN", "Kitchen"
        BAR = "BAR", "Bar"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    order = models.OneToOneField(PosOrder, on_delete=models.PROTECT, related_name="kitchen_ticket")
    station = models.CharField(max_length=16, choices=Station.choices, default=Station.KITCHEN, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    priority = models.PositiveSmallIntegerField(default=0)
    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="kitchen_tickets"
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-priority", "queued_at", "pk"]
        indexes = [
            models.Index(fields=["status", "priority", "queued_at"]),
            models.Index(fields=["station", "status", "queued_at"]),
        ]

    def __str__(self):
        return f"KOT {self.order.reference} · {self.status}"


class PosTender(TimeStampedModel):
    """Tender/cash collection for direct POS sales, never a mutable order total."""

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        CARD = "CARD", "Card / POS terminal"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CAPTURED = "CAPTURED", "Captured"
        VOIDED = "VOIDED", "Voided"
        REFUNDED = "REFUNDED", "Refunded"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    order = models.ForeignKey(PosOrder, on_delete=models.PROTECT, related_name="tenders")
    method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[POSITIVE_MONEY])
    currency = models.CharField(max_length=3, default="NGN")
    external_reference = models.CharField(max_length=120, blank=True, default="")
    cash_session = models.ForeignKey(
        "finance.CashSession", null=True, blank=True, on_delete=models.PROTECT, related_name="pos_tenders"
    )
    captured_at = models.DateTimeField(null=True, blank=True)
    captured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="pos_tenders_captured"
    )
    collection_transaction = models.OneToOneField(
        "finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="pos_tender_collection",
    )
    notes = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["order", "status"]),
            models.Index(fields=["cash_session", "status"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.amount} · {self.status}"


class PosOrderEvent(models.Model):
    """Append-only operational and financial workflow evidence."""

    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        SUBMITTED = "SUBMITTED", "Submitted"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"
        CHARGED = "CHARGED", "Charged to folio"
        TENDER_CAPTURED = "TENDER_CAPTURED", "Tender captured"
        VOID_REQUESTED = "VOID_REQUESTED", "Void requested"

    order = models.ForeignKey(PosOrder, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=24, choices=Type.choices, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="pos_order_events"
    )
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        indexes = [models.Index(fields=["order", "occurred_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("POS order events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("POS order events are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.order.reference} · {self.type}"


class RestaurantTableSessionEvent(models.Model):
    """Append-only table-service history, including split-check order links."""

    class Type(models.TextChoices):
        OPENED = "OPENED", "Opened"
        ORDER_ADDED = "ORDER_ADDED", "POS order added"
        CLOSED = "CLOSED", "Closed"

    table_session = models.ForeignKey(
        RestaurantTableSession, on_delete=models.PROTECT, related_name="events"
    )
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    order = models.ForeignKey(
        PosOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="table_session_events"
    )
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="restaurant_table_session_events",
    )
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        indexes = [models.Index(fields=["table_session", "occurred_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Restaurant table session events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Restaurant table session events cannot be deleted.")

    def __str__(self):
        return f"{self.table_session.reference} · {self.type}"
