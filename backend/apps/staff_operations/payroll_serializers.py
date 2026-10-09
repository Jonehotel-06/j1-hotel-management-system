"""Sensitive payroll serializers; no salary fields enter ordinary staff profile APIs."""
from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from .payroll_models import (
    PayrollEvent, PayrollLine, PayrollPeriod, PayrollSalaryPayment, PayrollStatutoryRuleEvent,
    PayrollStatutoryRuleSet, PayrollTaxIdentity, StaffCompensation,
)


class StaffCompensationCreateSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    effective_from = serializers.DateField()
    currency = serializers.CharField(required=False, allow_blank=False, max_length=3, default="NGN")
    basic_salary = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    housing_allowance = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    transport_allowance = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    overtime_rate = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), default="0.00")
    pension_applicable = serializers.BooleanField(required=False, default=True)
    pension_applicability_note = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    minimum_wage_applicable = serializers.BooleanField(required=False, default=True)
    minimum_wage_applicability_note = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")

    def validate(self, attrs):
        if not attrs["pension_applicable"] and not attrs["pension_applicability_note"].strip():
            raise serializers.ValidationError({"pension_applicability_note": "Record the legal basis for excluding this employee from mandatory pension."})
        if not attrs["minimum_wage_applicable"] and not attrs["minimum_wage_applicability_note"].strip():
            raise serializers.ValidationError({"minimum_wage_applicability_note": "Record why the National Minimum Wage Act exemption applies."})
        return attrs


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
            "pension_applicable", "pension_applicability_note", "minimum_wage_applicable",
            "minimum_wage_applicability_note", "created_by_email", "notes", "created_at",
        ]
        read_only_fields = fields


class PayrollTaxIdentityCreateSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    tax_id = serializers.CharField(min_length=3, max_length=64, trim_whitespace=True)
    effective_from = serializers.DateField()
    registered_on = serializers.DateField()
    evidence_reference = serializers.CharField(min_length=1, max_length=160, allow_blank=False, trim_whitespace=True)
    change_reported_on = serializers.DateField(required=False, allow_null=True)
    change_reason = serializers.CharField(required=False, allow_blank=True, max_length=300)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")

    def validate_tax_id(self, value):
        normalized = value.strip().upper()
        if not normalized or not any(char.isalnum() for char in normalized):
            raise serializers.ValidationError("Enter the employee's actual Nigeria Tax Identification Number.")
        return normalized

    def validate_evidence_reference(self, value):
        if not value.strip():
            raise serializers.ValidationError("Retain an evidence reference for the employee's registered Tax ID.")
        return value.strip()


class PayrollTaxIdentitySerializer(serializers.ModelSerializer):
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    employee_code = serializers.CharField(source="staff.staff_profile.employee_code", read_only=True)
    department = serializers.CharField(source="staff.staff_profile.department", read_only=True)
    tax_id_masked = serializers.SerializerMethodField()
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)

    def get_tax_id_masked(self, obj):
        value = str(obj.tax_id or "")
        return ("•" * max(len(value), 4)) if len(value) <= 4 else ("•" * (len(value) - 4)) + value[-4:]

    class Meta:
        model = PayrollTaxIdentity
        fields = [
            "id", "reference", "staff", "staff_name", "staff_email", "employee_code", "department",
            "tax_id_masked", "effective_from", "registered_on", "evidence_reference", "change_reported_on",
            "change_reason", "created_by_email", "notes", "created_at",
        ]
        read_only_fields = fields


class PayrollComponentInputSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))


