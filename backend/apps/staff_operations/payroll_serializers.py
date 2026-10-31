"""Sensitive payroll serializers; no salary fields enter ordinary staff profile APIs."""
from decimal import Decimal

from django.db.models import Q
from rest_framework import serializers

from .payroll_models import (
    LatenessDeductionPolicy, PayrollEvent, PayrollLine, PayrollPeriod, PayrollSalaryPayment,
    PayrollStockShortageAllocation, PayrollStockShortageCase, StaffCompensation,
    StaffLatenessDeduction,
)


class StaffCompensationCreateSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    effective_from = serializers.DateField()
    currency = serializers.CharField(required=False, allow_blank=False, max_length=3, default="NGN")
    basic_salary = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    housing_allowance = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    transport_allowance = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    overtime_rate = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), default="0.00")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class StaffCompensationSerializer(serializers.ModelSerializer):
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    employee_code = serializers.CharField(source="staff.staff_profile.employee_code", read_only=True)
    department = serializers.CharField(source="staff.staff_profile.department", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)

    class Meta:
        model = StaffCompensation
        fields = [
            "id", "staff", "staff_email", "staff_name", "employee_code", "department", "effective_from",
            "currency", "basic_salary", "housing_allowance", "transport_allowance", "overtime_rate",
            "created_by_email", "notes", "created_at",
        ]
        read_only_fields = fields


class PayrollComponentInputSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))


class PayrollAdjustmentInputSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    overtime_hours = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("0.00"), max_value=Decimal("1000.00"), required=False, default="0.00")
    allowances = PayrollComponentInputSerializer(many=True, required=False, default=list, max_length=30)
    bonus = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    deductions = PayrollComponentInputSerializer(many=True, required=False, default=list, max_length=30)


class PayrollPeriodCreateSerializer(serializers.Serializer):
    starts_on = serializers.DateField()
    ends_on = serializers.DateField()
    department = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    adjustments = PayrollAdjustmentInputSerializer(many=True, required=False, default=list, max_length=500)
    idempotency_key = serializers.CharField(max_length=128, allow_blank=False)
    replaces_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")


class LatenessPolicySerializer(serializers.ModelSerializer):
    updated_by_email = serializers.EmailField(source="updated_by.email", read_only=True, allow_null=True)

    class Meta:
        model = LatenessDeductionPolicy
        fields = [
            "id", "is_enabled", "grace_minutes", "rate_per_minute", "deduction_percent", "updated_by_email",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class LatenessPolicyUpdateSerializer(serializers.Serializer):
    is_enabled = serializers.BooleanField()
    grace_minutes = serializers.IntegerField(min_value=0, max_value=1440)
    rate_per_minute = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"))

    def validate(self, attrs):
        if attrs["is_enabled"] and attrs["rate_per_minute"] <= 0:
            raise serializers.ValidationError({"rate_per_minute": "An enabled policy needs a positive rate."})
        return attrs


class StaffLatenessDeductionSerializer(serializers.ModelSerializer):
    staff_id = serializers.IntegerField(source="staff.pk", read_only=True)
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    employee_code = serializers.CharField(source="staff.staff_profile.employee_code", read_only=True, allow_null=True)
    department = serializers.CharField(source="staff.staff_profile.department", read_only=True, allow_null=True)
    attendance_reference = serializers.CharField(source="attendance_record.reference", read_only=True)
    shift_reference = serializers.CharField(source="shift.reference", read_only=True, allow_null=True)
    applied_period_reference = serializers.SerializerMethodField()
    applied_period_status = serializers.SerializerMethodField()
    reimbursement_period_reference = serializers.SerializerMethodField()
    revocation_requested_by_email = serializers.EmailField(
        source="revocation_requested_by.email", read_only=True, allow_null=True,
    )
    decided_by_email = serializers.EmailField(source="decided_by.email", read_only=True, allow_null=True)

    def get_applied_period_reference(self, obj):
        return obj.applied_payroll_line.period.reference if obj.applied_payroll_line_id else None

    def get_applied_period_status(self, obj):
        return obj.applied_payroll_line.period.status if obj.applied_payroll_line_id else None

    def get_reimbursement_period_reference(self, obj):
        return obj.reimbursement_payroll_line.period.reference if obj.reimbursement_payroll_line_id else None

    class Meta:
        model = StaffLatenessDeduction
        fields = [
            "id", "reference", "staff_id", "staff_email", "staff_name", "employee_code", "department",
            "attendance_reference", "shift_reference", "business_date", "scheduled_start_at", "clock_in_at",
            "late_minutes", "grace_minutes", "rate_per_minute", "deduction_percent", "monthly_pay_basis", "days_in_month",
            "daily_pay", "amount", "currency", "status", "applied_period_reference", "applied_period_status",
            "revocation_reason", "revocation_evidence_reference", "revocation_requested_by_email",
            "revocation_requested_at", "decided_by_email", "decided_at", "decision_note",
            "reimbursement_period_reference", "reimbursed_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class LatenessRevocationRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=5, max_length=2000)
    evidence_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")


class LatenessRevocationDecisionSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    decision_note = serializers.CharField(min_length=5, max_length=1000)


class PayrollStockShortageCreateSerializer(serializers.Serializer):
    department_id = serializers.IntegerField(min_value=1, required=False)
    department_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    item_summary = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    total_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    stock_count_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    discovered_at = serializers.DateTimeField()
    stock_count_line_id = serializers.IntegerField(min_value=1)
    responsibility_type = serializers.ChoiceField(choices=["STAFF", "DEPARTMENT"])
    staff_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list)
    incident_date = serializers.DateField()
    effective_date = serializers.DateField()
    evidence_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")

    def validate(self, attrs):
        if not attrs.get("stock_count_reference", "").strip() and not attrs.get("item_summary", "").strip():
            raise serializers.ValidationError({"item_summary": "Describe the missing item(s), or link a posted stock count."})
        if not attrs.get("stock_count_reference", "").strip() and attrs.get("total_amount") is None:
            raise serializers.ValidationError({"total_amount": "Enter the total missing-stock value, or link a posted stock count."})
        if not attrs.get("stock_count_reference", "").strip() and not attrs.get("evidence_reference", "").strip():
            raise serializers.ValidationError({"evidence_reference": "Record the stock-count, incident, or investigation evidence reference."})
        if attrs["effective_date"] < attrs["incident_date"]:
            raise serializers.ValidationError({"effective_date": "The payroll effective date cannot precede the incident date."})
        return attrs


class ShortageAllocationInputSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))


