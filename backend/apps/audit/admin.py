# apps/audit/admin.py
from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "object_type", "object_id", "ip_address", "created_at")
    list_filter = ("action", "object_type", "created_at")
    search_fields = ("summary", "object_id")
    readonly_fields = ("actor", "action", "object_type", "object_id", "summary", "changes", "metadata", "ip_address", "created_at")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Audit logs must not be casually deleted, even by superusers.
        return False
