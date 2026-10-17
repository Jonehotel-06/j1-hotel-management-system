"""Django-admin fallback for housekeeping, maintenance and room outages."""
from django.contrib import admin

from .models import HousekeepingTask, HousekeepingTaskEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(HousekeepingTask)
class HousekeepingTaskAdmin(admin.ModelAdmin):
    list_display = ("reference", "room", "type", "priority", "status", "assigned_to", "due_at", "created_at")
    list_filter = ("type", "priority", "status")
    search_fields = ("reference", "summary", "room__room_number")
    date_hierarchy = "created_at"


@admin.register(HousekeepingTaskEvent)
class HousekeepingTaskEventAdmin(ReadOnlyAdmin):
    list_display = ("task", "type", "actor", "previous_status", "new_status", "created_at")
    list_filter = ("type",)
