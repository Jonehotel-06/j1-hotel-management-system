# apps/payments/admin.py
from django.contrib import admin

from .models import Payment, Refund


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "booking", "provider", "amount", "currency", "status", "transaction_id", "paid_at")
    list_filter = ("provider", "status", "currency")
    search_fields = ("reference", "transaction_id", "booking__booking_reference", "booking__guest__email")
    readonly_fields = (
        "booking", "user", "reference", "provider", "amount", "currency", "status", "channel",
        "gateway_response", "transaction_id", "paid_at", "notes", "metadata", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "payment", "amount", "currency", "status", "paystack_refund_id", "processed_at", "failed_at")
    list_filter = ("status", "currency")
    search_fields = (
        "booking__booking_reference", "payment__reference", "payment__transaction_id",
        "paystack_refund_id", "paystack_refund_reference", "paystack_transaction_reference",
        "cancellation_request__cancellation_reference",
    )
    readonly_fields = (
        "booking", "payment", "cancellation_request", "amount", "currency", "status", "requested_by",
        "paystack_transaction_id", "paystack_transaction_reference", "paystack_refund_id",
        "paystack_refund_reference", "customer_note", "merchant_note", "failure_reason",
        "submitted_at", "processed_at", "failed_at", "metadata", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
