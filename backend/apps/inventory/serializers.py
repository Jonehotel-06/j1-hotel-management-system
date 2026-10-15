"""Bounded inventory/procurement API representations and command validation."""
from decimal import Decimal, ROUND_HALF_UP

from rest_framework import serializers

from .models import (
    GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine, StockBalance,
    StockConsumptionRequest, StockCount, StockCountLine, StockItem, StockLocation, StockMovement,
    StockRecipe, StockRecipeLine, Supplier,
)


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

    class Meta:
        model = StockItem
        fields = [
            "id", "sku", "name", "category", "base_unit", "units_per_pack", "pack_price", "pack_cost",
            "reorder_level", "standard_cost", "currency",
            "preferred_supplier", "preferred_supplier_name", "is_active", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "pack_cost", "created_at", "updated_at"]

    def get_pack_cost(self, obj):
        if obj.units_per_pack and obj.standard_cost:
            return str((obj.standard_cost * obj.units_per_pack).quantize(Decimal("0.01")))
        return None

    def validate(self, attrs):
        # Staff may record the actual pack price and the pieces it contains
        # instead of doing the division themselves: when no per-piece cost was
        # entered, the standard cost (always per ONE base unit) is derived
        # from the pack price. An explicit per-piece price always wins, so a
        # corrected piece price is never silently overwritten.
        pack_price = attrs.get("pack_price", getattr(self.instance, "pack_price", None))
        units_per_pack = attrs.get("units_per_pack", getattr(self.instance, "units_per_pack", None))
        standard_cost = attrs.get("standard_cost", getattr(self.instance, "standard_cost", None))
        if pack_price and units_per_pack and not standard_cost:
            attrs["standard_cost"] = (pack_price / units_per_pack).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return attrs


class StockBalanceSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    base_unit = serializers.CharField(source="item.base_unit", read_only=True)
    reorder_level = serializers.DecimalField(source="item.reorder_level", max_digits=14, decimal_places=4, read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    needs_reorder = serializers.SerializerMethodField()

    class Meta:
        model = StockBalance
        fields = [
            "id", "item", "item_sku", "item_name", "base_unit", "reorder_level", "location", "location_code",
            "quantity_on_hand", "needs_reorder", "last_movement_at", "updated_at",
        ]
        read_only_fields = fields

    def get_needs_reorder(self, obj):
        return obj.quantity_on_hand <= obj.item.reorder_level


class StockMovementSerializer(serializers.ModelSerializer):
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = StockMovement
        fields = [
            "reference", "item_sku", "item_name", "location_code", "type", "quantity_delta", "unit_cost",
            "currency", "occurred_at", "source_reference", "notes", "actor_email", "metadata",
        ]
        read_only_fields = fields


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
    source_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class OpeningStockSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class ReturnStockSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0.0001"))
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), required=False, allow_null=True)
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


class PurchaseOrderListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    delivery_location_code = serializers.CharField(source="delivery_location.code", read_only=True)
    requested_by_email = serializers.EmailField(source="requested_by.email", read_only=True)
    approved_by_email = serializers.EmailField(source="approved_by.email", read_only=True, allow_null=True)
    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "reference", "supplier_name", "delivery_location_code", "status", "currency", "supplier_reference",
            "requested_by_email", "approved_by_email", "approved_at", "ordered_at", "expected_on", "line_count",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class PurchaseOrderDetailSerializer(PurchaseOrderListSerializer):
    lines = PurchaseOrderLineSerializer(many=True, read_only=True)
    notes = serializers.CharField(read_only=True)

    class Meta(PurchaseOrderListSerializer.Meta):
        fields = PurchaseOrderListSerializer.Meta.fields + ["notes", "lines"]


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
        fields = ["id", "item", "item_sku", "item_name", "base_unit", "quantity_per_menu_unit"]
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
