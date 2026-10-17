# apps/bookings/admin.py
from django.contrib import admin

from .models import Booking, BookingRoom, Guest, RoomOccupant


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "country", "created_at")
    search_fields = ("first_name", "last_name", "email", "phone")


class RoomOccupantInline(admin.TabularInline):
    model = RoomOccupant
    fk_name = "booking_room"
    extra = 0
    fields = ("full_name", "email", "phone", "is_lead_occupant", "notes")
    readonly_fields = ("portal_invited_at",)
    verbose_name = "Room guest"
    verbose_name_plural = "Room guests (people staying in this room)"


class BookingRoomInline(admin.TabularInline):
    model = BookingRoom
    extra = 0
    readonly_fields = ("room", "check_in", "check_out", "checked_in_at", "checked_in_by", "not_arrived_at")
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


@admin.register(RoomOccupant)
class RoomOccupantAdmin(admin.ModelAdmin):
    """Look up "which room is this email staying in?" from one place.

    This is the record that gives a guest in the second or third room of a
    booking their own portal identity, so it is searchable by email.
    """

    list_display = ("email", "full_name", "room_number", "booking_reference", "portal_invited_at", "created_at")
    list_select_related = ("booking_room__room", "booking_room__booking")
    search_fields = (
        "email", "full_name", "phone",
        "booking_room__room__room_number",
        "booking_room__booking__booking_reference",
    )
    list_filter = ("is_lead_occupant",)
    readonly_fields = ("created_at", "updated_at", "portal_invited_at")

    @admin.display(description="Room")
    def room_number(self, obj):
        return obj.booking_room.room.room_number

    @admin.display(description="Booking")
    def booking_reference(self, obj):
        return obj.booking_room.booking.booking_reference
