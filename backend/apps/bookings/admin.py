# apps/bookings/admin.py
from django.contrib import admin

from .models import Booking, BookingRoom, Guest


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "country", "created_at")
    search_fields = ("first_name", "last_name", "email", "phone")


class BookingRoomInline(admin.TabularInline):
    model = BookingRoom
    extra = 0
    readonly_fields = ("room", "check_in", "check_out")
    can_delete = False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "booking_reference", "guest", "room_type", "check_in", "check_out",
        "status", "payment_status", "total_amount", "amount_paid", "source", "created_at",
    )
    list_filter = ("status", "payment_status", "source", "room_type")
    search_fields = ("booking_reference", "guest__first_name", "guest__last_name", "guest__email")
    readonly_fields = (
        "booking_reference", "price_per_night", "subtotal", "discount_amount",
        "extra_guest_fee_amount", "tax_amount", "fee_amount", "total_amount",
        "required_payment", "amount_paid", "refund_amount", "created_at", "updated_at",
        "checked_in_at", "checked_out_at", "cancelled_at",
    )
    inlines = [BookingRoomInline]