class PayrollBenefitInputSerializer(serializers.Serializer):
    """Benefit-in-kind valuation inputs; the server derives taxable monthly values."""

    BENEFIT_TYPES = (
        ("EMPLOYER_OWNED_ASSET", "Employer-owned asset"),
        ("HIRED_ASSET", "Employer-hired asset"),
        ("EMPLOYER_ACCOMMODATION", "Employer-provided accommodation"),
        ("EXEMPT_GENERAL_CANTEEN", "Exempt general canteen meal"),
        ("EXEMPT_UNIFORM_PROTECTIVE_CLOTHING", "Exempt uniform/protective clothing"),
        ("EXEMPT_WORK_TOOLS", "Exempt work tools"),
        ("EXEMPT_RELOCATION", "Exempt relocation expense"),
    )
    benefit_type = serializers.ChoiceField(choices=BENEFIT_TYPES)
    description = serializers.CharField(max_length=160)
    acquisition_or_market_value = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default=Decimal("0.00"))
    annual_rental_value = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default=Decimal("0.00"))
    evidence_reference = serializers.CharField(max_length=160, allow_blank=False)

    def validate(self, attrs):
        benefit_type = attrs["benefit_type"]
        acquisition_value = attrs.get("acquisition_or_market_value", Decimal("0.00"))
        annual_rent = attrs.get("annual_rental_value", Decimal("0.00"))
        if benefit_type == "EMPLOYER_OWNED_ASSET" and acquisition_value <= Decimal("0.00"):
            raise serializers.ValidationError({"acquisition_or_market_value": "Enter the asset acquisition cost or market value."})
        if benefit_type in {"HIRED_ASSET", "EMPLOYER_ACCOMMODATION"} and annual_rent <= Decimal("0.00"):
            raise serializers.ValidationError({"annual_rental_value": "Enter the actual annual rent/rental value."})
        if benefit_type not in {"EMPLOYER_OWNED_ASSET"} and acquisition_value > Decimal("0.00"):
            raise serializers.ValidationError({"acquisition_or_market_value": "This benefit type does not use an owned-asset valuation."})
        if benefit_type not in {"HIRED_ASSET", "EMPLOYER_ACCOMMODATION"} and annual_rent > Decimal("0.00"):
            raise serializers.ValidationError({"annual_rental_value": "This benefit type does not use an annual rental valuation."})
        return attrs


class PayrollTaxClaimsInputSerializer(serializers.Serializer):
    """Actual employee claims; evidence identifiers point to retained source documents."""

    nhf_contribution = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    nhf_evidence_reference = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    nhis_contribution = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    nhis_evidence_reference = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    mortgage_interest = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    mortgage_owner_occupied = serializers.BooleanField(required=False, default=False)
    mortgage_evidence_reference = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    life_insurance_premium = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    life_insurance_paid_year = serializers.IntegerField(min_value=2000, max_value=2100, required=False, allow_null=True)
    life_insurance_evidence_reference = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    rent_payment_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    rent_paid_on = serializers.DateField(required=False, allow_null=True)
    rent_period_start = serializers.DateField(required=False, allow_null=True)
    rent_period_end = serializers.DateField(required=False, allow_null=True)
    rent_tenant_name = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    rent_landlord_name = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    rent_landlord_contact = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    rent_property_address = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    rent_evidence_reference = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        for amount_field, evidence_field, label in (
            ("nhf_contribution", "nhf_evidence_reference", "NHF"),
            ("nhis_contribution", "nhis_evidence_reference", "NHIS"),
            ("mortgage_interest", "mortgage_evidence_reference", "Mortgage interest"),
            ("life_insurance_premium", "life_insurance_evidence_reference", "Life-insurance premium"),
            ("rent_payment_amount", "rent_evidence_reference", "Rent"),
        ):
            if attrs.get(amount_field, Decimal("0.00")) > 0 and not attrs.get(evidence_field, "").strip():
                raise serializers.ValidationError({evidence_field: f"A retained evidence reference is required for a {label} claim."})
        if attrs.get("mortgage_interest", Decimal("0.00")) > 0 and not attrs.get("mortgage_owner_occupied"):
            raise serializers.ValidationError({"mortgage_owner_occupied": "Only interest on an owner-occupied residential property is eligible."})
        if attrs.get("life_insurance_premium", Decimal("0.00")) > 0 and attrs.get("life_insurance_paid_year") is None:
            raise serializers.ValidationError({"life_insurance_paid_year": "Specify the year the premium was actually paid."})
        if attrs.get("rent_payment_amount", Decimal("0.00")) > 0:
            required = {
                "rent_paid_on": "Enter the actual rent payment date.",
                "rent_period_start": "Enter the start of the period covered by the rent.",
                "rent_period_end": "Enter the end of the period covered by the rent.",
                "rent_tenant_name": "Enter the legal tenant's name.",
                "rent_landlord_name": "Enter the landlord's full name.",
                "rent_landlord_contact": "Enter the landlord's mobile number, Tax ID or NIN.",
                "rent_property_address": "Enter the rented property address.",
            }
            errors = {field: message for field, message in required.items() if not attrs.get(field)}
            if errors:
                raise serializers.ValidationError(errors)
            if attrs["rent_period_end"] < attrs["rent_period_start"]:
                raise serializers.ValidationError({"rent_period_end": "Rent coverage must end on or after its start date."})
        return attrs


