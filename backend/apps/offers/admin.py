# apps/offers/admin.py
from django.contrib import admin

from .models import GuestDiscount, GuestDiscountApplication, Offer


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ("title", "discount_type", "discount_value", "start_date", "end_date", "is_active", "is_featured")
    list_filter = ("is_active", "is_featured", "discount_type")
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = ("room_types",)


@admin.register(GuestDiscount)
class GuestDiscountAdmin(admin.ModelAdmin):
    list_display = ("guest", "discount_type", "discount_value", "start_date", "end_date", "is_active")
    list_filter = ("is_active", "discount_type")
    search_fields = ("guest__first_name", "guest__last_name", "guest__email", "reason")
    autocomplete_fields = ("guest",)


@admin.register(GuestDiscountApplication)
class GuestDiscountApplicationAdmin(admin.ModelAdmin):
    """Read-only: these rows are immutable financial history."""

    list_display = ("booking", "guest", "discount_type", "discount_value", "amount", "created_at")
    search_fields = ("booking__booking_reference", "guest__email")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
