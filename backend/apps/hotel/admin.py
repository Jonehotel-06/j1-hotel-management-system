# apps/hotel/admin.py
from django.contrib import admin

from .models import Facility, HotelPolicy, HotelSettings


@admin.register(HotelSettings)
class HotelSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Identity", {"fields": ("hotel_name", "tagline", "description")}),
        ("Contact", {"fields": ("address", "city", "state", "country", "phone", "email", "google_maps_url", "social_links")}),
        ("Stay rules", {"fields": ("check_in_time", "check_out_time", "min_stay_nights", "max_stay_nights", "currency")}),
        ("Money rules", {"fields": ("tax_rate_percent", "service_fee", "deposit_percent")}),
        ("Booking rules", {"fields": ("pending_booking_minutes", "cancellation_deadline_hours", "cancellation_fee_percent")}),
    )

    def has_add_permission(self, request):
        return not HotelSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(HotelPolicy)
class HotelPolicyAdmin(admin.ModelAdmin):
    list_display = ("title", "key", "is_active", "display_order", "updated_at")
    list_filter = ("is_active",)
    prepopulated_fields = {"key": ("title",)}


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ("name", "icon", "is_active", "display_order")
    list_filter = ("is_active",)
    prepopulated_fields = {"slug": ("name",)}
