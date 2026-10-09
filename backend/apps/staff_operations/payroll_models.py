"""Payroll records are private, effective-dated and correction-preserving."""
from datetime import date, timedelta
from decimal import Decimal
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel

ZERO = Decimal("0.00")


def _payroll_reference(prefix):
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def _new_payroll_reference():
    return _payroll_reference("PAY")


def _new_rule_reference():
    return _payroll_reference("RULE")


def _new_tax_identity_reference():
    return _payroll_reference("TIN")


def _default_nigeria_2026_bands():
    """Nigeria Tax Act 2025, Fourth Schedule (annual chargeable income)."""
    return [
        {"up_to": "800000.00", "rate": "0.0000"},
        {"up_to": "3000000.00", "rate": "0.1500"},
        {"up_to": "12000000.00", "rate": "0.1800"},
        {"up_to": "25000000.00", "rate": "0.2100"},
        {"up_to": "50000000.00", "rate": "0.2300"},
        {"up_to": None, "rate": "0.2500"},
    ]


class StaffCompensation(models.Model):
    """Append-only effective-dated salary terms; never exposed on staff profiles."""

    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="compensation_terms")
    effective_from = models.DateField(db_index=True)
    currency = models.CharField(max_length=3, default="NGN")
    basic_salary = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    housing_allowance = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    transport_allowance = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    overtime_rate = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    pension_applicable = models.BooleanField(default=True)
    pension_applicability_note = models.CharField(max_length=500, blank=True, default="")
    minimum_wage_applicable = models.BooleanField(default=True)
    minimum_wage_applicability_note = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="compensation_terms_created")
    notes = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["staff_id", "-effective_from", "-pk"]
        constraints = [models.UniqueConstraint(fields=["staff", "effective_from"], name="unique_compensation_effective_date")]
        indexes = [models.Index(fields=["staff", "effective_from"])]

    def clean(self):
        if not self.pension_applicable and not self.pension_applicability_note.strip():
            raise ValidationError({"pension_applicability_note": "Record the legal basis for excluding this employee from mandatory pension."})
        if not self.minimum_wage_applicable and not self.minimum_wage_applicability_note.strip():
            raise ValidationError({"minimum_wage_applicability_note": "Record why the National Minimum Wage Act exemption applies."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Compensation terms are append-only; add a new effective-dated record instead.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Compensation history cannot be deleted.")

    def __str__(self):
        return f"{self.staff_id} · {self.effective_from} · {self.currency}"


class PayrollStatutoryRuleSet(TimeStampedModel):
    """Immutable, effective-dated statutory rules with independent legal review."""

    class Status(models.TextChoices):
        PENDING_REVIEW = "PENDING_REVIEW", "Pending legal/compliance review"
        APPROVED = "APPROVED", "Approved for use"
        REJECTED = "REJECTED", "Rejected"

    reference = models.CharField(max_length=64, unique=True, default=_new_rule_reference, db_index=True)
    jurisdiction = models.CharField(max_length=2, default="NG", db_index=True)
    currency = models.CharField(max_length=3, default="NGN")
    effective_from = models.DateField(db_index=True)
    paye_bands = models.JSONField(default=_default_nigeria_2026_bands)
    minimum_wage_monthly = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("70000.00"), validators=[MinValueValidator(ZERO)])
    employee_pension_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.0800"), validators=[MinValueValidator(Decimal("0.0000"))])
    employer_pension_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.1000"), validators=[MinValueValidator(Decimal("0.0000"))])
    rent_relief_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.2000"), validators=[MinValueValidator(Decimal("0.0000"))])
    rent_relief_cap = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("500000.00"), validators=[MinValueValidator(ZERO)])
    owned_asset_benefit_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.0500"), validators=[MinValueValidator(Decimal("0.0000"))])
    accommodation_income_cap_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.2000"), validators=[MinValueValidator(Decimal("0.0000"))])
    legal_basis = models.TextField()
    source_references = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_REVIEW, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_rule_sets_created")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_rule_sets_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=1000, blank=True, default="")

    class Meta:
        ordering = ["-effective_from", "-created_at", "-pk"]
        indexes = [models.Index(fields=["jurisdiction", "currency", "status", "effective_from"])]

    def clean(self):
        if self.jurisdiction != "NG" or self.currency != "NGN":
            raise ValidationError("This ruleset model currently supports Nigeria (NG) and NGN only.")
        if self.effective_from < date(2026, 1, 1):
            raise ValidationError({"effective_from": "Nigeria statutory rules in this workflow cannot predate 1 January 2026."})
        if not isinstance(self.paye_bands, list) or not self.paye_bands:
            raise ValidationError({"paye_bands": "At least one progressive PAYE band is required."})
        previous_limit = Decimal("0")
        for index, band in enumerate(self.paye_bands):
            if not isinstance(band, dict) or set(band) != {"up_to", "rate"}:
                raise ValidationError({"paye_bands": f"Band {index + 1} must contain only up_to and rate."})
            try:
                rate = Decimal(str(band["rate"]))
                limit = None if band["up_to"] is None else Decimal(str(band["up_to"]))
            except Exception as exc:
                raise ValidationError({"paye_bands": f"Band {index + 1} has an invalid limit or rate."}) from exc
            if rate < 0 or rate > 1:
                raise ValidationError({"paye_bands": f"Band {index + 1} rate must be between 0 and 1."})
            if limit is None:
                if index != len(self.paye_bands) - 1:
                    raise ValidationError({"paye_bands": "Only the final band may have a blank up_to limit."})
            elif limit <= previous_limit:
                raise ValidationError({"paye_bands": "PAYE band limits must be strictly increasing."})
            else:
                previous_limit = limit
        if self.paye_bands[-1].get("up_to") is not None:
            raise ValidationError({"paye_bands": "The final PAYE band must have a blank up_to limit."})
        if not self.legal_basis.strip():
            raise ValidationError({"legal_basis": "A legal basis is required for a statutory ruleset."})
        if not isinstance(self.source_references, list) or not self.source_references or any(not isinstance(item, str) or not item.strip() for item in self.source_references):
            raise ValidationError({"source_references": "Source references must be a non-empty list of non-empty URLs or citations."})

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior:
                changes = {
                    field.attname for field in self._meta.concrete_fields
                    if getattr(prior, field.attname) != getattr(self, field.attname)
                }
                allowed = {"status", "reviewed_by_id", "reviewed_at", "review_note"}
                if prior.status != self.Status.PENDING_REVIEW or not changes.issubset(allowed):
                    raise ValidationError("Reviewed or submitted statutory rules are immutable; create a new effective-dated ruleset.")
                if self.status not in {self.Status.APPROVED, self.Status.REJECTED}:
                    raise ValidationError("A pending statutory ruleset can only be approved or rejected.")
        self.full_clean(exclude=["created_by", "reviewed_by"])
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Statutory payroll rules are retained as legal history and cannot be deleted.")

    def __str__(self):
        return f"{self.reference} · NG/NGN · {self.effective_from} · {self.status}"


class PayrollStatutoryRuleEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created for review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    rule_set = models.ForeignKey(PayrollStatutoryRuleSet, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_rule_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["rule_set", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Statutory-rule events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Statutory-rule events are append-only and cannot be deleted.")


class PayrollTaxIdentity(TimeStampedModel):
    """Confidential, append-only employee TIN history; not part of general staff profiles."""

    reference = models.CharField(max_length=64, unique=True, default=_new_tax_identity_reference, db_index=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payroll_tax_identities")
    tax_id = models.CharField(max_length=64, unique=True)
    effective_from = models.DateField(db_index=True)
    registered_on = models.DateField()
    evidence_reference = models.CharField(max_length=160)
    change_reported_on = models.DateField(null=True, blank=True)
    change_reason = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_tax_identities_created")
    notes = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["staff_id", "-effective_from", "-pk"]
        constraints = [models.UniqueConstraint(fields=["staff", "effective_from"], name="unique_payroll_tax_id_effective_date")]
        indexes = [models.Index(fields=["staff", "effective_from"])]

    def clean(self):
        self.tax_id = (self.tax_id or "").strip().upper()
        self.evidence_reference = (self.evidence_reference or "").strip()
        if not self.tax_id:
            raise ValidationError({"tax_id": "A Nigeria Tax Identification Number is required for statutory payroll."})
        if not self.evidence_reference:
            raise ValidationError({"evidence_reference": "Retain the employee's TIN registration evidence reference."})
        if self.registered_on and self.registered_on > timezone.localdate():
            raise ValidationError({"registered_on": "A Tax ID cannot be recorded as registered on a future date."})
        if self.change_reported_on and self.change_reported_on > timezone.localdate():
            raise ValidationError({"change_reported_on": "A change report date cannot be in the future."})
        prior_exists = bool(
            self.staff_id and self.effective_from
            and type(self).objects.filter(staff_id=self.staff_id, effective_from__lt=self.effective_from).exists()
        )
        if prior_exists and not self.change_reported_on:
            raise ValidationError({"change_reported_on": "A changed Tax ID/particulars must include the date reported to the tax authority."})
        if not prior_exists and self.change_reported_on:
            raise ValidationError({"change_reported_on": "A first registration must not be marked as a change report."})
        if prior_exists and self.change_reported_on:
            if self.change_reported_on < self.effective_from or self.change_reported_on > self.effective_from + timedelta(days=30):
                raise ValidationError({"change_reported_on": "Changes to taxpayer particulars must be reported within 30 days of their effective date."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Tax identity records are append-only; record a new effective-dated change instead.")
        self.full_clean(exclude=["created_by"])
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Tax identity history is retained and cannot be deleted.")

    def __str__(self):
        return f"{self.reference} · staff {self.staff_id} · {self.effective_from}"


class PayrollPeriod(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted for review"
        APPROVED = "APPROVED", "Approved / accrued"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"

    reference = models.CharField(max_length=64, unique=True, default=_new_payroll_reference, db_index=True)
    idempotency_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    idempotency_fingerprint = models.CharField(max_length=64, blank=True, default="")
    starts_on = models.DateField(db_index=True)
    ends_on = models.DateField(db_index=True)
    department = models.CharField(max_length=120, blank=True, default="", db_index=True)
    currency = models.CharField(max_length=3, default="NGN")
    statutory_rules = models.ForeignKey(PayrollStatutoryRuleSet, null=True, blank=True, on_delete=models.PROTECT, related_name="payroll_periods")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    replaces = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replacement_run")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payroll_runs_created")
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_runs_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=1000, blank=True, default="")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_runs_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_runs_paid")
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, blank=True, default="")
    external_reference = models.CharField(max_length=160, blank=True, default="")
    cash_session = models.ForeignKey("finance.CashSession", null=True, blank=True, on_delete=models.PROTECT, related_name="payroll_runs")
    accrual_transaction = models.OneToOneField("finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="payroll_accrual")
    payment_transaction = models.OneToOneField("finance.FinancialTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="payroll_payment")
    total_gross = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    total_deductions = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    total_net = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    total_paye = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    total_employer_pension = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)

    class Meta:
        ordering = ["-starts_on", "-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_on__gte=models.F("starts_on")), name="payroll_period_valid_dates"),
            models.CheckConstraint(condition=Q(total_gross__gte=ZERO) & Q(total_deductions__gte=ZERO) & Q(total_net__gte=ZERO) & Q(total_paye__gte=ZERO) & Q(total_employer_pension__gte=ZERO), name="payroll_totals_nonnegative"),
        ]
        indexes = [models.Index(fields=["status", "starts_on", "ends_on"])]

    @property
    def total_employer_cost(self):
        return (self.total_gross + self.total_employer_pension).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior:
                changes = {
                    field.attname for field in self._meta.concrete_fields
                    if getattr(prior, field.attname) != getattr(self, field.attname)
                }
                allowed = {
                    self.Status.DRAFT: {"status", "submitted_at", "total_gross", "total_deductions", "total_net", "total_paye", "total_employer_pension"},
                    self.Status.SUBMITTED: {
                        "status", "reviewed_by_id", "reviewed_at", "review_note",
                        "approved_by_id", "approved_at", "accrual_transaction_id",
                    },
                    self.Status.APPROVED: {
                        "status", "paid_by_id", "paid_at", "payment_method", "external_reference",
                        "cash_session_id", "payment_transaction_id",
                    },
                }.get(prior.status, set())
                if not changes.issubset(allowed):
                    raise ValidationError("Submitted or finalized payroll evidence cannot be rewritten.")
                if prior.status in {self.Status.REJECTED, self.Status.PAID}:
                    raise ValidationError("Rejected and paid payroll runs are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Payroll run history cannot be deleted.")

    def __str__(self):
        return f"{self.reference} · {self.starts_on}–{self.ends_on} · {self.status}"


class PayrollLine(models.Model):
    """Immutable employee-level payroll snapshot inside one draft/run."""

    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="lines")
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payroll_lines")
    compensation = models.ForeignKey(StaffCompensation, on_delete=models.PROTECT, related_name="payroll_lines")
    tax_identity = models.ForeignKey(PayrollTaxIdentity, null=True, blank=True, on_delete=models.PROTECT, related_name="payroll_lines")
    employee_code = models.CharField(max_length=40, blank=True, default="")
    employee_name = models.CharField(max_length=201)
    department = models.CharField(max_length=120, blank=True, default="")
    currency = models.CharField(max_length=3, default="NGN")
    basic_pay = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    housing_pay = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    transport_pay = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    overtime_hours = models.DecimalField(max_digits=8, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    overtime_rate = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    overtime_pay = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    allowances = models.JSONField(default=list, blank=True)
    bonus = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    benefits_in_kind = models.JSONField(default=list, blank=True)
    taxable_benefits = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    taxable_gross_pay = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    deductions = models.JSONField(default=list, blank=True)
    pensionable_pay = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    employee_pension = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    employer_pension = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    paye_tax = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    nhf_contribution = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    nhis_contribution = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    mortgage_interest_claim = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    life_insurance_premium_claim = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    rent_paid_attributable = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    rent_relief = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    tax_claims = models.JSONField(default=dict, blank=True)
    tax_snapshot = models.JSONField(default=dict, blank=True)
    gross_pay = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    deductions_total = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    net_pay = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["department", "employee_name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["period", "staff"], name="unique_payroll_staff_per_run"),
            models.CheckConstraint(condition=Q(net_pay__lte=models.F("gross_pay")), name="payroll_net_not_above_gross"),
        ]
        indexes = [models.Index(fields=["staff", "period"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Payroll lines are immutable; create a correction run instead.")
        if self.period_id and self.period.status != PayrollPeriod.Status.DRAFT:
            raise ValidationError("Payroll lines can only be added while a run is a draft.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Payroll lines cannot be deleted.")

    def __str__(self):
        return f"{self.period.reference} · {self.employee_code} · {self.net_pay} {self.currency}"


class PayrollEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved / accrued"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    period = models.ForeignKey(PayrollPeriod, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payroll_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["period", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Payroll events are append-only and cannot be updated.")
        from apps.core.request_context import get_request_context
        context = get_request_context()
        self.details = dict(self.details or {})
        if context.get("request_id"):
            self.details.setdefault("request_id", context["request_id"])
        terminal = context.get("terminal")
        if terminal is not None:
            self.details.setdefault("terminal_reference", terminal.reference)
        if self.actor_id:
            self.details.setdefault("actor_role", self.actor.role)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Payroll events are append-only and cannot be deleted.")
