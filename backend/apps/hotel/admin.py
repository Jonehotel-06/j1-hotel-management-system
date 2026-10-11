# apps/hotel/admin.py
from django.contrib import admin

from .models import Facility, HotelPolicy, HotelSettings


@admin.register(HotelSettings)
class HotelSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Identity", {"fields": ("hotel_name", "tagline", "description")}),
        ("Contact", {"fields": ("address", "city", "state", "country", "phone", "email", "google_maps_url", "social_links")}),
        ("Stay rules", {"fields": ("check_in_time", "check_out_time", "min_stay_nights", "max_stay_nights", "currency")}),
        ("Money rules", {"fields": ("service_fee", "deposit_percent")}),
        ("Statutory tax (never billed to guests)", {
            "fields": ("statutory_tax_enabled", "statutory_tax_rate_percent", "statutory_tax_basis"),
            "description": "Potential hotel tax exposure accrued for reporting/remittance only. "
                           "Customer billing never includes an added tax; see "
                           "apps.finance.services.tax_policy. The legacy tax_rate_percent field "
                           "is retained for historical records and no longer drives billing.",
        }),
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
