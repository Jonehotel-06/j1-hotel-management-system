"""Read-only operational history in Django admin."""
from django.contrib import admin

from .models import Stay, StayEvent, StayRoom


class StayRoomInline(admin.TabularInline):
    model = StayRoom
    extra = 0
    readonly_fields = tuple(field.name for field in StayRoom._meta.fields)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class StayEventInline(admin.TabularInline):
    model = StayEvent
    extra = 0
    readonly_fields = tuple(field.name for field in StayEvent._meta.fields)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Stay)
class StayAdmin(admin.ModelAdmin):
    list_display = ("reference", "booking", "guest", "status", "expected_arrival", "expected_departure")
    search_fields = ("reference", "booking__booking_reference", "guest__email")
    readonly_fields = tuple(field.name for field in Stay._meta.fields)
    inlines = (StayRoomInline, StayEventInline)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD")

    def has_delete_permission(self, request, obj=None):
        return False
