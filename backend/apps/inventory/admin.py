"""Django-admin fallback for inventory, procurement, and the stock ledger.

The staff dashboard is the primary interface; this registration exists so an
administrator can inspect and correct reference data without shell access.
The ledger itself (movements, receipt lines, count events) is append-only, so
those models are registered read-only: editing them in the admin would either
be rejected by the model or, worse, rewrite stock history.
"""
from django.contrib import admin

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    StockBalance,
    StockConsumptionRequest,
    StockConsumptionRequestEvent,
    StockCount,
    StockCountEvent,
    StockCountLine,
    StockItem,
    StockLocation,
    StockMovement,
    StockRecipe,
    StockRecipeLine,
    Supplier,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspect-only registration for append-only/derived records."""

    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockLocation)
class StockLocationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "reference", "status", "phone", "email")
    list_filter = ("status",)
    search_fields = ("name", "reference", "email", "phone", "tax_reference")


class StockRecipeLineInline(admin.TabularInline):
    model = StockRecipeLine
    extra = 0
    autocomplete_fields = ()


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = (
        "sku", "name", "category", "item_type", "base_unit",
        "units_per_pack", "standard_cost", "reorder_level", "is_active",
    )
    list_filter = ("item_type", "is_active", "category")
    search_fields = ("sku", "name", "category")
    autocomplete_fields = ()


@admin.register(StockBalance)
class StockBalanceAdmin(ReadOnlyAdmin):
    list_display = ("item", "location", "quantity_on_hand", "last_movement_at")
    list_filter = ("location",)
    search_fields = ("item__sku", "item__name")


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyAdmin):
    list_display = (
        "reference", "item", "location", "type", "quantity_delta",
        "unit_cost", "department", "recipient", "occurred_at",
    )
    list_filter = ("type", "location", "department")
    search_fields = ("reference", "source_reference", "item__sku", "item__name")
    date_hierarchy = "occurred_at"


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0
    readonly_fields = ("item_sku", "item_name")


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "supplier", "delivery_location", "status",
        "approved_by", "expected_on", "created_at",
    )
    list_filter = ("status", "delivery_location")
    search_fields = ("reference", "supplier__name", "supplier_reference")
    date_hierarchy = "created_at"
    inlines = [PurchaseOrderLineInline]
    readonly_fields = ("approved_at", "ordered_at", "approval_receipt_uploaded_at")


class GoodsReceiptLineInline(admin.TabularInline):
    model = GoodsReceiptLine
    extra = 0
    readonly_fields = ("item", "purchase_order_line", "quantity_received", "unit_cost", "stock_movement")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(ReadOnlyAdmin):
    list_display = ("reference", "purchase_order", "supplier", "location", "received_on", "received_by")
    list_filter = ("location",)
    search_fields = ("reference", "supplier_delivery_reference", "purchase_order__reference")
    date_hierarchy = "received_on"
    inlines = [GoodsReceiptLineInline]


@admin.register(StockRecipe)
class StockRecipeAdmin(admin.ModelAdmin):
    list_display = ("menu_item", "version", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("menu_item__name", "menu_item__sku")
    inlines = [StockRecipeLineInline]


@admin.register(StockConsumptionRequest)
class StockConsumptionRequestAdmin(ReadOnlyAdmin):
    list_display = ("reference", "pos_order", "status", "requested_at", "processed_at")
    list_filter = ("status",)


@admin.register(StockConsumptionRequestEvent)
class StockConsumptionRequestEventAdmin(ReadOnlyAdmin):
    list_display = ("request", "type", "created_at")
    list_filter = ("type",)


class StockCountLineInline(admin.TabularInline):
    model = StockCountLine
    extra = 0
    readonly_fields = ("item", "expected_quantity", "counted_quantity", "variance", "adjustment_movement")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StockCount)
class StockCountAdmin(ReadOnlyAdmin):
    list_display = ("reference", "location", "status", "initiated_by", "submitted_at", "approved_at")
    list_filter = ("status", "location")
    inlines = [StockCountLineInline]


@admin.register(StockCountEvent)
class StockCountEventAdmin(ReadOnlyAdmin):
    list_display = ("stock_count", "type", "actor", "created_at")
    list_filter = ("type",)
