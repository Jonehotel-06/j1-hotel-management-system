"""Bounded inventory/procurement API representations and command validation."""
from decimal import Decimal, ROUND_HALF_UP

from rest_framework import serializers

from apps.pos.models import MenuItem

from .models import (
    GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine, PurchaseOrderPayment,
    StockBalance, StockConsumptionRequest, StockCount, StockCountLine, StockDepartment, StockItem,
    StockLocation, StockMovement, StockRecipe, StockRecipeLine, Supplier,
)


class EmptyAsNoneRelatedField(serializers.PrimaryKeyRelatedField):
    """An untouched select in an edit form posts "", which must mean "unchanged".

    The dashes edit dialog submits every field it rendered, so an optional
    relation that the user never touched arrives as an empty string rather than
    as a missing key. Treating it as ``None`` keeps the PATCH partial instead of
    answering "Invalid pk \"\" - object does not exist".
    """

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip() == "":
            return None
        return super().to_internal_value(data)


class EmptyAsNoneDecimalField(serializers.DecimalField):
    """An optional decimal posted as "" means "leave the stored value alone"."""

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip() == "":
            return None
        if data is None:
            return None
        return super().to_internal_value(data)


class InventoryMenuItemDirectorySerializer(serializers.Serializer):
    """Minimal active POS-menu projection for inventory-owned recipe setup."""

    id = serializers.IntegerField(read_only=True)
    sku = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    currency = serializers.CharField(read_only=True)


class StockLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockLocation
        fields = ["id", "code", "name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class ProcurementLocationLookupSerializer(serializers.ModelSerializer):
    """Active delivery location, as the purchase-order form needs to pick one."""

    class Meta:
        model = StockLocation
        fields = ["id", "code", "name", "is_active"]
        read_only_fields = fields


class ProcurementItemLookupSerializer(serializers.ModelSerializer):
    """Purchase-side projection of an active stock item for purchase-order lines.

    Deliberately minimal: the unit and pack data the line needs to convert a
    pack quantity into base units, plus PURCHASE-cost references. No POS or
    selling-price field is exposed here — purchase cost and the customer-facing
    price stay separate.
    """

    preferred_supplier_name = serializers.CharField(source="preferred_supplier.name", read_only=True, allow_null=True)

    class Meta:
        model = StockItem
        fields = [
            "id", "sku", "name", "category", "item_type", "base_unit", "units_per_pack", "pack_price",
            "standard_cost", "currency", "preferred_supplier", "preferred_supplier_name", "is_active",
        ]
        read_only_fields = fields


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            "id", "reference", "name", "status", "contact_name", "email", "phone", "address",
            "payment_terms", "tax_reference", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "reference", "created_at", "updated_at"]


