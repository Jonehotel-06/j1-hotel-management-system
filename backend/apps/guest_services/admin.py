"""Django-admin fallback for guest service requests, QR links and laundry."""
from django.contrib import admin

from .models import LaundryItem, LaundryOrder, ServiceQRLink, ServiceRequest, ServiceRequestEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "category", "priority", "status", "owner_team", "assigned_to", "room", "created_at")
    list_filter = ("category", "priority", "status", "owner_team")
    search_fields = ("reference", "summary", "detail", "portal_email")
    date_hierarchy = "created_at"


@admin.register(ServiceQRLink)
class ServiceQRLinkAdmin(admin.ModelAdmin):
    list_display = ("reference", "target_key", "target_type", "room", "table_number", "label", "is_active", "expires_at")
    list_filter = ("target_type", "is_active")
    search_fields = ("reference", "target_key", "label")


@admin.register(ServiceRequestEvent)
class ServiceRequestEventAdmin(ReadOnlyAdmin):
    list_display = ("request", "type", "actor", "previous_status", "new_status", "created_at")
    list_filter = ("type",)


class LaundryItemInline(admin.TabularInline):
    model = LaundryItem
    extra = 0
    readonly_fields = ("description", "quantity", "service_type", "unit_price")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LaundryOrder)
class LaundryOrderAdmin(admin.ModelAdmin):
    list_display = ("reference", "guest_name", "room", "status", "payment_method", "total_amount", "currency", "created_at")
    list_filter = ("status", "payment_method")
    search_fields = ("reference", "guest_name", "guest_phone")
    date_hierarchy = "created_at"
    inlines = [LaundryItemInline]

# Product configuration only: quantities still belong to inventory/procurement.
from .models import MiniMartProduct, MiniMartSale, MiniMartSaleLine  # noqa: E402
from apps.accounts.capabilities import has_capability  # noqa: E402


@admin.register(MiniMartProduct)
class MiniMartProductAdmin(admin.ModelAdmin):
    list_display = ("item", "location", "selling_price", "is_active")
    list_filter = ("is_active", "item__category", "location")
    search_fields = ("item__name", "item__sku", "item__barcode")

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff and request.user.role in {"ADMIN", "GENERAL_MANAGER"} and has_capability(request.user, "inventory.manage")

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False  # Retire the configuration; preserve historical sale links.


@admin.register(MiniMartSale, MiniMartSaleLine)
class MiniMartEvidenceAdmin(ReadOnlyAdmin):
    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser and request.user.is_active

    def has_module_permission(self, request):
        return self.has_view_permission(request)
