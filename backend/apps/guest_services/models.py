"""Guest-service requests and immutable operational activity evidence."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class ServiceRequest(TimeStampedModel):
    """A routed guest need; mutable projection with append-only event history."""

    class Category(models.TextChoices):
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeeping"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        ROOM_SERVICE = "ROOM_SERVICE", "Room service"
        FOOD_BEVERAGE = "FOOD_BEVERAGE", "Food & beverage service"
        AMENITY = "AMENITY", "Amenity / supplies"
        TRANSPORT = "TRANSPORT", "Transport"
        BILLING = "BILLING", "Billing / folio"
        GENERAL = "GENERAL", "General assistance"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class Channel(models.TextChoices):
        PORTAL = "PORTAL", "Guest portal"
        QR = "QR", "Room / table QR"
        FRONT_DESK = "FRONT_DESK", "Front desk"
        PHONE = "PHONE", "Phone"
        IN_PERSON = "IN_PERSON", "In person"
        STAFF = "STAFF", "Staff created"

    class OwnerTeam(models.TextChoices):
        FRONT_DESK = "FRONT_DESK", "Front desk"
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeeping"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        FOOD_BEVERAGE = "FOOD_BEVERAGE", "Food & beverage"
        MANAGEMENT = "MANAGEMENT", "Management"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"
        ESCALATED = "ESCALATED", "Escalated"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    # A server-scoped digest, not an untrusted raw client key. It is global so
    # retries converge even under concurrent requests without cross-guest reuse.
    idempotency_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    guest = models.ForeignKey(
        "bookings.Guest", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests"
    )
    # When the person asking is a recorded room guest (not the booking contact),
    # this points at THEIR row. The request still belongs to the booking's guest
    # for folio/billing purposes, but the desk and the audit trail can name the
    # person who actually raised it.
    occupant = models.ForeignKey(
        "bookings.RoomOccupant", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_requests",
    )
    stay = models.ForeignKey(
        "stays.Stay", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests"
    )
    room = models.ForeignKey(
        "rooms.Room", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests"
    )
    table_number = models.CharField(max_length=40, blank=True, default="")
    qr_link = models.ForeignKey(
        "guest_services.ServiceQRLink", null=True, blank=True, on_delete=models.PROTECT,
        related_name="service_requests",
    )
    category = models.CharField(max_length=30, choices=Category.choices, db_index=True)
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    owner_team = models.CharField(max_length=20, choices=OwnerTeam.choices, default=OwnerTeam.FRONT_DESK, db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_service_requests",
    )
    summary = models.CharField(max_length=255)
    detail = models.TextField(blank=True, default="")
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_requests_created",
    )
    # Retain the verified portal identity as a snapshot even if a Guest email
    # is later corrected; never expose it in general staff list output by default.
    portal_email = models.EmailField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "assigned_to", "due_at"]),
            models.Index(fields=["owner_team", "status", "due_at"]),
            models.Index(fields=["stay", "created_at"]),
            models.Index(fields=["room", "status"]),
            models.Index(fields=["guest", "created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.summary}"


def _default_service_qr_expiry():
    """Expiry for a newly issued QR token (callable default keeps migrations and direct creates consistent)."""
    from datetime import timedelta

    hours = int(getattr(settings, "SERVICE_QR_LINK_TTL_HOURS", 720))
    return timezone.now() + timedelta(hours=hours)


class ServiceQRLink(TimeStampedModel):
    """Revocable bearer link for an in-room or table-side service request."""

    class TargetType(models.TextChoices):
        ROOM = "ROOM", "Guest room"
        TABLE = "TABLE", "Restaurant / bar table"

    reference = models.CharField(max_length=48, unique=True, db_index=True)
    target_key = models.CharField(max_length=128, unique=True, editable=False)
    target_type = models.CharField(max_length=12, choices=TargetType.choices, db_index=True)
    room = models.ForeignKey(
        "rooms.Room", null=True, blank=True, on_delete=models.PROTECT, related_name="service_qr_links"
    )
    table_number = models.CharField(max_length=40, blank=True, default="")
    label = models.CharField(max_length=160, blank=True, default="")
    # The bearer value is shown only at creation/rotation; the database keeps
    # only its SHA-256 digest. Links are disabled, never deleted, for auditability.
    token_hash = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True, db_index=True)
    # Null (legacy/unknown) fails closed: a token without a valid expiry is refused.
    expires_at = models.DateTimeField(null=True, blank=True, default=_default_service_qr_expiry, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_qr_links_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_qr_links_updated",
    )

    class Meta:
        ordering = ["target_type", "label", "reference"]
        indexes = [models.Index(fields=["is_active", "target_type", "updated_at"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(target_type="ROOM", room__isnull=False, table_number="")
                    | (models.Q(target_type="TABLE", room__isnull=True) & ~models.Q(table_number=""))
                ),
                name="service_qr_target_shape",
            ),
        ]

    def __str__(self):
        return f"{self.reference} · {self.label or self.target_type}"

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values("target_key", "target_type", "room_id", "table_number").first()
            if original and any(original[field] != getattr(self, field) for field in ("target_key", "target_type", "room_id", "table_number")):
                raise ValidationError("A service QR target is immutable; create a new link for another location.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Service QR history is retained; revoke the link instead.")


class ServiceRequestEvent(models.Model):
    """Append-only status, assignment, escalation, and conversation evidence."""

    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        ASSIGNED = "ASSIGNED", "Assigned"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        COMMENT = "COMMENT", "Comment"
        POS_ORDER_LINKED = "POS_ORDER_LINKED", "POS draft linked"
        ESCALATED = "ESCALATED", "Escalated"
        CANCELLED = "CANCELLED", "Cancelled"

    request = models.ForeignKey(ServiceRequest, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_request_events",
    )
    actor_label = models.CharField(max_length=160, blank=True, default="")
    message = models.TextField(blank=True, default="")
    guest_visible = models.BooleanField(default=False, db_index=True)
    previous_status = models.CharField(max_length=20, blank=True, default="")
    new_status = models.CharField(max_length=20, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["request", "created_at"]),
            models.Index(fields=["request", "guest_visible", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Service-request events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Service-request events are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.request.reference} · {self.type}"


class LaundryOrder(TimeStampedModel):
    """One guest laundry collection: items, processing, pricing and billing.

    Laundry is tracked here (not as a generic service request) because it has
    priced item lines and a folio charge. Billing posts exactly once through
    the existing Finance folio ledger (Dr guest receivable / Cr other
    operating revenue); this record never moves money by itself.

    Two kinds of customer are served from this one workflow:

    * **In-house** — the order carries a ``stay`` (and its ``guest``) and the
      price is charged to that stay's folio with :func:`bill_laundry_order`.
    * **Walk-in** — a non-resident leaves laundry at reception. There is no
      stay and no folio, so the order carries the customer's own
      ``guest_name``/``guest_phone`` and the money is taken at the desk with
      :func:`settle_walk_in_laundry`, which posts the collection exactly once
      (Dr tender / Cr other operating revenue). Both kinds stay fully
      traceable; neither can move money twice.
    """

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        READY = "READY", "Ready for delivery"
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CARD = "CARD", "Card / terminal"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    # Nullable so a walk-in customer (no booking, no room) can use the service.
    stay = models.ForeignKey(
        "stays.Stay", null=True, blank=True, on_delete=models.PROTECT, related_name="laundry_orders",
    )
    guest = models.ForeignKey(
        "bookings.Guest", null=True, blank=True, on_delete=models.PROTECT, related_name="laundry_orders",
    )
    # Walk-in customer identity. Blank for an in-house order, whose name comes
    # from the linked guest. Scanning a report never shows an unnamed pile of
    # laundry: one of the two identities is always present.
    guest_name = models.CharField(max_length=200, blank=True, default="")
    guest_phone = models.CharField(max_length=40, blank=True, default="")
    # How a walk-in order was actually paid for (blank until it is settled).
    payment_method = models.CharField(max_length=20, blank=True, default="")
    room = models.ForeignKey(
        "rooms.Room", null=True, blank=True, on_delete=models.PROTECT, related_name="laundry_orders",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_laundry_orders",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="received_laundry_orders",
    )
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="NGN")
    billed_at = models.DateTimeField(null=True, blank=True)
    folio = models.ForeignKey(
        "finance.Folio", null=True, blank=True, on_delete=models.PROTECT, related_name="laundry_orders",
    )
    financial_transaction = models.ForeignKey(
        "finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT,
        related_name="laundry_orders",
    )
    ready_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="delivered_laundry_orders",
    )
    delivered_to = models.CharField(max_length=160, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["stay", "status"]),
            models.Index(fields=["guest_name", "status"]),
        ]

    @property
    def is_walk_in(self) -> bool:
        """True when this order belongs to a non-resident walk-in customer."""
        return self.stay_id is None

    @property
    def customer_name(self) -> str:
        """The name to show on a screen or report, for either kind of customer."""
        if self.stay_id is not None and self.guest_id is not None:
            return self.guest.full_name
        return self.guest_name or self.guest_phone or "Walk-in customer"

    @property
    def settled(self) -> bool:
        """True once money has been recorded for this order (folio or payment)."""
        return self.financial_transaction_id is not None

    def __str__(self):
        return f"{self.reference} · {self.get_status_display()}"


class LaundryItem(models.Model):
    """One priced laundry line (e.g. 3 × shirt, wash & iron)."""

    class ServiceType(models.TextChoices):
        WASH = "WASH", "Wash"
        WASH_IRON = "WASH_IRON", "Wash & iron"
        DRY_CLEAN = "DRY_CLEAN", "Dry clean"
        PRESS = "PRESS", "Press / iron only"

    order = models.ForeignKey(LaundryOrder, on_delete=models.PROTECT, related_name="items")
    description = models.CharField(max_length=160)
    quantity = models.PositiveSmallIntegerField(default=1)
    service_type = models.CharField(max_length=16, choices=ServiceType.choices, default=ServiceType.WASH)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["pk"]

    @property
    def line_total(self) -> Decimal:
        return (Decimal(self.quantity) * Decimal(self.unit_price)).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.quantity} × {self.description}"


# ---------------------------------------------------------------------------
# Receptionist guest amenities (basic guest supplies)
#
# The Reception Department holds an allocated stock of basic guest amenities
# (soap, toothbrush, toothpaste, tissue). The Store Keeper allocates that stock
# through the EXISTING inventory ledger (a transfer from Main Store into the
# Reception location). The receptionist then issues amenity units to a guest
# checking in; the issuance deducts from the Reception location's balance (NOT
# directly from Main Store) and is recorded here as an append-only evidence
# row beside the immutable StockMovement that changed the balance.
# ---------------------------------------------------------------------------


class GuestAmenityIssue(TimeStampedModel):
    """Append-only record of one basic guest amenity issued at check-in.

    One row per issued unit-line. It never owns a balance: the balance change is
    the immutable ``StockMovement`` (type ``ISSUE``) created from the Reception
    location, and this row is the auditable human record beside it (who, which
    guest, which room, what, how many, and the departmental balance before and
    after).
    """

    class Amenity(models.TextChoices):
        SOAP = "SOAP", "Soap"
        TOOTHBRUSH = "TOOTHBRUSH", "Toothbrush"
        TOOTHPASTE = "TOOTHPASTE", "Toothpaste"
        TISSUE = "TISSUE", "Tissue"
        OTHER = "OTHER", "Other"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    #: Server-scoped digest of the caller's idempotency key. A retried request
    #: (lost reply, double tap) returns the existing row instead of issuing twice.
    source_key = models.CharField(max_length=180, unique=True)
    guest = models.ForeignKey(
        "bookings.Guest", on_delete=models.PROTECT, related_name="amenity_issues"
    )
    booking = models.ForeignKey(
        "bookings.Booking", null=True, blank=True, on_delete=models.PROTECT,
        related_name="amenity_issues",
    )
    room_number = models.CharField(max_length=40, blank=True, default="")
    receptionist = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="amenity_issues_issued",
    )
    item = models.ForeignKey(
        "inventory.StockItem", on_delete=models.PROTECT, related_name="amenity_issues"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4)
    #: The Reception Department location the stock was issued FROM.
    location = models.ForeignKey(
        "inventory.StockLocation", on_delete=models.PROTECT, related_name="amenity_issues"
    )
    departmental_balance_before = models.DecimalField(max_digits=14, decimal_places=4)
    departmental_balance_after = models.DecimalField(max_digits=14, decimal_places=4)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    notes = models.TextField(blank=True, default="")
    movement = models.OneToOneField(
        "inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT,
        related_name="amenity_issue",
    )

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [
            models.Index(fields=["guest", "occurred_at"]),
            models.Index(fields=["booking", "occurred_at"]),
            models.Index(fields=["location", "item", "occurred_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.item.sku} × {self.quantity} to {self.guest.full_name}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Guest amenity issues are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Guest amenity issues are append-only and cannot be deleted.")


# ---------------------------------------------------------------------------
# Complementary breakfast for lodged guests
#
# Breakfast is a guest-accommodation benefit, NOT a paid restaurant order. An
# issuance is recorded on its own immutable row and never creates a POS order, a
# folio charge or restaurant revenue. Ingredient consumption is manual, so an
# issuance performs no inventory movement either.
# ---------------------------------------------------------------------------


class BreakfastBenefitConfig(TimeStampedModel):
    """Hotel-wide complementary-breakfast configuration (admin/GM owned)."""

    is_enabled = models.BooleanField(default=True, db_index=True)
    #: Service window as HH:MM strings; empty means "all day".
    service_start = models.CharField(max_length=5, blank=True, default="")
    service_end = models.CharField(max_length=5, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="breakfast_config_updates",
    )

    class Meta:
        verbose_name = "Breakfast benefit configuration"

    def __str__(self):
        return "Complementary breakfast benefit"


class BreakfastIssuance(TimeStampedModel):
    """One complementary breakfast served to one guest on one service date.

    Duplicate claims for the same guest and service date are prevented by a
    database constraint, so a repeated request (or a double tap) can never
    record the same breakfast twice.
    """

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    source_key = models.CharField(max_length=180, unique=True)
    guest = models.ForeignKey(
        "bookings.Guest", on_delete=models.PROTECT, related_name="breakfast_issuances"
    )
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="breakfast_issuances"
    )
    service_date = models.DateField(db_index=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="breakfast_issuances_issued",
    )
    #: Number of guests the booking entitles to breakfast.
    eligible_guests = models.PositiveIntegerField(default=1)
    #: What was served, as evidence: [{item_id, item_sku, item_name, quantity, unit}].
    items = models.JSONField(default=list, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-service_date", "-occurred_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["guest", "service_date"], name="one_breakfast_per_guest_service_date"
            )
        ]
        indexes = [
            models.Index(fields=["booking", "service_date"]),
            models.Index(fields=["service_date", "guest"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.guest.full_name} · {self.service_date}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Breakfast issuances are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Breakfast issuances are append-only and cannot be deleted.")
