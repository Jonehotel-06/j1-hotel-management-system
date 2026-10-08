"""Bounded inventory/procurement API representations and command validation."""
from decimal import Decimal

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

    class Meta:
        model = StockItem
        fields = [
            "id", "sku", "name", "category", "base_unit", "reorder_level", "standard_cost", "currency",
            "preferred_supplier", "preferred_supplier_name", "is_active", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


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
    type = serializers.ChoiceField(choices=[StockMovement.Type.ISSUE, StockMovement.Type.MAINTENANCE_ISSUE], default=StockMovement.Type.ISSUE)
    source_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseOrderLine
        fields = ["id", "item", "item_sku", "item_name", "quantity_ordered", "quantity_received", "unit_cost", "notes"]
        read_only_fields = fields


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
    movement_reference = serializers.CharField(source="stock_movement.reference", read_only=True)
    class Meta:
        model = GoodsReceiptLine
        fields = ["id", "item_sku", "quantity_received", "unit_cost", "movement_reference"]
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
    adjustment_movement_reference = serializers.CharField(source="adjustment_movement.reference", read_only=True, allow_null=True)

    class Meta:
        model = StockCountLine
        fields = ["id", "item", "item_sku", "item_name", "expected_quantity", "counted_quantity", "variance", "adjustment_movement_reference"]
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

    class Meta:
        model = StockRecipeLine
        fields = ["id", "item", "item_sku", "item_name", "quantity_per_menu_unit"]
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


class StockRecipeCreateSerializer(serializers.Serializer):
    menu_item_id = serializers.IntegerField(min_value=1)
    location_id = serializers.IntegerField(min_value=1)
    tracking_mode = serializers.ChoiceField(choices=StockRecipe.TrackingMode.choices, default=StockRecipe.TrackingMode.COMPONENTS)
    components = StockRecipeComponentInputSerializer(many=True, required=False, max_length=100)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    idempotency_key = serializers.CharField(max_length=128)