class StockItemSerializer(serializers.ModelSerializer):
    preferred_supplier_name = serializers.CharField(source="preferred_supplier.name", read_only=True, allow_null=True)
    # What one pack/bag costs at the per-piece standard cost; display-only.
    pack_cost = serializers.SerializerMethodField()
    # How the item is used (sellable product / recipe ingredient / operational
    # consumable / reusable asset). The label is additive so a screen never has
    # to reproduce the choice list.
    item_type_label = serializers.CharField(source="get_item_type_display", read_only=True)
    # Explicit direct-sale configuration: which POS menu item this stocked
    # product is sold as, how many base units one menu unit consumes, and which
    # store it is drawn from. Empty means "deduct only through a recipe".
    direct_sale_menu_item = EmptyAsNoneRelatedField(
        queryset=MenuItem.objects.all(), required=False, allow_null=True
    )
    direct_sale_menu_item_name = serializers.CharField(
        source="direct_sale_menu_item.name", read_only=True, allow_null=True
    )
    direct_sale_menu_item_sku = serializers.CharField(
        source="direct_sale_menu_item.sku", read_only=True, allow_null=True
    )
    direct_sale_quantity = EmptyAsNoneDecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True
    )
    direct_sale_location = EmptyAsNoneRelatedField(
        queryset=StockLocation.objects.all(), required=False, allow_null=True
    )
    direct_sale_location_code = serializers.CharField(
        source="direct_sale_location.code", read_only=True, allow_null=True
    )
    direct_sale_configured = serializers.BooleanField(source="is_direct_sale_configured", read_only=True)

    class Meta:
        model = StockItem
        fields = [
            "id", "sku", "name", "category", "item_type", "item_type_label", "base_unit", "units_per_pack",
            "pack_price", "pack_cost", "reorder_level", "standard_cost", "currency",
            "preferred_supplier", "preferred_supplier_name", "is_active", "notes",
            "direct_sale_menu_item", "direct_sale_menu_item_name", "direct_sale_menu_item_sku",
            "direct_sale_quantity", "direct_sale_location", "direct_sale_location_code", "direct_sale_configured",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "pack_cost", "item_type_label", "direct_sale_menu_item_name", "direct_sale_menu_item_sku",
            "direct_sale_location_code", "direct_sale_configured", "created_at", "updated_at",
        ]

    def get_pack_cost(self, obj):
        if obj.units_per_pack and obj.standard_cost:
            return str((obj.standard_cost * obj.units_per_pack).quantize(Decimal("0.01")))
        return None

    #: Words that describe a supplier container rather than a countable unit.
    #: Once an item states how many pieces a container holds, the ledger must
    #: count PIECES: recording 12 pieces as "1 PACK" is what made receipts look
    #: like they arrived in packs while the physical count was in pieces.
    PACKAGING_UNITS = {"PACK", "PACKET", "BAG", "CRATE", "CARTON", "CASE", "SACK", "BOX"}

    def validate_base_unit(self, value):
        return str(value or "").strip().upper()

    def validate(self, attrs):
        # Staff may record the actual pack price and the pieces it contains
        # instead of doing the division themselves: when no per-piece cost was
        # entered, the standard cost (always per ONE base unit) is derived
        # from the pack price. An explicit per-piece price always wins, so a
        # corrected piece price is never silently overwritten.
        pack_price = attrs.get("pack_price", getattr(self.instance, "pack_price", None))
        units_per_pack = attrs.get("units_per_pack", getattr(self.instance, "units_per_pack", None))
        standard_cost = attrs.get("standard_cost", getattr(self.instance, "standard_cost", None))
        base_unit = str(attrs.get("base_unit", getattr(self.instance, "base_unit", "")) or "").strip().upper()
        if base_unit in self.PACKAGING_UNITS and units_per_pack:
            raise serializers.ValidationError({
                "base_unit": (
                    "Count this item in PIECE, not in a pack/bag. A pack contains "
                    f"{units_per_pack} pieces, so the stock ledger must count pieces; "
                    "enter the pack size under 'Pieces in one pack/bag' instead."
                )
            })
        if pack_price and units_per_pack and not standard_cost:
            attrs["standard_cost"] = (pack_price / units_per_pack).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self._validate_direct_sale(attrs)
        return attrs

    def _validate_direct_sale(self, attrs):
        """A direct-sale configuration must be complete, and unique per menu item.

        Deducting for a menu item requires either a real recipe or this explicit
        configuration; half a configuration would deduct from the wrong store
        (or not at all), and two stock items claiming the same menu item would
        deduct the same sale twice, so both are refused here.
        """
        instance = self.instance
        menu_item = attrs.get("direct_sale_menu_item", getattr(instance, "direct_sale_menu_item", None))
        location = attrs.get("direct_sale_location", getattr(instance, "direct_sale_location", None))
        quantity = attrs.get("direct_sale_quantity", getattr(instance, "direct_sale_quantity", None))
        if menu_item is None:
            # Clearing the menu item clears the whole configuration. Leaving a
            # store or a multiplier behind would be a half configuration that
            # reads as if something were set up, so both are normalised away in
            # the same save; the deduction path already ignores either one
            # without a menu item, so nothing can be deducted from a leftover.
            attrs["direct_sale_location"] = None
            attrs["direct_sale_quantity"] = Decimal("1.0000")
            return
        if location is None:
            raise serializers.ValidationError({
                "direct_sale_location": "Choose the store this product is drawn from; that store's balance is what the sale deducts."
            })
        if quantity is None or quantity <= Decimal("0.0000"):
            raise serializers.ValidationError({
                "direct_sale_quantity": "Enter how many base units one menu unit deducts (24 for a crate of 24 bottles)."
            })
        clash = (
            StockItem.objects.filter(direct_sale_menu_item=menu_item)
            .exclude(pk=getattr(instance, "pk", None))
            .first()
        )
        if clash is not None:
            raise serializers.ValidationError({
                "direct_sale_menu_item": (
                    f"{clash.sku} · {clash.name} already deducts this menu item. Clear that configuration first: "
                    "one sale must never be deducted by two stock items."
                )
            })


class StockBalanceSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_type = serializers.CharField(source="item.item_type", read_only=True)
    item_type_label = serializers.CharField(source="item.get_item_type_display", read_only=True)
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)
    reorder_level = serializers.DecimalField(source="item.reorder_level", max_digits=14, decimal_places=4, read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    needs_reorder = serializers.SerializerMethodField()
    stock_state = serializers.SerializerMethodField()

    class Meta:
        model = StockBalance
        fields = [
            "id", "item", "item_sku", "item_name", "item_type", "item_type_label", "base_unit", "reorder_level",
            "location", "location_code", "quantity_on_hand", "needs_reorder", "stock_state", "last_movement_at", "updated_at",
        ]
        read_only_fields = fields

    def get_needs_reorder(self, obj):
        return obj.quantity_on_hand <= obj.item.reorder_level

    def get_stock_state(self, obj):
        if obj.quantity_on_hand <= 0:
            return "OUT_OF_STOCK"
        return "LOW_STOCK" if self.get_needs_reorder(obj) else "ADEQUATE"


class StockMovementSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_type = serializers.CharField(source="item.item_type", read_only=True)
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)
    department_label = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "reference", "item_sku", "item_name", "item_type", "base_unit", "location_code", "type",
            "quantity_delta", "unit_cost", "currency", "occurred_at", "source_reference", "notes",
            "department", "department_label", "recipient", "actor_email", "metadata",
        ]
        read_only_fields = fields

    def get_department_label(self, obj):
        if not obj.department:
            return ""
        historical_labels = {
            "BAR": "Bar (consolidated into Waiters/Waitresses)",
            "MAINTENANCE": "Maintenance (consolidated into Housekeepers)",
            "HR": "Human Resources (consolidated into Admin Manager/Procurement)",
        }
        return dict(StockDepartment.choices).get(
            obj.department, historical_labels.get(obj.department, obj.department)
        )


class IssueStockSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    type = serializers.ChoiceField(
        choices=[
            StockMovement.Type.ISSUE,
            StockMovement.Type.MAINTENANCE_ISSUE,
            StockMovement.Type.WRITE_OFF,
        ],
        default=StockMovement.Type.ISSUE,
    )
    # Who is receiving the goods and who takes custody of them. Both optional so
    # every existing caller keeps working, but they are the audit trail of an
    # internal issue and are recorded on the immutable movement itself.
    department = serializers.ChoiceField(
        choices=StockDepartment.choices, required=False, allow_blank=True, default=""
    )
    recipient = serializers.CharField(required=False, allow_blank=True, max_length=120)
    source_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    #: Why the stock is leaving the store. Recorded on the movement's notes.
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class OpeningStockSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False, allow_null=True)
    unit = serializers.CharField(required=False, allow_blank=True, max_length=30)
    conversion_factor = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class ReturnStockSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False, allow_null=True)
    direction = serializers.ChoiceField(choices=["IN", "OUT"], required=False, default="IN")
    department = serializers.ChoiceField(
        choices=StockDepartment.choices, required=False, allow_blank=True, default=""
    )
    recipient = serializers.CharField(required=False, allow_blank=True, max_length=120)
    #: Cite the issue (STM reference or its source reference) when unused stock or
    #: a reusable asset comes back; the ledger then enforces the issue ceiling.
    source_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class TransferStockSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    from_location_id = serializers.IntegerField(min_value=1)
    to_location_id = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    # ``base_unit`` is the product's own stock unit and ``quantity_outstanding``
    # is derived with Decimal arithmetic on the server. Both are additive fields:
    # they let the UI render "4 BOTTLE" and an exact outstanding quantity instead
    # of dividing floats in the browser. No stored value is changed.
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)
    units_per_pack = serializers.DecimalField(source="item.units_per_pack", max_digits=10, decimal_places=2, read_only=True, allow_null=True)
    quantity_outstanding = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "id", "item", "item_sku", "item_name", "base_unit", "units_per_pack", "quantity_ordered", "quantity_received",
            "quantity_outstanding", "unit_cost", "notes",
        ]
        read_only_fields = fields

    def get_quantity_outstanding(self, obj):
        outstanding = (obj.quantity_ordered or Decimal("0.0000")) - (obj.quantity_received or Decimal("0.0000"))
        return str(outstanding if outstanding > 0 else Decimal("0.0000"))


class PurchaseOrderPaymentSerializer(serializers.ModelSerializer):
    """One recorded money-out to a supplier, with its evidence."""

    evidence_url = serializers.SerializerMethodField()
    uploaded_by_email = serializers.EmailField(source="evidence_uploaded_by.email", read_only=True, allow_null=True)

    class Meta:
        model = PurchaseOrderPayment
        fields = [
            "id", "amount", "method", "reference", "paid_on",
            "evidence_url", "evidence_uploaded_at", "uploaded_by_email",
            "notes", "created_at",
        ]
        read_only_fields = fields

    def get_evidence_url(self, obj):
        from apps.core.storage import absolute_media_url

        return absolute_media_url(obj.evidence_file, self.context.get("request"))


class PurchaseOrderPaymentInputSerializer(serializers.Serializer):
    """Record a supplier payment against an approved order.

    ``amount`` defaults to the outstanding balance server-side, so a
    full settlement is a one-field submission.
    """

    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False,
    )
    method = serializers.ChoiceField(choices=PurchaseOrderPayment.PaymentMethod.choices)
    reference = serializers.CharField(required=False, allow_blank=True, max_length=160)
    paid_on = serializers.DateField(required=False)
    evidence_file = serializers.FileField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class PurchaseOrderListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    delivery_location_code = serializers.CharField(source="delivery_location.code", read_only=True)
    requested_by_email = serializers.EmailField(source="requested_by.email", read_only=True)
    approved_by_email = serializers.EmailField(source="approved_by.email", read_only=True, allow_null=True)
    paid_by_email = serializers.EmailField(source="paid_by.email", read_only=True, allow_null=True)
    approval_receipt_url = serializers.SerializerMethodField()
    line_count = serializers.IntegerField(read_only=True)
    # Money fields come from the related managers (the views prefetch
    # them) so lists and detail agree on the order's arithmetic.
    total_amount = serializers.SerializerMethodField()
    total_paid = serializers.SerializerMethodField()
    outstanding_amount = serializers.SerializerMethodField()
    # ``status`` is the one stored workflow state. These two are DERIVED,
    # read-only projections of the lines and the recorded payments, so the
    # independent questions "has the stock arrived?" and "has the supplier been
    # paid?" stay answerable when status reads PAID (goods may still be due) or
    # RECEIVED (money may still be owed). Never stored, never used for control.
    receipt_progress = serializers.SerializerMethodField()
    payment_progress = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            "reference", "supplier", "supplier_name", "delivery_location", "delivery_location_code",
            "status", "currency", "supplier_reference",
            "requested_by_email", "approved_by_email", "approved_at", "ordered_at", "expected_on", "line_count",
            "payment_reference", "approval_receipt_url", "approval_receipt_uploaded_at",
            "total_amount", "total_paid", "outstanding_amount", "receipt_progress", "payment_progress",
            "paid_at", "paid_by", "paid_by_email",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_approval_receipt_url(self, obj):
        from apps.core.storage import absolute_media_url

        return absolute_media_url(obj.approval_receipt, self.context.get("request"))

    def get_total_amount(self, obj):
        return str(obj.total_amount)

    def get_total_paid(self, obj):
        return str(obj.total_paid)

    def get_outstanding_amount(self, obj):
        return str(obj.outstanding_amount)

    def get_receipt_progress(self, obj):
        lines = list(obj.lines.all())
        received = [Decimal(line.quantity_received or 0) for line in lines]
        if not lines or not any(value > 0 for value in received):
            return "NOT_RECEIVED"
        if all(Decimal(line.quantity_received or 0) >= Decimal(line.quantity_ordered or 0) for line in lines):
            return "RECEIVED"
        return "PARTIALLY_RECEIVED"

    def get_payment_progress(self, obj):
        total, paid = obj.total_amount, obj.total_paid
        if total <= 0:
            return "NOTHING_DUE"
        if paid <= 0:
            return "UNPAID"
        return "PAID" if paid >= total else "PARTIALLY_PAID"


