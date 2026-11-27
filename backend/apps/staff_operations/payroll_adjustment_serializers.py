"""Serializers for salary adjustments (partial month, absence, disciplinary, other).

Inputs carry days, amounts and justification only; every stored figure is
recalculated by ``services.payroll_adjustment_service`` on the server.
"""
from decimal import Decimal

from rest_framework import serializers

from .payroll_models import PayrollSalaryAdjustment, PayrollSalaryAdjustmentEvent


class SalaryAdjustmentInputSerializer(serializers.Serializer):
    """Shared by the preview and the record endpoints."""

    staff_id = serializers.IntegerField(min_value=1)
    payroll_year = serializers.IntegerField(min_value=2000, max_value=2200)
    payroll_month = serializers.IntegerField(min_value=1, max_value=12)
    adjustment_type = serializers.ChoiceField(choices=PayrollSalaryAdjustment.Type.choices)
    calculation_basis = serializers.ChoiceField(
        choices=PayrollSalaryAdjustment.Basis.choices, required=False, default=PayrollSalaryAdjustment.Basis.CALENDAR_DAYS,
    )
    applicable_days = serializers.IntegerField(min_value=1, max_value=31, required=False, allow_null=True, default=None)
    days_worked = serializers.IntegerField(min_value=0, max_value=31, required=False, allow_null=True, default=None)
    absence_days = serializers.IntegerField(min_value=0, max_value=31, required=False, allow_null=True, default=None)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, allow_null=True, default=None)
    other_direction = serializers.ChoiceField(choices=PayrollSalaryAdjustment.Direction.choices, required=False, allow_blank=True, default="")
    additional_payment = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default=Decimal("0.00"))
    additional_payment_reason = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")
    violation_category = serializers.CharField(required=False, allow_blank=True, max_length=60, default="")
    incident_date = serializers.DateField(required=False, allow_null=True, default=None)
    evidence_reference = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")


class SalaryAdjustmentCreateSerializer(SalaryAdjustmentInputSerializer):
    submit = serializers.BooleanField(required=False, default=True)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128, default="")


class SalaryAdjustmentDecisionSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    decision_note = serializers.CharField(max_length=1000, allow_blank=False)


class SalaryAdjustmentReversalSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class SalaryAdjustmentEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = PayrollSalaryAdjustmentEvent
        fields = ["id", "type", "actor_email", "details", "created_at"]
        read_only_fields = fields


class SalaryAdjustmentSerializer(serializers.ModelSerializer):
    staff_id = serializers.IntegerField(read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    employee_code = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    adjustment_type_label = serializers.CharField(source="get_adjustment_type_display", read_only=True)
    calculation_basis_label = serializers.CharField(source="get_calculation_basis_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    month_label = serializers.CharField(read_only=True)
    total_deductions = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    decided_by_email = serializers.EmailField(source="decided_by.email", read_only=True, allow_null=True)
    reversed_by_email = serializers.EmailField(source="reversed_by.email", read_only=True, allow_null=True)
    applied_period_reference = serializers.CharField(source="applied_payroll_line.period.reference", read_only=True, default=None)
    applied_period_status = serializers.CharField(source="applied_payroll_line.period.status", read_only=True, default=None)
    applied_payroll_line_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = PayrollSalaryAdjustment
        fields = [
            "id", "reference", "staff_id", "staff_name", "staff_email", "employee_code", "department",
            "payroll_year", "payroll_month", "month_label", "adjustment_type", "adjustment_type_label",
            "calculation_basis", "calculation_basis_label", "currency",
            "monthly_basic", "monthly_housing", "monthly_transport", "monthly_salary",
            "applicable_days", "days_worked", "absence_days", "daily_rate",
            "prorated_basic", "prorated_housing", "prorated_transport", "prorated_salary",
            "absence_deduction", "disciplinary_deduction", "other_deduction", "total_deductions",
            "additional_payment", "additional_payment_reason", "final_payable",
            "reason", "violation_category", "incident_date", "evidence_reference", "notes",
            "status", "status_label", "created_by_email", "submitted_at", "decided_by_email", "decided_at", "decision_note",
            "applied_payroll_line_id", "applied_period_reference", "applied_period_status", "applied_at", "paid_at",
            "reversed_by_email", "reversed_at", "reversal_reason", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_employee_code(self, obj):
        profile = getattr(obj.staff, "staff_profile", None)
        return getattr(profile, "employee_code", "") if profile else ""

    def get_department(self, obj):
        profile = getattr(obj.staff, "staff_profile", None)
        return getattr(profile, "department", "") if profile else ""


class SalaryAdjustmentDetailSerializer(SalaryAdjustmentSerializer):
    events = SalaryAdjustmentEventSerializer(many=True, read_only=True)
    month_summary = serializers.SerializerMethodField()

    class Meta(SalaryAdjustmentSerializer.Meta):
        fields = SalaryAdjustmentSerializer.Meta.fields + ["events", "month_summary"]
        read_only_fields = fields

    def get_month_summary(self, obj):
        from .services.payroll_adjustment_service import month_summary
        return month_summary(staff_id=obj.staff_id, payroll_year=obj.payroll_year, payroll_month=obj.payroll_month)