class PayrollPriorYTDInputSerializer(serializers.Serializer):
    """Opening cumulative values for periods processed outside this system."""

    pay_periods = serializers.IntegerField(min_value=1, max_value=11)
    gross_emoluments = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"))
    cash_emoluments = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False)
    employee_pension = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    nhf_contribution = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    nhis_contribution = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    mortgage_interest = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    life_insurance_premium = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    rent_paid_attributable = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    paye_withheld = serializers.DecimalField(max_digits=16, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    evidence_reference = serializers.CharField(max_length=160, allow_blank=False)


class PayrollAdjustmentInputSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    overtime_hours = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("0.00"), max_value=Decimal("1000.00"), required=False, default="0.00")
    allowances = PayrollComponentInputSerializer(many=True, required=False, default=list, max_length=30)
    bonus = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="0.00")
    deductions = PayrollComponentInputSerializer(many=True, required=False, default=list, max_length=30)
    benefits_in_kind = PayrollBenefitInputSerializer(many=True, required=False, default=list, max_length=30)
    tax_claims = PayrollTaxClaimsInputSerializer(required=False, default=dict)
    prior_ytd = PayrollPriorYTDInputSerializer(required=False, allow_null=True)


class PayrollPeriodCreateSerializer(serializers.Serializer):
    starts_on = serializers.DateField()
    ends_on = serializers.DateField()
    department = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    adjustments = PayrollAdjustmentInputSerializer(many=True, required=False, default=list, max_length=500)
    idempotency_key = serializers.CharField(max_length=128, allow_blank=False)
    replaces_reference = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")