class PurchaseOrderDetailSerializer(PurchaseOrderListSerializer):
    lines = PurchaseOrderLineSerializer(many=True, read_only=True)
    payments = PurchaseOrderPaymentSerializer(many=True, read_only=True)
    notes = serializers.CharField(read_only=True)

    class Meta(PurchaseOrderListSerializer.Meta):
        fields = PurchaseOrderListSerializer.Meta.fields + ["notes", "lines", "payments"]


class PurchaseOrderDecisionSerializer(serializers.Serializer):
    """Reject (return to draft) or cancel: the reason is mandatory evidence."""

    reason = serializers.CharField(max_length=1000, trim_whitespace=True)


class PurchaseOrderGoodsReceiptSummarySerializer(serializers.ModelSerializer):
    """One goods receipt against an order, valued at its own received unit costs."""

    received_value = serializers.SerializerMethodField()
    received_by_email = serializers.EmailField(source="received_by.email", read_only=True, allow_null=True)

    class Meta:
        model = GoodsReceipt
        fields = ["reference", "received_on", "supplier_delivery_reference", "received_by_email", "received_value", "created_at"]
        read_only_fields = fields

    def get_received_value(self, obj):
        total = sum((Decimal(line.quantity_received) * Decimal(line.unit_cost) for line in obj.lines.all()), Decimal("0.00"))
        return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PurchaseOrderAccountingSerializer(PurchaseOrderDetailSerializer):
    """Read-only hand-off view of a submitted order for Accounting.

    Everything an accountant needs to verify the order before raising the
    supplier payable through the existing Expenses workflow: the approved
    lines and costs, what has physically arrived (and its value), what the
    procurement desk has already recorded as paid, and the evidence files.
    """

    goods_receipts = PurchaseOrderGoodsReceiptSummarySerializer(many=True, read_only=True)
    received_value = serializers.SerializerMethodField()

    class Meta(PurchaseOrderDetailSerializer.Meta):
        fields = PurchaseOrderDetailSerializer.Meta.fields + ["goods_receipts", "received_value"]

    def get_received_value(self, obj):
        total = sum(
            (Decimal(line.quantity_received) * Decimal(line.unit_cost)
             for receipt in obj.goods_receipts.all() for line in receipt.lines.all()),
            Decimal("0.00"),
        )
        return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PurchaseOrderCreateLineSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    quantity_ordered = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)


class PurchaseOrderCreateSerializer(serializers.Serializer):
    supplier_id = serializers.IntegerField(min_value=1)
    delivery_location_id = serializers.IntegerField(min_value=1)
    expected_on = serializers.DateField(required=False, allow_null=True)
    supplier_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)
    lines = PurchaseOrderCreateLineSerializer(many=True, min_length=1, max_length=200)


