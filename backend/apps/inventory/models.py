"""Authoritative stock ledger, balance projection, suppliers, and procurement records."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.utils import hotel_today

ZERO = Decimal("0.00")
POSITIVE = MinValueValidator(Decimal("0.0001"))


class StockLocation(TimeStampedModel):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} · {self.name}"


class Supplier(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    contact_name = models.CharField(max_length=160, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    address = models.TextField(blank=True, default="")
    payment_terms = models.CharField(max_length=160, blank=True, default="")
    tax_reference = models.CharField(max_length=120, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class StockItem(TimeStampedModel):
    sku = models.CharField(max_length=80, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=120, blank=True, default="", db_index=True)
    base_unit = models.CharField(max_length=30, default="UNIT")
    reorder_level = models.DecimalField(max_digits=14, decimal_places=4, default=ZERO, validators=[MinValueValidator(ZERO)])
    standard_cost = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    currency = models.CharField(max_length=3, default="NGN")
    preferred_supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, related_name="preferred_items")
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["category", "name"]
        indexes = [models.Index(fields=["is_active", "category"])]

    def __str__(self):
        return f"{self.sku} · {self.name}"


class StockBalance(TimeStampedModel):
    """Locked projection; immutable movements remain the source of truth."""
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="balances")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="balances")
    quantity_on_hand = models.DecimalField(max_digits=14, decimal_places=4, default=ZERO)
    last_movement_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["item", "location"], name="one_stock_balance_per_item_location")]
        indexes = [models.Index(fields=["location", "quantity_on_hand"])]

    def __str__(self):
        return f"{self.item.sku} @ {self.location.code}: {self.quantity_on_hand}"


class StockMovement(models.Model):
    """Append-only signed quantity movement. No mutable source-of-truth quantity exists."""
    class Type(models.TextChoices):
        OPENING = "OPENING", "Opening balance"
        RECEIPT = "RECEIPT", "Goods receipt"
        ISSUE = "ISSUE", "Operational issue"
        POS_CONSUMPTION = "POS_CONSUMPTION", "POS consumption"
        MAINTENANCE_ISSUE = "MAINTENANCE_ISSUE", "Maintenance part issue"
        MAINTENANCE_RETURN = "MAINTENANCE_RETURN", "Maintenance part return"
        TRANSFER_OUT = "TRANSFER_OUT", "Transfer out"
        TRANSFER_IN = "TRANSFER_IN", "Transfer in"
        ADJUSTMENT = "ADJUSTMENT", "Approved stock adjustment"
        WRITE_OFF = "WRITE_OFF", "Write-off"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    source_key = models.CharField(max_length=180, unique=True)
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="movements")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="movements")
    balance = models.ForeignKey(StockBalance, on_delete=models.PROTECT, related_name="movements")
    type = models.CharField(max_length=24, choices=Type.choices, db_index=True)
    quantity_delta = models.DecimalField(max_digits=14, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    currency = models.CharField(max_length=3, default="NGN")
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    source_reference = models.CharField(max_length=120, blank=True, default="", db_index=True)
    notes = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_movements")

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [
            models.Index(fields=["item", "location", "occurred_at"]),
            models.Index(fields=["type", "occurred_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Stock movements are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stock movements are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.reference} · {self.item.sku} {self.quantity_delta}"


class PurchaseOrder(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        ORDERED = "ORDERED", "Ordered"
        PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially received"
        RECEIVED = "RECEIVED", "Received"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    delivery_location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT, db_index=True)
    currency = models.CharField(max_length=3, default="NGN")
    supplier_reference = models.CharField(max_length=120, blank=True, default="")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="purchase_orders_requested")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_orders_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    expected_on = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["status", "expected_on"]), models.Index(fields=["supplier", "status"])]

    def __str__(self):
        return f"{self.reference} · {self.supplier.name}"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="purchase_order_lines")
    item_sku = models.CharField(max_length=80)
    item_name = models.CharField(max_length=255)
    quantity_ordered = models.DecimalField(max_digits=14, decimal_places=4, validators=[POSITIVE])
    quantity_received = models.DecimalField(max_digits=14, decimal_places=4, default=ZERO, validators=[MinValueValidator(ZERO)])
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)])
    notes = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["purchase_order", "item"], name="one_item_per_purchase_order")]

    def __str__(self):
        return f"{self.purchase_order.reference} · {self.item_sku}"


class GoodsReceipt(TimeStampedModel):
    reference = models.CharField(max_length=64, unique=True, db_index=True)
    source_key = models.CharField(max_length=180, unique=True)
    purchase_order = models.ForeignKey(PurchaseOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="goods_receipts")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="goods_receipts")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT, related_name="goods_receipts")
    received_on = models.DateField(default=hotel_today, db_index=True)
    supplier_delivery_reference = models.CharField(max_length=120, blank=True, default="")
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="goods_receipts_received")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-received_on", "-created_at"]
        indexes = [models.Index(fields=["purchase_order", "received_on"])]

    def __str__(self):
        return self.reference


class GoodsReceiptLine(models.Model):
    goods_receipt = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name="lines")
    purchase_order_line = models.ForeignKey(PurchaseOrderLine, null=True, blank=True, on_delete=models.PROTECT, related_name="receipt_lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="goods_receipt_lines")
    quantity_received = models.DecimalField(max_digits=14, decimal_places=4, validators=[POSITIVE])
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)])
    stock_movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="goods_receipt_line")

    class Meta:
        ordering = ["pk"]


class StockRecipe(TimeStampedModel):
    """Versioned BOM mapping from a POS menu item to inventory components.

    A new recipe is created instead of editing a prior version so a delivered
    order can retain the recipe snapshot that was in force at hand-off.
    """
    class TrackingMode(models.TextChoices):
        COMPONENTS = "COMPONENTS", "Consume component stock"
        NO_STOCK = "NO_STOCK", "Explicitly non-stock tracked"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=180, null=True, blank=True, unique=True)
    menu_item = models.ForeignKey("pos.MenuItem", on_delete=models.PROTECT, related_name="stock_recipes")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="stock_recipes")
    version = models.PositiveIntegerField()
    tracking_mode = models.CharField(max_length=16, choices=TrackingMode.choices, default=TrackingMode.COMPONENTS)
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_recipes_created")
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_recipes_retired")

    class Meta:
        ordering = ["menu_item_id", "-version"]
        constraints = [models.UniqueConstraint(fields=["menu_item", "version"], name="one_recipe_version_per_menu_item")]
        indexes = [models.Index(fields=["menu_item", "is_active", "-version"])]

    def __str__(self):
        return f"{self.menu_item} · recipe v{self.version}"


class StockRecipeLine(models.Model):
    recipe = models.ForeignKey(StockRecipe, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="recipe_lines")
    quantity_per_menu_unit = models.DecimalField(max_digits=14, decimal_places=4, validators=[POSITIVE])

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["recipe", "item"], name="one_stock_item_per_recipe")]


class StockConsumptionRequest(TimeStampedModel):
    """Durable hand-off from delivered POS to inventory-owned recipe depletion.

    POS never edits a stock balance directly. Inventory can later resolve the
    snapshotted lines against recipes and create its own immutable movements.
    """
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending recipe consumption"
        PROCESSED = "PROCESSED", "Stock consumption posted"
        SKIPPED = "SKIPPED", "No controlled recipe"
        FAILED = "FAILED", "Needs inventory review"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    source_key = models.CharField(max_length=180, unique=True)
    pos_order = models.OneToOneField("pos.PosOrder", on_delete=models.PROTECT, related_name="stock_consumption_request")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    payload = models.JSONField(default=dict)
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stock_consumption_requests_processed",
    )
    resolution_note = models.TextField(blank=True, default="")
    resolution_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at", "-pk"]
        indexes = [models.Index(fields=["status", "requested_at"])]

    def __str__(self):
        return f"{self.reference} · {self.pos_order.reference} · {self.status}"


class StockConsumptionRequestEvent(models.Model):
    """Append-only audit evidence for inventory's handling of a POS hand-off."""
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        PROCESSED = "PROCESSED", "Consumption posted"
        SKIPPED = "SKIPPED", "Explicitly non-stock"
        FAILED = "FAILED", "Recipe processing failed"

    request = models.ForeignKey(StockConsumptionRequest, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_consumption_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["request", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Stock-consumption events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stock-consumption events are append-only and cannot be deleted.")


class StockCount(TimeStampedModel):
    """A counted inventory snapshot; variance never mutates stock without approval."""
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending variance approval"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="stock_counts")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="stock_counts_initiated")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_counts_submitted")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_counts_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["location", "status"])]

    def __str__(self):
        return self.reference


class StockCountLine(models.Model):
    stock_count = models.ForeignKey(StockCount, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="stock_count_lines")
    expected_quantity = models.DecimalField(max_digits=14, decimal_places=4)
    counted_quantity = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    variance = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    adjustment_movement = models.OneToOneField(StockMovement, null=True, blank=True, on_delete=models.PROTECT, related_name="stock_count_line")

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["stock_count", "item"], name="one_item_per_stock_count")]


class StockCountEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    stock_count = models.ForeignKey(StockCount, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_count_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["stock_count", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Stock-count events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stock-count events are append-only and cannot be deleted.")
