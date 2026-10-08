"""Portal credential records are intentionally view-only in Django admin."""
from django.contrib import admin

from .models import PortalAccessChallenge, PortalSession


@admin.register(PortalAccessChallenge)
class PortalAccessChallengeAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "purpose", "expires_at", "consumed_at", "created_at")
    search_fields = ("email",)
    readonly_fields = tuple(field.name for field in PortalAccessChallenge._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PortalSession)
class PortalSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "authentication_method", "issued_at", "expires_at", "revoked_at")
    search_fields = ("email",)
    readonly_fields = tuple(field.name for field in PortalSession._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD")

    def has_delete_permission(self, request, obj=None):
        return False