class PayrollStockShortageDecisionSerializer(serializers.Serializer):
    allocations = ShortageAllocationInputSerializer(many=True, required=False, default=list)
    approved = serializers.BooleanField()
    decision_note = serializers.CharField(min_length=5, max_length=1000, allow_blank=False)


class PayrollStockShortageAllocationSerializer(serializers.ModelSerializer):
    staff_id = serializers.IntegerField(read_only=True)
    applied_period_reference = serializers.CharField(source="applied_payroll_line.period.reference", read_only=True, allow_null=True)

    class Meta:
        model = PayrollStockShortageAllocation
        fields = [
            "id", "staff_id", "employee_code", "employee_name", "department", "amount",
            "applied_payroll_line", "applied_period_reference", "created_at",
        ]
        read_only_fields = fields


class PayrollStockShortageSerializer(serializers.ModelSerializer):
    department_ids = serializers.PrimaryKeyRelatedField(source="responsible_departments", many=True, read_only=True)
    department_id = serializers.IntegerField(source="department.pk", read_only=True)
    stock_count_reference = serializers.CharField(source="stock_count.reference", read_only=True, allow_null=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    decided_by_email = serializers.EmailField(source="decided_by.email", read_only=True, allow_null=True)
    staff_count = serializers.SerializerMethodField()
    applied_count = serializers.SerializerMethodField()
    share_amount = serializers.SerializerMethodField()

    class Meta:
        model = PayrollStockShortageCase
        fields = [
            "id", "reference", "department_id", "department_ids", "department_code", "department_name", "stock_count_reference",
            "item_summary", "shortage_lines", "discovered_at", "responsibility_type", "responsibility", "incident_date", "effective_date", "total_amount", "currency",
            "evidence_reference", "notes", "status", "created_by_email", "decided_by_email", "decided_at",
            "decision_note", "staff_count", "applied_count", "share_amount", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_staff_count(self, obj):
        allocations = getattr(obj, "allocations", None)
        return allocations.count() if allocations is not None else 0

    def get_applied_count(self, obj):
        allocations = getattr(obj, "allocations", None)
        if allocations is None:
            return 0
        return allocations.filter(applied_payroll_line__isnull=False).count()

    def get_share_amount(self, obj):
        allocation = obj.allocations.order_by("employee_code", "pk").first()
        return str(allocation.amount) if allocation else None


class PayrollStockShortageDetailSerializer(PayrollStockShortageSerializer):
    allocations = PayrollStockShortageAllocationSerializer(many=True, read_only=True)

    class Meta(PayrollStockShortageSerializer.Meta):
        fields = PayrollStockShortageSerializer.Meta.fields + ["allocations"]


class PayrollReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_note = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")


class PayrollPaymentSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PayrollPeriod.PaymentMethod.choices)
    external_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")


class PayrollSalaryPaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"),
    )
    method = serializers.ChoiceField(choices=PayrollSalaryPayment.Method.choices)
    payment_date = serializers.DateField(required=False)
    external_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    evidence_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    receipt_url = serializers.URLField(required=False, allow_blank=True, max_length=200, default="")
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")
    idempotency_key = serializers.CharField(min_length=8, max_length=160, allow_blank=False)


class PayrollSalaryPaymentReversalSerializer(serializers.Serializer):
    correction_reason = serializers.CharField(min_length=5, max_length=500, allow_blank=False)
    payment_date = serializers.DateField(required=False)
    external_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    evidence_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    idempotency_key = serializers.CharField(min_length=8, max_length=160, allow_blank=False)


