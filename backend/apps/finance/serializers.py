"""Read-oriented finance API serializers; posting remains service-only."""
from decimal import Decimal

from rest_framework import serializers

from apps.core.utils import hotel_today

from .models import ApprovalRequest, CashSession, Expense, ExpenseEvent, FinancialControlPolicy, FinancialLine, FinancialTransaction, Folio, FolioPosting


class FolioSerializer(serializers.ModelSerializer):
    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True)
    stay_reference = serializers.CharField(source="stay.reference", read_only=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Folio
        fields = [
            "id", "reference", "kind", "status", "currency", "guest_name", "booking_reference", "stay_reference",
            "balance", "opened_at", "closed_at", "notes",
        ]
        read_only_fields = fields


class FolioPostingSerializer(serializers.ModelSerializer):
    transaction_reference = serializers.CharField(source="transaction.reference", read_only=True)
    financial_line_account = serializers.CharField(source="line.account_code", read_only=True)

    class Meta:
        model = FolioPosting
        fields = [
            "id", "transaction_reference", "financial_line_account", "kind", "effect", "amount", "currency",
            "description", "business_date", "source_reference", "created_at",
        ]
        read_only_fields = fields


class FinancialTransactionSerializer(serializers.ModelSerializer):
    initiated_by_email = serializers.CharField(source="initiated_by.email", read_only=True)
    approved_by_email = serializers.CharField(source="approved_by.email", read_only=True)
    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = FinancialTransaction
        fields = [
            "id", "reference", "type", "status", "business_date", "occurred_at", "posted_at", "currency",
            "source_reference", "external_reference", "narrative", "initiated_by_email", "approved_by_email",
            "line_count", "created_at",
        ]
        read_only_fields = fields


class FinancialLineSerializer(serializers.ModelSerializer):
    folio_reference = serializers.CharField(source="folio.reference", read_only=True)

    class Meta:
        model = FinancialLine
        fields = ["id", "folio_reference", "account_code", "direction", "amount", "description", "created_at"]
        read_only_fields = fields


class FinancialTransactionDetailSerializer(FinancialTransactionSerializer):
    lines = FinancialLineSerializer(many=True, read_only=True)

    class Meta(FinancialTransactionSerializer.Meta):
        fields = FinancialTransactionSerializer.Meta.fields + ["metadata", "lines"]


class CashSessionSerializer(serializers.ModelSerializer):
    cashier_email = serializers.CharField(source="cashier.email", read_only=True)
    opened_by_email = serializers.CharField(source="opened_by.email", read_only=True)
    closed_by_email = serializers.CharField(source="closed_by.email", read_only=True)

    class Meta:
        model = CashSession
        fields = [
            "id", "reference", "cashier_email", "business_date", "location", "terminal", "status",
            "opening_float", "expected_cash", "counted_cash", "variance", "opened_at", "closed_at",
            "opened_by_email", "closed_by_email", "notes",
        ]
        read_only_fields = fields


class ApprovalRequestSerializer(serializers.ModelSerializer):
    requester_email = serializers.CharField(source="requester.email", read_only=True)
    reviewer_email = serializers.CharField(source="reviewer.email", read_only=True)
    financial_reference = serializers.CharField(source="financial_transaction.reference", read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = [
            "id", "reference", "type", "status", "amount", "currency", "source_reference", "requester_email",
            "reviewer_email", "reviewed_at", "expires_at", "financial_reference", "reason", "review_note",
            "policy_snapshot", "created_at",
        ]
        read_only_fields = fields


class ApprovalReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_note = serializers.CharField(required=False, allow_blank=True, default="")


class FinancialControlPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialControlPolicy
        fields = [
            "id", "name", "effective_from", "is_active", "require_separate_approver",
            "refund_manager_approval_threshold", "discount_manager_approval_threshold",
            "cash_variance_manager_approval_threshold", "expense_manager_approval_threshold", "notes", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_effective_from(self, value):
        if value < hotel_today():
            raise serializers.ValidationError("A new control policy cannot be backdated; create it for today or a future hotel date.")
        return value

    def validate(self, attrs):
        if self.instance is not None:
            raise serializers.ValidationError("Financial control policies are effective-dated; create a successor instead of editing history.")
        return attrs


class ExpenseEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = ExpenseEvent
        fields = ["type", "actor_email", "details", "created_at"]
        read_only_fields = fields


class ExpenseListSerializer(serializers.ModelSerializer):
    requested_by_email = serializers.EmailField(source="requested_by.email", read_only=True)
    approved_by_email = serializers.EmailField(source="approved_by.email", read_only=True, allow_null=True)
    posted_by_email = serializers.EmailField(source="posted_by.email", read_only=True, allow_null=True)
    approval_reference = serializers.CharField(source="approval_request.reference", read_only=True, allow_null=True)
    financial_reference = serializers.CharField(source="financial_transaction.reference", read_only=True, allow_null=True)
    cash_session_reference = serializers.CharField(source="cash_session.reference", read_only=True, allow_null=True)

    class Meta:
        model = Expense
        fields = [
            "reference", "status", "category", "accounting_class", "payment_method", "amount", "currency", "incurred_on",
            "payee", "supplier_reference", "description", "receipt_url", "requested_by_email", "approved_by_email", "approved_at",
            "approval_reference", "cash_session_reference", "financial_reference", "posted_by_email", "posted_at", "created_at",
        ]
        read_only_fields = fields


class ExpenseDetailSerializer(ExpenseListSerializer):
    events = ExpenseEventSerializer(many=True, read_only=True)
    notes = serializers.CharField(read_only=True)

    class Meta(ExpenseListSerializer.Meta):
        fields = ExpenseListSerializer.Meta.fields + ["notes", "events"]


class ExpenseCreateSerializer(serializers.Serializer):
    category = serializers.CharField(max_length=120)
    accounting_class = serializers.ChoiceField(choices=[("OPERATING", "Operating expense"), ("INVENTORY", "Inventory asset")], default="OPERATING")
    payment_method = serializers.ChoiceField(choices=[("CASH", "Cash paid out"), ("BANK_TRANSFER", "Bank transfer"), ("CARD", "Card / terminal"), ("PAYABLE", "Accounts payable")], default="BANK_TRANSFER")
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    currency = serializers.CharField(required=False, default="NGN", min_length=3, max_length=3)
    incurred_on = serializers.DateField(required=False)
    payee = serializers.CharField(required=False, allow_blank=True, max_length=255)
    supplier_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    receipt_url = serializers.URLField(required=False, allow_blank=True, max_length=200)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64)
    idempotency_key = serializers.CharField(max_length=128)

    def validate(self, attrs):
        if attrs.get("payment_method") == "CASH" and not attrs.get("cash_session_reference"):
            raise serializers.ValidationError({"cash_session_reference": "Cash expenses require a cash-session reference."})
        if attrs.get("payment_method") != "CASH" and attrs.get("cash_session_reference"):
            raise serializers.ValidationError({"cash_session_reference": "Only CASH expenses may use a cash-session reference."})
        attrs["currency"] = attrs.get("currency", "NGN").upper()
        return attrs


class ExpenseReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_note = serializers.CharField(required=False, allow_blank=True, max_length=5000)
