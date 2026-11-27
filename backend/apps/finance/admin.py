"""Django-admin fallback for finance.

The immutable financial ledger (transactions, lines, postings, events) is
registered READ-ONLY. A posted transaction must never be editable — the API
enforces that by refusing updates, and the admin must not offer a screen that
looks like it could.
"""
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.urls import path
from django.db.models import Sum, Q, DecimalField, F, Value, Case, When
from django.db.models.functions import Coalesce

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
    """Read-mostly folio management, with the guest balance shown in the list.

    Repair (2026-11-26): the hotel could not find a folio by the guest or the
    booking, could not see the balance, and had no way to correct a mistyped
    entry. Search now covers guest name/email, booking reference and folio
    reference; the balance columns are computed from the POSTED statement items
    (never a cached field), and the balance appears on the folio change page.
    """

    list_display = ("reference", "guest", "booking", "status", "currency", "charges", "credits", "balance", "created_at")
    list_filter = ("status", "kind", "currency")
    search_fields = (
        "reference", "booking__booking_reference", "stay__reference",
        "guest__full_name", "guest__email", "guest__phone",
        "postings__transaction__reference", "postings__source_reference",
    )
    autocomplete_fields = ("guest", "booking", "stay")
    date_hierarchy = "opened_at"
    readonly_fields = ("balance_display", "created_at", "updated_at", "opened_at", "closed_at")

    def get_queryset(self, request):
        from .services.folio_service import folio_balance_queryset

        return folio_balance_queryset(super().get_queryset(request)).select_related(
            "guest", "booking", "stay"
        ).distinct()

    @admin.display(description="Charges")
    def charges(self, obj):
        return getattr(obj, "charges_total", None)

    @admin.display(description="Credits")
    def credits(self, obj):
        return getattr(obj, "credits_total", None)

    @admin.display(description="Balance (NGN)", ordering="balance")
    def balance(self, obj):
        return getattr(obj, "balance", None)

    @admin.display(description="Current balance (posted entries only)")
    def balance_display(self, obj):
        if obj is None or not obj.pk:
            return "—"
        from .services.folio_service import folio_balance_queryset

        row = folio_balance_queryset(Folio.objects.filter(pk=obj.pk)).first()
        return f"{row.balance} {row.currency} (charges {row.charges_total} − credits {row.credits_total})"


@admin.register(FinancialTransaction)
class FinancialTransactionAdmin(admin.ModelAdmin):
    """Posted history is immutable; only DRAFT entries may be changed/deleted.

    Repair (2026-11-26): the whole ledger was read-only in the admin, so a
    mistyped DRAFT could not be corrected and a POSTED entry offered no path at
    all. Drafts are now editable/deletable, and everything that is posted or
    voided stays untouchable — the model itself refuses such a write.
    """

    list_display = ("reference", "type", "status", "business_date", "currency", "initiated_by", "approved_by", "created_at")
    list_filter = ("type", "status", "business_date", "currency")
    search_fields = (
        "reference", "source_reference", "narrative", "external_reference",
        "folio_postings__folio__reference", "folio_postings__folio__guest__full_name",
        "folio_postings__folio__booking__booking_reference",
    )
    date_hierarchy = "business_date"
    readonly_fields = ("reference", "posted_at", "approved_at", "created_at", "updated_at")

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.status == FinancialTransaction.Status.DRAFT

    def has_delete_permission(self, request, obj=None):
        if obj is None:
            return False
        return obj.status == FinancialTransaction.Status.DRAFT

    def has_add_permission(self, request):
        # Money is posted by the ledger services, never typed straight into the
        # journal from the admin.
        return False


@admin.register(FinancialLine)
class FinancialLineAdmin(ReadOnlyAdmin):
    list_display = ("transaction", "account_code", "direction", "amount", "description")
    list_filter = ("direction", "account_code")
    search_fields = ("transaction__reference", "account_code")


class FolioPostingVoidForm(forms.Form):
    reason = forms.CharField(
        max_length=500, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Why this entry is being voided. Stored on the reversal and in the audit log.",
    )

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if len(reason) < 5:
            raise forms.ValidationError("Give the reason for voiding this entry (at least 5 characters).")
        return reason


@admin.register(FolioPosting)
class FolioPostingAdmin(ReadOnlyAdmin):
    """Statement items are immutable; the action posts an approved reversal.

    Repair (2026-11-26): the admin had no search by guest/booking/folio and no
    controlled correction at all. The rows themselves still cannot be edited or
    deleted — the "Void selected entries" action posts the offsetting journal.
    """

    list_display = (
        "folio", "guest", "kind", "effect", "settles", "amount", "currency",
        "transaction", "is_reversal", "business_date", "created_at",
    )
    list_filter = ("kind", "effect", "settles", "currency", "business_date")
    search_fields = (
        "folio__reference", "transaction__reference", "source_reference", "description",
        "folio__guest__full_name", "folio__guest__email",
        "folio__booking__booking_reference", "folio__stay__reference",
    )
    date_hierarchy = "business_date"
    actions = ("void_selected_entries",)

    @admin.display(description="Guest")
    def guest(self, obj):
        return obj.folio.guest.full_name if obj.folio_id else ""

    @admin.display(description="Reversal?", boolean=True)
    def is_reversal(self, obj):
        return obj.reversal_of_id is not None

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "void-entry/", self.admin_site.admin_view(self.void_entry_view),
                name="finance_folioposting_void",
            ),
        ]
        return custom + urls

    def void_selected_entries(self, request, queryset):
        selected = list(queryset.values_list("pk", flat=True))
        if not selected:
            self.message_user(request, "Select at least one entry to void.", messages.WARNING)
            return None
        if len(selected) > 50:
            self.message_user(request, "Void at most 50 entries at a time.", messages.WARNING)
            return None
        request.session["folio_posting_void_pks"] = selected
        return redirect("admin:finance_folioposting_void")

    void_selected_entries.short_description = "Void selected folio entries (posts an approved reversal)"

    def void_entry_view(self, request):
        from .services.folio_service import void_folio_posting

        pks = request.session.get("folio_posting_void_pks") or []
        postings = list(
            FolioPosting.objects.filter(pk__in=pks).select_related("folio", "transaction", "line").order_by("pk")
        )
        if not postings:
            self.message_user(request, "Nothing is selected to void.", messages.WARNING)
            return redirect("admin:finance_folioposting_changelist")
        from apps.accounts.capabilities import has_capability

        if not has_capability(request.user, "payment.refund.approve"):
            raise PermissionDenied(
                "Voiding a posted folio entry requires the sensitive-money approval capability."
            )
        form = FolioPostingVoidForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            reason = form.cleaned_data["reason"]
            voided, failed = 0, 0
            for posting in postings:
                try:
                    void_folio_posting(
                        posting=posting, actor=request.user, reason=reason,
                        allow_closed_folio=has_capability(request.user, "finance.controls.manage"),
                        request=request,
                    )
                    voided += 1
                except (ValidationError, PermissionDenied) as exc:
                    failed += 1
                    self.message_user(
                        request, f"Entry {posting.pk} was not voided: {exc}", messages.ERROR,
                    )
            if voided:
                self.message_user(
                    request,
                    f"{voided} folio entr{'y' if voided == 1 else 'ies'} voided with an offsetting reversal.",
                    messages.SUCCESS,
                )
            if not failed:
                request.session.pop("folio_posting_void_pks", None)
            return redirect("admin:finance_folioposting_changelist")
        context = {
            **self.admin_site.each_context(request),
            "title": "Void selected folio entries",
            "postings": postings,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "admin/finance/folioposting/void_entries.html", context)


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