class PayrollSalaryPaymentSerializer(serializers.ModelSerializer):
    line_id = serializers.IntegerField(read_only=True)
    recorded_by_email = serializers.EmailField(source="recorded_by.email", read_only=True, allow_null=True)
    financial_reference = serializers.CharField(source="financial_transaction.reference", read_only=True, allow_null=True)
    cash_session_reference = serializers.CharField(source="cash_session.reference", read_only=True, allow_null=True)
    reversal_of_reference = serializers.CharField(source="reversal_of.reference", read_only=True, allow_null=True)
    reversed_by_reference = serializers.SerializerMethodField()
    receipt_file_url = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    def get_receipt_file_url(self, obj):
        try:
            return obj.receipt_file.url if obj.receipt_file else ""
        except ValueError:
            return ""

    class Meta:
        model = PayrollSalaryPayment
        fields = [
            "reference", "line_id", "amount", "currency", "payment_date", "method",
            "external_reference", "evidence_reference", "receipt_url", "receipt_file_url", "notes", "recorded_by_email",
            "financial_reference", "cash_session_reference", "reversal_of_reference",
            "reversed_by_reference", "correction_reason", "is_legacy_import", "status", "created_at",
        ]
        read_only_fields = fields

    def get_reversed_by_reference(self, obj):
        reversal = getattr(obj, "reversed_by", None)
        return reversal.reference if reversal else None

    def get_status(self, obj):
        if obj.reversal_of_id:
            return "REVERSAL"
        return "REVERSED" if getattr(obj, "reversed_by", None) else "RECORDED"


class PayrollEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = PayrollEvent
        fields = ["type", "actor_email", "details", "created_at"]
        read_only_fields = fields


class PayrollLineSerializer(serializers.ModelSerializer):
    staff_id = serializers.IntegerField(read_only=True)
    salary_amount_paid = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    salary_balance = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    class Meta:
        model = PayrollLine
        fields = [
            "id", "staff_id", "compensation", "employee_code", "employee_name", "department", "currency",
            "basic_pay", "housing_pay", "transport_pay", "overtime_hours", "overtime_rate", "overtime_pay",
            "allowances", "bonus", "deductions", "gross_pay", "deductions_total", "net_pay",
            "salary_amount_paid", "salary_balance", "created_at",
        ]
        read_only_fields = fields


class MyPayslipSerializer(serializers.ModelSerializer):
    period_reference = serializers.CharField(source="period.reference", read_only=True)
    period_starts_on = serializers.DateField(source="period.starts_on", read_only=True)
    period_ends_on = serializers.DateField(source="period.ends_on", read_only=True)
    period_status = serializers.CharField(source="period.status", read_only=True)
    salary_amount_paid = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    salary_balance = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    class Meta:
        model = PayrollLine
        fields = [
            "id", "period_reference", "period_starts_on", "period_ends_on", "period_status", "currency",
            "basic_pay", "housing_pay", "transport_pay", "overtime_pay", "bonus", "gross_pay",
            "deductions_total", "net_pay", "salary_amount_paid", "salary_balance",
        ]
        read_only_fields = fields


class PayrollPeriodListSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True, allow_null=True)
    approved_by_email = serializers.EmailField(source="approved_by.email", read_only=True, allow_null=True)
    paid_by_email = serializers.EmailField(source="paid_by.email", read_only=True, allow_null=True)
    replaces_reference = serializers.CharField(source="replaces.reference", read_only=True, allow_null=True)
    line_count = serializers.SerializerMethodField()
    excluded_employee_count = serializers.SerializerMethodField()

    def get_line_count(self, obj):
        return obj.lines.filter(staff__is_superuser=False).exclude(staff__role="ADMIN").count()

    def get_excluded_employee_count(self, obj):
        return obj.lines.filter(Q(staff__is_superuser=True) | Q(staff__role="ADMIN")).count()
    salary_amount_paid = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    salary_balance = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    class Meta:
        model = PayrollPeriod
        fields = [
            "id", "reference", "starts_on", "ends_on", "department", "currency",
            "status", "replaces_reference", "line_count", "excluded_employee_count", "total_gross", "total_deductions", "total_net",
            "salary_amount_paid", "salary_balance", "created_by_email", "submitted_at",
            "reviewed_by_email", "reviewed_at", "review_note", "approved_by_email", "approved_at", "paid_by_email",
            "paid_at", "payment_method", "external_reference", "created_at", "updated_at",
        ]
        read_only_fields = fields


class PayrollPeriodDetailSerializer(PayrollPeriodListSerializer):
    lines = serializers.SerializerMethodField()

    def get_lines(self, obj):
        return PayrollLineSerializer(obj.lines.filter(staff__is_superuser=False).exclude(staff__role="ADMIN"), many=True, context=self.context).data
    events = PayrollEventSerializer(many=True, read_only=True)
    accrual_financial_reference = serializers.CharField(source="accrual_transaction.reference", read_only=True, allow_null=True)
    payment_financial_reference = serializers.CharField(source="payment_transaction.reference", read_only=True, allow_null=True)

    class Meta(PayrollPeriodListSerializer.Meta):
        fields = PayrollPeriodListSerializer.Meta.fields + ["accrual_financial_reference", "payment_financial_reference", "lines", "events"]
