# apps/events/admin.py
from django.contrib import admin

from .models import EventLocation, EventPayment, HotelEvent


@admin.register(EventLocation)
class EventLocationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "display_order")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(HotelEvent)
class HotelEventAdmin(admin.ModelAdmin):
    list_display = ("reference", "guest", "location", "event_date", "total_charged", "amount_paid", "payment_status", "status")
    list_filter = ("status", "payment_status", "location", "event_date")
    search_fields = ("reference", "guest__first_name", "guest__last_name", "guest__phone", "guest__email")
    readonly_fields = ("reference", "amount_paid", "payment_status", "charge_transaction", "created_at", "updated_at")


@admin.register(EventPayment)
class EventPaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "event", "amount", "method", "status", "paid_at")
    list_filter = ("method", "status")
    search_fields = ("reference", "external_reference", "event__reference")
    readonly_fields = ("reference", "collection_transaction", "created_at", "updated_at")