class PurchaseOrderUpdateSerializer(serializers.Serializer):
    """Partial edit of an unapproved order.

    Every field is optional; supplying ``lines`` replaces the whole line set
    (the pre-approval UI always submits the complete table, so a partial line
    merge would be ambiguous). The service enforces the APPROVED boundary.
    """

    supplier_id = serializers.IntegerField(min_value=1, required=False)
    delivery_location_id = serializers.IntegerField(min_value=1, required=False)
    expected_on = serializers.DateField(required=False, allow_null=True)
    supplier_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    lines = PurchaseOrderCreateLineSerializer(many=True, min_length=1, max_length=200, required=False)


class GoodsReceiptInputLineSerializer(serializers.Serializer):
    purchase_order_line_id = serializers.IntegerField(min_value=1)
    quantity_received = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)


class GoodsReceiptCreateSerializer(serializers.Serializer):
    idempotency_key = serializers.CharField(max_length=128)
    received_on = serializers.DateField(required=False)
    supplier_delivery_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    lines = GoodsReceiptInputLineSerializer(many=True, min_length=1, max_length=200)


class GoodsReceiptLineSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)
    currency = serializers.CharField(source="goods_receipt.purchase_order.currency", read_only=True, allow_null=True)
    movement_reference = serializers.CharField(source="stock_movement.reference", read_only=True)
    class Meta:
        model = GoodsReceiptLine
        fields = [
            "id", "item_sku", "item_name", "base_unit", "currency", "quantity_received", "unit_cost", "movement_reference",
        ]
        read_only_fields = fields


class GoodsReceiptSerializer(serializers.ModelSerializer):
    lines = GoodsReceiptLineSerializer(many=True, read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, allow_null=True)
    class Meta:
        model = GoodsReceipt
        fields = [
            "reference", "purchase_order", "location_code", "supplier_name", "received_on",
            "supplier_delivery_reference", "notes", "lines", "created_at",
        ]
        read_only_fields = fields


class StockConsumptionRequestSerializer(serializers.ModelSerializer):
    order_reference = serializers.CharField(source="pos_order.reference", read_only=True)
    created_by_email = serializers.EmailField(source="pos_order.created_by.email", read_only=True, allow_null=True)
    processed_by_email = serializers.EmailField(source="processed_by.email", read_only=True, allow_null=True)

    class Meta:
        model = StockConsumptionRequest
        fields = ["reference", "order_reference", "status", "payload", "requested_at", "processed_at", "created_by_email", "processed_by_email", "resolution_note", "resolution_payload"]
        read_only_fields = fields


class StockCountLineSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)
    adjustment_movement_reference = serializers.CharField(source="adjustment_movement.reference", read_only=True, allow_null=True)

    class Meta:
        model = StockCountLine
        fields = ["id", "item", "item_sku", "item_name", "base_unit", "expected_quantity", "counted_quantity", "variance", "adjustment_movement_reference"]
        read_only_fields = fields


class StockCountSerializer(serializers.ModelSerializer):
    location_code = serializers.CharField(source="location.code", read_only=True)
    initiated_by_email = serializers.EmailField(source="initiated_by.email", read_only=True)
    submitted_by_email = serializers.EmailField(source="submitted_by.email", read_only=True, allow_null=True)
    approved_by_email = serializers.EmailField(source="approved_by.email", read_only=True, allow_null=True)
    lines = StockCountLineSerializer(many=True, read_only=True)

    class Meta:
        model = StockCount
        fields = [
            "reference", "location", "location_code", "status", "initiated_by_email", "submitted_by_email", "submitted_at",
            "approved_by_email", "approved_at", "notes", "created_at", "updated_at", "lines",
        ]
        read_only_fields = fields


class StockCountCreateSerializer(serializers.Serializer):
    location_id = serializers.IntegerField(min_value=1)
    item_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=500)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class StockCountSubmitLineSerializer(serializers.Serializer):
    line_id = serializers.IntegerField(min_value=1)
    counted_quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0"))


