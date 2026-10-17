"""Django-admin fallback for maintenance work orders and room outages."""
from django.contrib import admin

from .models import MaintenanceEvent, MaintenanceWorkOrder, RoomOutage


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MaintenanceWorkOrder)
class MaintenanceWorkOrderAdmin(admin.ModelAdmin):
    list_display = ("reference", "room", "category", "priority", "status", "assigned_to", "due_at", "created_at")
    list_filter = ("category", "priority", "status")
    search_fields = ("reference", "summary", "description", "room__room_number")
    date_hierarchy = "created_at"


@admin.register(RoomOutage)
class RoomOutageAdmin(admin.ModelAdmin):
    list_display = ("room", "status", "outage_status", "reason", "started_at", "cleared_at")
    list_filter = ("status", "outage_status")
    search_fields = ("room__room_number", "reason")


@admin.register(MaintenanceEvent)
class MaintenanceEventAdmin(ReadOnlyAdmin):
    list_display = ("work_order", "type", "actor", "previous_status", "new_status", "created_at")
    list_filter = ("type",)
