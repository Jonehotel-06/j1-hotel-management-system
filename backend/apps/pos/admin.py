"""Django-admin fallback for the POS.

Orders, lines, tenders and kitchen tickets are workflow records; the event
models are append-only audit rows and stay read-only here.
"""
from django.contrib import admin

from .models import (
    KitchenTicket,
    MenuCategory,
    MenuItem,
    MenuModifier,
    PosOrder,
    PosOrderEvent,
    PosOrderLine,
    PosTender,
    RestaurantTable,
    RestaurantTableSession,
    RestaurantTableSessionEvent,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class MenuModifierInline(admin.TabularInline):
    model = MenuModifier
    extra = 0


@admin.register(MenuCategory)
class MenuCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "service_area", "sort_order", "is_active")
    list_filter = ("service_area", "is_active")
    search_fields = ("name", "slug")


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ("sku", "name", "category", "base_price", "is_available", "is_active", "sort_order")
    list_filter = ("category", "is_available", "is_active")
    search_fields = ("sku", "name", "barcode")
    inlines = [MenuModifierInline]


@admin.register(RestaurantTable)
class RestaurantTableAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "section", "seats", "is_active")
    list_filter = ("section", "is_active")
    search_fields = ("code", "name")


@admin.register(RestaurantTableSession)
class RestaurantTableSessionAdmin(admin.ModelAdmin):
    list_display = ("reference", "table", "status", "covers", "opened_at", "closed_at")
    list_filter = ("status", "table")
    search_fields = ("reference", "table__code")


@admin.register(PosOrder)
class PosOrderAdmin(admin.ModelAdmin):
    list_display = ("reference", "mode", "status", "settlement_status", "total_amount", "currency", "guest_name", "created_by", "created_at")
    list_filter = ("mode", "status", "settlement_status")
    search_fields = ("reference", "guest_name", "table_number")
    date_hierarchy = "created_at"
    readonly_fields = ("submitted_at", "delivered_at", "cancelled_at", "charge_transaction")


@admin.register(PosOrderLine)
class PosOrderLineAdmin(ReadOnlyAdmin):
    list_display = ("order", "item_sku", "item_name", "quantity", "unit_price", "line_total")
    search_fields = ("order__reference", "item_name", "item_sku")


@admin.register(KitchenTicket)
class KitchenTicketAdmin(admin.ModelAdmin):
    list_display = ("order", "station", "status", "priority", "queued_at", "assigned_to")
    list_filter = ("station", "status", "priority")
    search_fields = ("order__reference",)


@admin.register(PosTender)
class PosTenderAdmin(ReadOnlyAdmin):
    list_display = ("reference", "order", "method", "status", "amount", "currency", "captured_at")
    list_filter = ("method", "status")


@admin.register(PosOrderEvent)
class PosOrderEventAdmin(ReadOnlyAdmin):
    list_display = ("order", "type", "actor", "occurred_at")
    list_filter = ("type",)


@admin.register(RestaurantTableSessionEvent)
class RestaurantTableSessionEventAdmin(ReadOnlyAdmin):
    list_display = ("table_session", "type", "actor", "occurred_at")
    list_filter = ("type",)
