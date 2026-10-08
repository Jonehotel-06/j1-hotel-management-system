# apps/enquiries/admin.py
from django.contrib import admin

from .models import Enquiry


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    list_display = ("subject", "name", "email", "enquiry_type", "status", "cancellation_status", "refund_status", "created_at")
    list_filter = ("enquiry_type", "status", "cancellation_status", "refund_status")
    search_fields = (
        "name", "email", "subject", "message", "cancellation_reference",
        "booking_reference", "payment_reference", "related_booking__booking_reference",
        "related_payment__reference", "related_payment__transaction_id",
    )
    readonly_fields = (
        "cancellation_reference", "public_access_token_hash", "public_access_expires_at",
        "related_booking", "related_payment", "processed_by", "processed_at", "metadata",
        "email_events", "created_at", "updated_at",
    )
