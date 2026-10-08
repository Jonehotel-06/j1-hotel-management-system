# apps/notifications/admin.py
from django.contrib import admin

from .models import EmailLog, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "recipient", "is_read", "created_at")
    list_filter = ("type", "is_read")
    search_fields = ("title", "message", "recipient__email")


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = (
        "id", "kind", "status", "to_email", "booking_reference",
        "created_at", "sent_at",
    )
    list_filter = ("status", "kind")
    search_fields = (
        "to_email", "subject", "booking_reference", "payment_reference",
        "provider_message_id",
    )
    readonly_fields = (
        "kind", "to_email", "subject", "body", "booking_reference", "payment_reference",
        "booking_id", "attach_receipt_pdf", "status", "provider_message_id",
        "error_class", "error_message", "failure_stage", "created_by",
        "created_at", "sent_at", "failed_at",
    )

    def has_add_permission(self, request):
        return False
