# apps/rooms/admin.py
from django.contrib import admin

from .models import Amenity, Room, RoomType, RoomTypeImage


@admin.register(Amenity)
class AmenityAdmin(admin.ModelAdmin):
    list_display = ("name", "icon", "is_active")
    prepopulated_fields = {"slug": ("name",)}


class RoomTypeImageInline(admin.TabularInline):
    model = RoomTypeImage
    extra = 0


@admin.register(RoomType)
class RoomTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "base_price", "max_guests", "is_featured", "is_active", "display_order")
    list_filter = ("is_active", "is_featured")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("amenities",)
    inlines = [RoomTypeImageInline]


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("room_number", "room_type", "floor", "status", "housekeeping_status", "is_active")
    list_filter = ("status", "housekeeping_status", "room_type", "is_active")
    search_fields = ("room_number",)