class StockCountSubmitSerializer(serializers.Serializer):
    lines = StockCountSubmitLineSerializer(many=True, min_length=1, max_length=500)


class StockRecipeLineSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)

    class Meta:
        model = StockRecipeLine
        fields = [
            "id", "item", "item_sku", "item_name", "base_unit", "quantity_per_menu_unit",
            "entered_quantity", "entered_unit",
        ]
        read_only_fields = fields


class StockRecipeSerializer(serializers.ModelSerializer):
    menu_item_sku = serializers.CharField(source="menu_item.sku", read_only=True)
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    retired_by_email = serializers.EmailField(source="retired_by.email", read_only=True, allow_null=True)
    lines = StockRecipeLineSerializer(many=True, read_only=True)

    class Meta:
        model = StockRecipe
        fields = [
            "reference", "menu_item", "menu_item_sku", "menu_item_name", "location", "location_code", "version",
            "tracking_mode", "is_active", "notes", "created_by_email", "retired_at", "retired_by_email", "created_at", "lines",
        ]
        read_only_fields = fields


class StockRecipeComponentInputSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    quantity_per_menu_unit = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit = serializers.CharField(required=False, allow_blank=True, max_length=30)
    conversion_factor = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False, allow_null=True
    )


class StockRecipeCreateSerializer(serializers.Serializer):
    menu_item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    tracking_mode = serializers.ChoiceField(choices=StockRecipe.TrackingMode.choices, default=StockRecipe.TrackingMode.COMPONENTS)
    components = StockRecipeComponentInputSerializer(many=True, required=False, max_length=100)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    idempotency_key = serializers.CharField(max_length=128)


class ProductProfitabilityRowSerializer(serializers.Serializer):
    """Read-only projection row.

    Every field is read-only and several are nullable: a null here means "not
    derivable from stored data" (no cost basis or no active recipe) and must be
    rendered as an em dash rather than as a zero.
    """

    projection = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(choices=["OK", "NOT_SELLABLE", "NO_COST_BASIS"], read_only=True)
    item_id = serializers.IntegerField(read_only=True)
    sku = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    category = serializers.CharField(read_only=True)
    department = serializers.CharField(read_only=True)
    base_unit = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    location_id = serializers.IntegerField(read_only=True)
    location_code = serializers.CharField(read_only=True)
    location_name = serializers.CharField(read_only=True)
    supplier_id = serializers.IntegerField(read_only=True, allow_null=True)
    supplier_name = serializers.CharField(read_only=True, allow_null=True)
    purchase_unit_cost = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True, allow_null=True)
    cost_basis = serializers.CharField(read_only=True)
    standard_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True)
    latest_receipt_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True)
    selling_price = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True, allow_null=True)
    price_basis = serializers.CharField(read_only=True)
    recipe_count = serializers.IntegerField(read_only=True)
    current_stock = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True)
    reorder_level = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True)
    needs_reorder = serializers.BooleanField(read_only=True)
    stock_value = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True, allow_null=True)
    expected_sales_revenue = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True, allow_null=True)
    expected_gross_profit = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True, allow_null=True)
    expected_gross_margin_percent = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, allow_null=True)
    last_movement_at = serializers.DateTimeField(read_only=True, allow_null=True)


class ProductProfitabilitySummarySerializer(serializers.Serializer):
    projection = serializers.CharField(read_only=True)
    row_count = serializers.IntegerField(read_only=True)
    valued_row_count = serializers.IntegerField(read_only=True)
    sellable_row_count = serializers.IntegerField(read_only=True)
    unpriced_row_count = serializers.IntegerField(read_only=True)
    total_stock_value = serializers.DecimalField(max_digits=24, decimal_places=2, read_only=True)
    total_expected_sales_revenue = serializers.DecimalField(max_digits=24, decimal_places=2, read_only=True)
    total_expected_gross_profit = serializers.DecimalField(max_digits=24, decimal_places=2, read_only=True)
    overall_expected_gross_margin_percent = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, allow_null=True)