class PayrollStatutoryRuleSetCreateSerializer(serializers.Serializer):
    jurisdiction = serializers.CharField(required=False, max_length=2, default="NG")
    currency = serializers.CharField(required=False, max_length=3, default="NGN")
    effective_from = serializers.DateField()
    paye_bands = serializers.JSONField()
    minimum_wage_monthly = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="70000.00")
    employee_pension_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=Decimal("0.0000"), max_value=Decimal("1.0000"), required=False, default="0.0800")
    employer_pension_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=Decimal("0.0000"), max_value=Decimal("1.0000"), required=False, default="0.1000")
    rent_relief_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=Decimal("0.0000"), max_value=Decimal("1.0000"), required=False, default="0.2000")
    rent_relief_cap = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.00"), required=False, default="500000.00")
    owned_asset_benefit_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=Decimal("0.0000"), max_value=Decimal("1.0000"), required=False, default="0.0500")
    accommodation_income_cap_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=Decimal("0.0000"), max_value=Decimal("1.0000"), required=False, default="0.2000")
    legal_basis = serializers.CharField(min_length=10, max_length=4000)
    source_references = serializers.ListField(child=serializers.CharField(max_length=500), min_length=1, max_length=20)

    def validate_jurisdiction(self, value):
        if value.upper() != "NG":
            raise serializers.ValidationError("Only Nigeria (NG) statutory payroll rules are configured.")
        return value.upper()

    def validate_currency(self, value):
        if value.upper() != "NGN":
            raise serializers.ValidationError("Nigeria statutory rules must use NGN. Configure a separate reviewed FX process for other currencies.")
        return value.upper()

    def validate_paye_bands(self, bands):
        if not isinstance(bands, list) or not bands:
            raise serializers.ValidationError("Provide an ordered list of progressive PAYE bands.")
        normalized = []
        previous = Decimal("0")
        for index, band in enumerate(bands):
            if not isinstance(band, dict) or set(band) != {"up_to", "rate"}:
                raise serializers.ValidationError(f"Band {index + 1} must contain exactly up_to and rate.")
            try:
                rate = Decimal(str(band["rate"]))
                limit = None if band["up_to"] is None else Decimal(str(band["up_to"]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise serializers.ValidationError(f"Band {index + 1} has an invalid limit or rate.") from exc
            if not rate.is_finite() or rate < 0 or rate > 1:
                raise serializers.ValidationError(f"Band {index + 1} rate must be from 0 to 1.")
            if limit is None:
                if index != len(bands) - 1:
                    raise serializers.ValidationError("Only the final band may have a null up_to limit.")
                normalized.append({"up_to": None, "rate": str(rate.quantize(Decimal("0.0001")))})
                continue
            if not limit.is_finite() or limit <= previous:
                raise serializers.ValidationError("PAYE band limits must be finite, positive and strictly increasing.")
            previous = limit
            normalized.append({"up_to": str(limit.quantize(Decimal("0.01"))), "rate": str(rate.quantize(Decimal("0.0001")))})
        if normalized[-1]["up_to"] is not None:
            raise serializers.ValidationError("The final PAYE band must have up_to set to null.")
        return normalized

    def validate_source_references(self, values):
        if any(not value.strip() for value in values):
            raise serializers.ValidationError("Source references cannot be blank.")
        return [value.strip() for value in values]


class PayrollStatutoryRuleSetSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True, allow_null=True)

    class Meta:
        model = PayrollStatutoryRuleSet
        fields = [
            "id", "reference", "jurisdiction", "currency", "effective_from", "paye_bands",
            "minimum_wage_monthly", "employee_pension_rate", "employer_pension_rate", "rent_relief_rate",
            "rent_relief_cap", "owned_asset_benefit_rate", "accommodation_income_cap_rate", "legal_basis", "source_references", "status", "created_by_email",
            "reviewed_by_email", "reviewed_at", "review_note", "created_at",
        ]
        read_only_fields = fields


class PayrollStatutoryRuleEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = PayrollStatutoryRuleEvent
        fields = ["type", "actor_email", "details", "created_at"]
        read_only_fields = fields


class PayrollStatutoryRuleSetDetailSerializer(PayrollStatutoryRuleSetSerializer):
    events = PayrollStatutoryRuleEventSerializer(many=True, read_only=True)

    class Meta(PayrollStatutoryRuleSetSerializer.Meta):
        fields = PayrollStatutoryRuleSetSerializer.Meta.fields + ["events"]


class PayrollStatutoryRuleReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_note = serializers.CharField(min_length=5, max_length=1000, allow_blank=False)


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
    status = serializers.SerializerMethodField()

    class Meta:
        model = PayrollSalaryPayment
        fields = [
            "reference", "line_id", "amount", "currency", "payment_date", "method",
            "external_reference", "evidence_reference", "notes", "recorded_by_email",
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
    tax_identity_reference = serializers.CharField(source="tax_identity.reference", read_only=True, allow_null=True)
    salary_amount_paid = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    salary_balance = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    class Meta:
        model = PayrollLine
        fields = [
            "id", "staff_id", "compensation", "tax_identity_reference", "employee_code", "employee_name", "department", "currency",
            "basic_pay", "housing_pay", "transport_pay", "overtime_hours", "overtime_rate", "overtime_pay",
            "allowances", "bonus", "benefits_in_kind", "taxable_benefits", "taxable_gross_pay", "deductions", "pensionable_pay", "employee_pension", "employer_pension",
            "paye_tax", "nhf_contribution", "nhis_contribution", "mortgage_interest_claim",
            "life_insurance_premium_claim", "rent_paid_attributable", "rent_relief", "tax_claims", "tax_snapshot",
            "gross_pay", "deductions_total", "net_pay", "salary_amount_paid", "salary_balance", "created_at",
        ]
        read_only_fields = fields


class PayrollPeriodListSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True, allow_null=True)
    approved_by_email = serializers.EmailField(source="approved_by.email", read_only=True, allow_null=True)
    paid_by_email = serializers.EmailField(source="paid_by.email", read_only=True, allow_null=True)
    replaces_reference = serializers.CharField(source="replaces.reference", read_only=True, allow_null=True)
    statutory_rules_reference = serializers.CharField(source="statutory_rules.reference", read_only=True, allow_null=True)
    total_employer_cost = serializers.SerializerMethodField()
    line_count = serializers.IntegerField(read_only=True)
    salary_amount_paid = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    salary_balance = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    def get_total_employer_cost(self, obj):
        return str(obj.total_employer_cost)

    class Meta:
        model = PayrollPeriod
        fields = [
            "id", "reference", "starts_on", "ends_on", "department", "currency", "statutory_rules_reference",
            "status", "replaces_reference", "line_count", "total_gross", "total_deductions", "total_net",
            "salary_amount_paid", "salary_balance",
            "total_paye", "total_employer_pension", "total_employer_cost", "created_by_email", "submitted_at",
            "reviewed_by_email", "reviewed_at", "review_note", "approved_by_email", "approved_at", "paid_by_email",
            "paid_at", "payment_method", "external_reference", "created_at", "updated_at",
        ]
        read_only_fields = fields


class PayrollPeriodDetailSerializer(PayrollPeriodListSerializer):
    lines = PayrollLineSerializer(many=True, read_only=True)
    events = PayrollEventSerializer(many=True, read_only=True)
    accrual_financial_reference = serializers.CharField(source="accrual_transaction.reference", read_only=True, allow_null=True)
    payment_financial_reference = serializers.CharField(source="payment_transaction.reference", read_only=True, allow_null=True)

    class Meta(PayrollPeriodListSerializer.Meta):
        fields = PayrollPeriodListSerializer.Meta.fields + ["accrual_financial_reference", "payment_financial_reference", "lines", "events"]
