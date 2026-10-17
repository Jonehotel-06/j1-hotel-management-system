"""Django-admin fallback for finance.

The immutable financial ledger (transactions, lines, postings, events) is
registered READ-ONLY. A posted transaction must never be editable — the API
enforces that by refusing updates, and the admin must not offer a screen that
looks like it could.
"""
from django.contrib import admin

from .models import (
    ApprovalRequest,
    CashMovement,
    CashSession,
    Expense,
    ExpenseEvent,
    FinancialControlPolicy,
    FinancialEvent,
    FinancialLine,
    FinancialTransaction,
    Folio,
    FolioPosting,
    PaymentAllocation,
    RefundApplication,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FinancialControlPolicy)
class FinancialControlPolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "effective_from", "is_active", "require_separate_approver", "expense_manager_approval_threshold")
    list_filter = ("is_active", "require_separate_approver")
    search_fields = ("name",)


@admin.register(Folio)
class FolioAdmin(admin.ModelAdmin):
    list_display = ("reference", "booking", "status", "currency", "created_at")
    list_filter = ("status",)
    search_fields = ("reference", "booking__booking_reference")


@admin.register(FinancialTransaction)
class FinancialTransactionAdmin(ReadOnlyAdmin):
    list_display = ("reference", "type", "status", "business_date", "currency", "created_at")
    list_filter = ("type", "status", "business_date")
    search_fields = ("reference", "source_reference", "narrative")
    date_hierarchy = "business_date"


@admin.register(FinancialLine)
class FinancialLineAdmin(ReadOnlyAdmin):
    list_display = ("transaction", "account_code", "direction", "amount", "description")
    list_filter = ("direction", "account_code")
    search_fields = ("transaction__reference", "account_code")


@admin.register(FolioPosting)
class FolioPostingAdmin(ReadOnlyAdmin):
    list_display = ("folio", "line", "kind", "effect", "amount", "created_at")
    list_filter = ("kind", "effect")
    search_fields = ("folio__reference", "transaction__reference")


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(ReadOnlyAdmin):
    list_display = ("payment", "folio", "amount", "created_at")
    search_fields = ("payment__reference", "folio__reference")


@admin.register(RefundApplication)
class RefundApplicationAdmin(ReadOnlyAdmin):
    list_display = ("refund", "payment", "amount", "source_key", "created_at")
    search_fields = ("source_key", "payment__reference", "refund__reference")


@admin.register(CashSession)
class CashSessionAdmin(admin.ModelAdmin):
    list_display = ("reference", "cashier", "status", "opened_at", "closed_at", "opening_float", "expected_cash")
    list_filter = ("status",)
    search_fields = ("reference", "cashier__email")


@admin.register(CashMovement)
class CashMovementAdmin(ReadOnlyAdmin):
    list_display = ("cash_session", "type", "amount", "currency", "occurred_at")
    list_filter = ("type", "currency")
    search_fields = ("cash_session__reference", "source_reference")


class ExpenseEventInline(admin.TabularInline):
    model = ExpenseEvent
    extra = 0
    readonly_fields = ("type", "actor", "details", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "status", "category", "amount", "currency",
        "payment_method", "incurred_on", "payee", "requested_by", "paid_at",
    )
    list_filter = ("status", "payment_method", "accounting_class", "category")
    search_fields = ("reference", "category", "payee", "supplier_reference", "description")
    date_hierarchy = "incurred_on"
    inlines = [ExpenseEventInline]
    readonly_fields = (
        "reference", "status", "approved_by", "approved_at", "posted_by", "posted_at",
        "financial_transaction", "settlement_transaction", "paid_at", "paid_by",
        "created_at", "updated_at",
    )


@admin.register(ExpenseEvent)
class ExpenseEventAdmin(ReadOnlyAdmin):
    list_display = ("expense", "type", "actor", "created_at")
    list_filter = ("type",)
    search_fields = ("expense__reference",)


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "type", "status", "amount", "currency", "requester", "created_at")
    list_filter = ("type", "status")
    search_fields = ("reference", "source_reference", "requester__email")
    readonly_fields = ("reference", "created_at", "updated_at")


@admin.register(FinancialEvent)
class FinancialEventAdmin(ReadOnlyAdmin):
    list_display = ("transaction", "type", "actor", "occurred_at")
    list_filter = ("type",)
    search_fields = ("transaction__reference",)
