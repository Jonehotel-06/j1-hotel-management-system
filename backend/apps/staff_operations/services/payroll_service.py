"""Locked payroll preparation, maker-checker approval, and ledger settlement."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction

from ..models import (
    PayrollEvent, PayrollLine, PayrollPeriod, PayrollSalaryPayment, PayrollStatutoryRuleEvent,
    PayrollStatutoryRuleSet, PayrollTaxIdentity, StaffCompensation, StaffProfile,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _decimal(value, label, *, maximum=None):
    try:
        amount = Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({label: "A valid two-decimal amount is required."}) from exc
    if amount < ZERO:
        raise ValidationError({label: "Amount must not be negative."})
    if maximum is not None and amount > maximum:
        raise ValidationError({label: "Amount exceeds the supported maximum."})
    return amount


def _hours(value, label):
    try:
        hours = Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({label: "Hours must be a valid non-negative decimal."}) from exc
    if hours < 0 or hours > Decimal("1000.00"):
        raise ValidationError({label: "Hours must be between 0 and 1000."})
    return hours


def _component_rows(rows, label):
    normalized = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            raise ValidationError({label: f"Component {index + 1} must be an object."})
        name = str(row.get("label") or "").strip()
        if not name:
            raise ValidationError({label: f"Component {index + 1} needs a label."})
        amount = _decimal(row.get("amount"), f"{label}[{index}].amount")
        if amount <= ZERO:
            raise ValidationError({label: f"Component {index + 1} amount must be greater than zero."})
        normalized.append({"label": name[:100], "amount": str(amount)})
    return normalized


def _benefit_snapshot(raw_benefits, *, projected_annual_cash, rule_set):
    """Value taxable benefits per the approved annual Nigeria rules; exempt classes remain evidenced but untaxed."""
    taxable_types = {"EMPLOYER_OWNED_ASSET", "HIRED_ASSET", "EMPLOYER_ACCOMMODATION"}
    exempt_types = {
        "EXEMPT_GENERAL_CANTEEN", "EXEMPT_UNIFORM_PROTECTIVE_CLOTHING",
        "EXEMPT_WORK_TOOLS", "EXEMPT_RELOCATION",
    }
    rows = []
    total = ZERO
    if len(raw_benefits or []) > 30:
        raise ValidationError({"benefits_in_kind": "At most 30 benefit records may be supplied per employee and pay period."})
    for index, raw in enumerate(raw_benefits or []):
        if not isinstance(raw, dict):
            raise ValidationError({"benefits_in_kind": f"Benefit {index + 1} must be an object."})
        kind = str(raw.get("benefit_type") or "").strip().upper()
        description = str(raw.get("description") or "").strip()[:160]
        evidence_reference = str(raw.get("evidence_reference") or "").strip()[:160]
        acquisition = _decimal(raw.get("acquisition_or_market_value", ZERO), f"benefits_in_kind[{index}].acquisition_or_market_value")
        annual_rent = _decimal(raw.get("annual_rental_value", ZERO), f"benefits_in_kind[{index}].annual_rental_value")
        if kind not in taxable_types | exempt_types or not description or not evidence_reference:
            raise ValidationError({"benefits_in_kind": f"Benefit {index + 1} needs a supported type, description and evidence reference."})
        if kind == "EMPLOYER_OWNED_ASSET":
            if acquisition <= ZERO or annual_rent > ZERO:
                raise ValidationError({"benefits_in_kind": "Owned assets require acquisition/market value and cannot use annual rental value."})
            annual_taxable = (acquisition * Decimal(rule_set.owned_asset_benefit_rate)).quantize(CENT, rounding=ROUND_HALF_UP)
        elif kind in {"HIRED_ASSET", "EMPLOYER_ACCOMMODATION"}:
            if annual_rent <= ZERO or acquisition > ZERO:
                raise ValidationError({"benefits_in_kind": "Hired assets/accommodation require annual rental value and cannot use owned-asset value."})
            annual_taxable = annual_rent
            if kind == "EMPLOYER_ACCOMMODATION":
                accommodation_cap = (Decimal(projected_annual_cash) * Decimal(rule_set.accommodation_income_cap_rate)).quantize(CENT, rounding=ROUND_HALF_UP)
                annual_taxable = min(annual_rent, accommodation_cap)
        else:
            if acquisition > ZERO or annual_rent > ZERO:
                raise ValidationError({"benefits_in_kind": "Statutorily exempt canteen, uniform, work-tool and relocation records cannot carry a taxable valuation."})
            annual_taxable = ZERO
        period_taxable = (annual_taxable / Decimal("12")).quantize(CENT, rounding=ROUND_HALF_UP)
        total += period_taxable
        rows.append({
            "benefit_type": kind,
            "description": description,
            "acquisition_or_market_value": str(acquisition),
            "annual_rental_value": str(annual_rent),
            "annual_taxable_value": str(annual_taxable),
            "period_taxable_value": str(period_taxable),
            "evidence_reference": evidence_reference,
        })
    return rows, total.quantize(CENT)


def _components_total(rows):
    return sum((Decimal(row["amount"]) for row in rows), ZERO).quantize(CENT)


def progressive_tax(chargeable_income, bands):
    """Apply annual progressive tax bands without a minimum-tax floor."""
    income = max(Decimal(str(chargeable_income or ZERO)), ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
    previous_limit = ZERO
    tax = ZERO
    breakdown = []
    for band in bands:
        upper = None if band.get("up_to") is None else Decimal(str(band["up_to"]))
        rate = Decimal(str(band["rate"]))
        taxable_in_band = max(income - previous_limit, ZERO)
        if upper is not None:
            taxable_in_band = min(taxable_in_band, upper - previous_limit)
        band_tax = (taxable_in_band * rate).quantize(CENT, rounding=ROUND_HALF_UP)
        tax += band_tax
        breakdown.append({
            "from": str(previous_limit.quantize(CENT)),
            "up_to": str(upper.quantize(CENT)) if upper is not None else None,
            "rate": str(rate),
            "taxable_income": str(taxable_in_band.quantize(CENT)),
            "tax": str(band_tax),
        })
        if upper is None or income <= upper:
            break
        previous_limit = upper
    return tax.quantize(CENT, rounding=ROUND_HALF_UP), breakdown


def _rent_attributable_to_year(*, amount, covered_from, covered_to, tax_year):
    """Prorate actual rent across covered calendar days, as required by JRB guidance."""
    year_start, year_end = date(tax_year, 1, 1), date(tax_year, 12, 31)
    overlap_start = max(covered_from, year_start)
    overlap_end = min(covered_to, year_end)
    if overlap_end < overlap_start:
        return ZERO
    total_days = Decimal((covered_to - covered_from).days + 1)
    eligible_days = Decimal((overlap_end - overlap_start).days + 1)
    return (Decimal(amount) * eligible_days / total_days).quantize(CENT, rounding=ROUND_HALF_UP)


def _claims_snapshot(raw_claims, *, tax_year, rule_set):
    claims = dict(raw_claims or {})
    life_premium = _decimal(claims.get("life_insurance_premium", ZERO), "life_insurance_premium")
    life_paid_year = claims.get("life_insurance_paid_year")
    if life_premium > ZERO and int(life_paid_year or 0) != tax_year - 1:
        raise ValidationError({"life_insurance_paid_year": f"Life-insurance premiums for tax year {tax_year} must have been paid in {tax_year - 1}."})
    rent_amount = _decimal(claims.get("rent_payment_amount", ZERO), "rent_payment_amount")
    rent_paid_on = claims.get("rent_paid_on")
    rent_from = claims.get("rent_period_start")
    rent_to = claims.get("rent_period_end")
    rent_attributable = ZERO
    if rent_amount > ZERO:
        if rent_paid_on is None or rent_from is None or rent_to is None:
            raise ValidationError({"tax_claims": "Rent relief requires the payment date and the full coverage period."})
        if rent_paid_on.year != tax_year:
            raise ValidationError({"rent_paid_on": f"Only rent actually paid in tax year {tax_year} can be claimed for that year."})
        if rent_to < rent_from:
            raise ValidationError({"rent_period_end": "Rent coverage must end on or after its start date."})
        rent_attributable = _rent_attributable_to_year(
            amount=rent_amount, covered_from=rent_from, covered_to=rent_to, tax_year=tax_year,
        )
    return {
        "nhf_contribution": _decimal(claims.get("nhf_contribution", ZERO), "nhf_contribution"),
        "nhf_evidence_reference": str(claims.get("nhf_evidence_reference") or "").strip()[:160],
        "nhis_contribution": _decimal(claims.get("nhis_contribution", ZERO), "nhis_contribution"),
        "nhis_evidence_reference": str(claims.get("nhis_evidence_reference") or "").strip()[:160],
        "mortgage_interest": _decimal(claims.get("mortgage_interest", ZERO), "mortgage_interest"),
        "mortgage_owner_occupied": bool(claims.get("mortgage_owner_occupied", False)),
        "mortgage_evidence_reference": str(claims.get("mortgage_evidence_reference") or "").strip()[:160],
        "life_insurance_premium": life_premium,
        "life_insurance_paid_year": int(life_paid_year) if life_paid_year else None,
        "life_insurance_evidence_reference": str(claims.get("life_insurance_evidence_reference") or "").strip()[:160],
        "rent_payment_amount": rent_amount,
        "rent_paid_on": rent_paid_on.isoformat() if rent_paid_on else "",
        "rent_period_start": rent_from.isoformat() if rent_from else "",
        "rent_period_end": rent_to.isoformat() if rent_to else "",
        "rent_tenant_name": str(claims.get("rent_tenant_name") or "").strip()[:160],
        "rent_landlord_name": str(claims.get("rent_landlord_name") or "").strip()[:160],
        "rent_landlord_contact": str(claims.get("rent_landlord_contact") or "").strip()[:160],
        "rent_property_address": str(claims.get("rent_property_address") or "").strip()[:300],
        "rent_evidence_reference": str(claims.get("rent_evidence_reference") or "").strip()[:160],
        "rent_paid_attributable": rent_attributable,
        "rent_relief": min(
            (rent_attributable * Decimal(rule_set.rent_relief_rate)).quantize(CENT, rounding=ROUND_HALF_UP),
            Decimal(rule_set.rent_relief_cap),
        ),
    }


def _snapshot_amount(snapshot, key):
    try:
        return Decimal(str((snapshot or {}).get(key, ZERO))).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("A prior payroll tax snapshot is invalid; correct it through a documented replacement run.")


def _rule_event(rule_set, event_type, actor, details=None):
    return PayrollStatutoryRuleEvent.objects.create(
        rule_set=rule_set, type=event_type, actor=actor, details=dict(details or {}),
    )


def _event(period, event_type, actor, details=None):
    return PayrollEvent.objects.create(
        period=period,
        type=event_type,
        actor=actor,
        details=dict(details or {}),
    )


@transaction.atomic
def create_statutory_rule_set(*, actor, **values):
    if not has_capability(actor, "payroll.rules.manage"):
        raise PermissionDenied("This user cannot propose statutory payroll rules.")
    values = dict(values)
    values["jurisdiction"] = str(values.get("jurisdiction") or "NG").upper()
    values["currency"] = str(values.get("currency") or "NGN").upper()
    rule_set = PayrollStatutoryRuleSet(
        **values, created_by=actor, status=PayrollStatutoryRuleSet.Status.PENDING_REVIEW,
    )
    rule_set.full_clean(exclude=["created_by", "reviewed_by"])
    rule_set.save()
    _rule_event(rule_set, PayrollStatutoryRuleEvent.Type.CREATED, actor, {
        "effective_from": rule_set.effective_from.isoformat(),
        "jurisdiction": rule_set.jurisdiction,
        "currency": rule_set.currency,
        "legal_basis": rule_set.legal_basis[:1000],
        "source_references": rule_set.source_references,
    })
    return rule_set


@transaction.atomic
def review_statutory_rule_set(*, rule_set, reviewer, approved: bool, review_note=""):
    if not has_capability(reviewer, "payroll.rules.review"):
        raise PermissionDenied("This user cannot review statutory payroll rules.")
    rule_set = PayrollStatutoryRuleSet.objects.select_for_update().get(pk=rule_set.pk)
    target_status = PayrollStatutoryRuleSet.Status.APPROVED if approved else PayrollStatutoryRuleSet.Status.REJECTED
    if rule_set.status == target_status:
        return rule_set
    if rule_set.status != PayrollStatutoryRuleSet.Status.PENDING_REVIEW:
        raise ValidationError("A reviewed statutory ruleset is immutable; submit a new effective-dated version.")
    if rule_set.created_by_id and rule_set.created_by_id == reviewer.pk:
        raise PermissionDenied("The proposer cannot legally/compliance-review their own statutory ruleset.")
    note = str(review_note or "").strip()
    if len(note) < 5:
        raise ValidationError({"review_note": "Record a substantive legal/compliance review note."})
    if approved:
        same_date = PayrollStatutoryRuleSet.objects.select_for_update().filter(
            jurisdiction=rule_set.jurisdiction, currency=rule_set.currency,
            effective_from=rule_set.effective_from, status=PayrollStatutoryRuleSet.Status.APPROVED,
        ).exclude(pk=rule_set.pk).exists()
        if same_date:
            raise ValidationError("An approved ruleset already exists for this effective date; use a later effective date for the next version.")
        later_version = PayrollStatutoryRuleSet.objects.select_for_update().filter(
            jurisdiction=rule_set.jurisdiction, currency=rule_set.currency,
            effective_from__gt=rule_set.effective_from, status=PayrollStatutoryRuleSet.Status.APPROVED,
        ).exists()
        if later_version:
            raise ValidationError("Rulesets must be approved in effective-date order; do not introduce an unreviewed retroactive rule.")
    rule_set.status = target_status
    rule_set.reviewed_by = reviewer
    rule_set.reviewed_at = timezone.now()
    rule_set.review_note = note[:1000]
    rule_set.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    _rule_event(rule_set, PayrollStatutoryRuleEvent.Type.APPROVED if approved else PayrollStatutoryRuleEvent.Type.REJECTED, reviewer, {
        "review_note": rule_set.review_note,
        "effective_from": rule_set.effective_from.isoformat(),
    })
    return rule_set


@transaction.atomic
def create_payroll_tax_identity(
    *, staff, actor, tax_id, effective_from, registered_on, evidence_reference,
    change_reported_on=None, change_reason="", notes="",
):
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot manage confidential payroll tax identity records.")
    staff = User.objects.select_for_update().filter(pk=staff.pk).first()
    if staff is None or not staff.is_active or not staff.is_staff_member:
        raise ValidationError("An active staff account is required for a payroll Tax ID.")
    if not StaffProfile.objects.filter(user=staff).exists():
        from .staff_operations_service import ensure_staff_profile
        ensure_staff_profile(staff=staff)
    if not str(tax_id or "").strip() or not str(evidence_reference or "").strip():
        raise ValidationError({"tax_id": "A Tax ID and retained registration evidence reference are required."})
    prior = PayrollTaxIdentity.objects.select_for_update().filter(staff=staff).order_by("-effective_from", "-pk").first()
    if prior:
        if effective_from <= prior.effective_from:
            raise ValidationError({"effective_from": "Tax identity changes must be appended with a later effective date."})
        if change_reported_on is None:
            raise ValidationError({"change_reported_on": "Changes to taxpayer particulars must record the date reported to the tax authority."})
        if change_reported_on < effective_from or change_reported_on > effective_from + timedelta(days=30):
            raise ValidationError({"change_reported_on": "Changes to taxpayer particulars must be reported within 30 days of their effective date."})
        if change_reported_on > hotel_today():
            raise ValidationError({"change_reported_on": "A change report date cannot be in the future."})
        if not str(change_reason or "").strip():
            raise ValidationError({"change_reason": "Describe the taxpayer particular that changed."})
    elif change_reported_on is not None:
        raise ValidationError({"change_reported_on": "A first registration is not a changed-particulars report."})
    identity = PayrollTaxIdentity(
        staff=staff, tax_id=str(tax_id).strip().upper()[:64], effective_from=effective_from,
        registered_on=registered_on, evidence_reference=str(evidence_reference).strip()[:160],
        change_reported_on=change_reported_on, change_reason=str(change_reason or "").strip()[:300],
        created_by=actor, notes=str(notes or "").strip()[:500],
    )
    try:
        identity.save()
    except IntegrityError as exc:
        raise ValidationError({"tax_id": "This Tax ID or effective-dated staff record already exists."}) from exc
    return identity


@transaction.atomic
def create_compensation(
    *, staff, actor, effective_from, currency, basic_salary, housing_allowance=ZERO,
    transport_allowance=ZERO, overtime_rate=ZERO, pension_applicable=True,
    pension_applicability_note="", minimum_wage_applicable=True,
    minimum_wage_applicability_note="", notes="",
):
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot manage compensation records.")
    staff = User.objects.select_for_update().filter(pk=staff.pk).first()
    if staff is None or not staff.is_active or not staff.is_staff_member:
        raise ValidationError("An active staff account is required for compensation.")
    if effective_from < hotel_today():
        raise ValidationError({"effective_from": "New compensation terms must be effective today or in the future."})
    currency = str(currency or "NGN").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError({"currency": "Use a three-letter ISO currency code."})
    basic_salary = _decimal(basic_salary, "basic_salary")
    if basic_salary <= ZERO:
        raise ValidationError({"basic_salary": "Basic salary must be greater than zero."})
    housing_allowance = _decimal(housing_allowance, "housing_allowance")
    transport_allowance = _decimal(transport_allowance, "transport_allowance")
    overtime_rate = _decimal(overtime_rate, "overtime_rate")
    if not StaffProfile.objects.filter(user=staff).exists():
        from .staff_operations_service import ensure_staff_profile
        ensure_staff_profile(staff=staff)
    compensation = StaffCompensation(
        staff=staff,
        effective_from=effective_from,
        currency=currency,
        basic_salary=basic_salary,
        housing_allowance=housing_allowance,
        transport_allowance=transport_allowance,
        overtime_rate=overtime_rate,
        pension_applicable=bool(pension_applicable),
        pension_applicability_note=(pension_applicability_note or "")[:500],
        minimum_wage_applicable=bool(minimum_wage_applicable),
        minimum_wage_applicability_note=(minimum_wage_applicability_note or "")[:500],
        created_by=actor,
        notes=(notes or "")[:500],
    )
    compensation.full_clean(exclude=["created_by"])
    try:
        compensation.save()
    except IntegrityError as exc:
        raise ValidationError({"effective_from": "Compensation already exists for this staff member on that date."}) from exc
    return compensation


@transaction.atomic
def create_payroll_period(
    *, actor, starts_on, ends_on, department="", adjustments=None, idempotency_key="", replaces=None,
):
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot prepare payroll.")
    key = str(idempotency_key or "").strip() or None
    if not key:
        raise ValidationError({"idempotency_key": "A payroll idempotency key is required."})
    fingerprint_payload = {
        "starts_on": starts_on.isoformat(),
        "ends_on": ends_on.isoformat(),
        "department": (department or "").strip(),
        "adjustments": adjustments or [],
        "replaces": getattr(replaces, "reference", ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    existing = PayrollPeriod.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        if existing.idempotency_fingerprint != fingerprint:
            raise ValidationError({"idempotency_key": "This key was already used for a different payroll request."})
        return existing, False
    if ends_on < starts_on:
        raise ValidationError({"ends_on": "End date must be on or after start date."})
    if (ends_on - starts_on).days > 62:
        raise ValidationError("A payroll run cannot cover more than 63 inclusive days.")
    if starts_on > hotel_today():
        raise ValidationError({"starts_on": "A payroll run cannot start in the future."})
    if ends_on >= date(2026, 1, 1) and (starts_on.year, starts_on.month) != (ends_on.year, ends_on.month):
        raise ValidationError({"starts_on": "A payroll run effective under the 2026 Nigeria rules must stay within one calendar month."})
    if replaces is not None:
        replaces = PayrollPeriod.objects.select_for_update().filter(pk=replaces.pk).first()
        if replaces is None or replaces.status != PayrollPeriod.Status.REJECTED:
            raise ValidationError({"replaces_reference": "Only a rejected payroll run can be replaced."})
        if (replaces.starts_on, replaces.ends_on, replaces.department) != (
            starts_on, ends_on, (department or "")[:120]
        ):
            raise ValidationError("A replacement must use the same dates and department as the rejected run.")
        if hasattr(replaces, "replacement_run"):
            raise ValidationError("This rejected payroll run already has a replacement.")

    eligible_profiles = list(
        StaffProfile.objects.select_for_update()
        .select_related("user")
        .filter(
            user__is_active=True,
            employment_status__in=[StaffProfile.EmploymentStatus.ACTIVE, StaffProfile.EmploymentStatus.ON_LEAVE],
        )
        .exclude(user__role=User.Role.GUEST)
        .order_by("user__last_name", "user__first_name", "user_id")
    )
    if department:
        eligible_profiles = [p for p in eligible_profiles if p.department.casefold() == department.strip().casefold()]
    if not eligible_profiles:
        raise ValidationError("No active staff profiles match this payroll run.")

    profile_by_user = {profile.user_id: profile for profile in eligible_profiles}
    user_ids = list(profile_by_user)
    compensation_rows = list(
        StaffCompensation.objects.select_for_update()
        .filter(staff_id__in=user_ids, effective_from__lte=starts_on)
        .select_related("staff")
        .order_by("staff_id", "-effective_from", "-pk")
    )
    compensation_by_user = {}
    for row in compensation_rows:
        compensation_by_user.setdefault(row.staff_id, row)

    missing = [profile.employee_code for profile in eligible_profiles if profile.user_id not in compensation_by_user]
    if missing:
        raise ValidationError({"staff": "Compensation is missing for: " + ", ".join(missing[:30])})

    adjustments_by_staff = {}
    for row in adjustments or []:
        staff_id = int(row.get("staff_id") or 0)
        if staff_id not in profile_by_user:
            raise ValidationError({"adjustments": f"Staff account {staff_id} is not eligible for this payroll run."})
        if staff_id in adjustments_by_staff:
            raise ValidationError({"adjustments": f"Staff account {staff_id} appears more than once."})
        adjustments_by_staff[staff_id] = row

    selected_compensation = [compensation_by_user[uid] for uid in user_ids]
    currencies = {row.currency.upper() for row in selected_compensation}
    if len(currencies) != 1:
        raise ValidationError("A payroll run must use one currency; separate different currencies into separate runs.")
    currency = next(iter(currencies))
    if replaces is not None and replaces.currency != currency:
        raise ValidationError("A replacement payroll run must use the original run's currency.")

    statutory_rules = None
    statutory_effective = starts_on >= date(2026, 1, 1)
    if statutory_effective:
        if (starts_on.year, starts_on.month) != (ends_on.year, ends_on.month):
            raise ValidationError({"starts_on": "A 2026 Nigeria statutory payroll run must stay within one calendar month."})
        if currency != "NGN":
            raise ValidationError({"currency": "Nigeria statutory payroll calculations require NGN; no foreign-exchange conversion provider is configured."})
        statutory_rules = PayrollStatutoryRuleSet.objects.select_for_update().filter(
            jurisdiction="NG", currency="NGN", status=PayrollStatutoryRuleSet.Status.APPROVED,
            effective_from__lte=starts_on,
        ).order_by("-effective_from", "-pk").first()
        if statutory_rules is None:
            pending = PayrollStatutoryRuleSet.objects.filter(
                jurisdiction="NG", currency="NGN", status=PayrollStatutoryRuleSet.Status.PENDING_REVIEW,
                effective_from__lte=starts_on,
            ).exists()
            reason = "The effective statutory rules are awaiting independent legal/compliance review." if pending else "No approved effective-dated Nigeria statutory rules are configured."
            raise ValidationError({"statutory_rules": reason + " Review/approve a ruleset before preparing this payroll."})

    tax_identity_by_user = {}
    if statutory_rules:
        for staff_id in user_ids:
            identity = PayrollTaxIdentity.objects.select_for_update().filter(
                staff_id=staff_id, effective_from__lte=starts_on,
            ).order_by("-effective_from", "-pk").first()
            if identity is None:
                raise ValidationError({"tax_id": f"A registered Nigeria Tax ID is required for {profile_by_user[staff_id].employee_code} before statutory payroll can be prepared."})
            tax_identity_by_user[staff_id] = identity

    for compensation in selected_compensation:
        changed_during_period = StaffCompensation.objects.filter(
            staff_id=compensation.staff_id,
            effective_from__gt=starts_on,
            effective_from__lte=ends_on,
        ).exists()
        if changed_during_period:
            raise ValidationError(
                {"starts_on": f"Compensation for {profile_by_user[compensation.staff_id].employee_code} changes during the period; split the run at the effective date."}
            )

    tax_year = ends_on.year
    if statutory_rules:
        active_statuses = [
            PayrollPeriod.Status.DRAFT, PayrollPeriod.Status.SUBMITTED,
            PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.PARTIALLY_PAID, PayrollPeriod.Status.PAID,
        ]
        for staff_id in user_ids:
            same_month = PayrollLine.objects.select_for_update().filter(
                staff_id=staff_id, period__ends_on__year=tax_year, period__ends_on__month=ends_on.month,
                period__status__in=active_statuses,
            ).exists()
            if same_month:
                raise ValidationError({"starts_on": f"A non-rejected payroll run already exists for {profile_by_user[staff_id].employee_code} in {tax_year}-{ends_on.month:02d}. Use the existing run or its documented rejected-run replacement."})
            earlier_lines = PayrollLine.objects.select_for_update().filter(
                staff_id=staff_id, period__ends_on__year=tax_year, period__ends_on__lt=starts_on,
            )
            if earlier_lines.filter(period__status__in=[PayrollPeriod.Status.DRAFT, PayrollPeriod.Status.SUBMITTED]).exists():
                raise ValidationError({"starts_on": f"A prior payroll run for {profile_by_user[staff_id].employee_code} is not yet finalized; complete or correct it before preparing this month."})
            if earlier_lines.filter(period__status=PayrollPeriod.Status.REJECTED, period__replacement_run__isnull=True).exists():
                raise ValidationError({"starts_on": f"A rejected prior-period run for {profile_by_user[staff_id].employee_code} must be replaced before the next statutory payroll."})

    try:
        with transaction.atomic():
            period = PayrollPeriod.objects.create(
                idempotency_key=key,
                idempotency_fingerprint=fingerprint,
                starts_on=starts_on,
                ends_on=ends_on,
                department=(department or "")[:120],
                currency=currency,
                statutory_rules=statutory_rules,
                created_by=actor,
                replaces=replaces,
            )
    except IntegrityError:
        existing = PayrollPeriod.objects.select_for_update().filter(idempotency_key=key).first()
        if existing:
            if existing.idempotency_fingerprint != fingerprint:
                raise ValidationError({"idempotency_key": "This key was already used for a different payroll request."})
            return existing, False
        raise
    totals = {"gross": ZERO, "deductions": ZERO, "net": ZERO, "paye": ZERO, "employer_pension": ZERO}
    for staff_id in user_ids:
        profile = profile_by_user[staff_id]
        compensation = compensation_by_user[staff_id]
        tax_identity = tax_identity_by_user.get(staff_id)
        inputs = adjustments_by_staff.get(staff_id, {})
        hours = _hours(inputs.get("overtime_hours", 0), "overtime_hours")
        overtime_rate = Decimal(compensation.overtime_rate).quantize(CENT)
        overtime_pay = (hours * overtime_rate).quantize(CENT, rounding=ROUND_HALF_UP)
        allowances = _component_rows(inputs.get("allowances", []), "allowances")
        manual_deductions = _component_rows(inputs.get("deductions", []), "deductions")
        bonus = _decimal(inputs.get("bonus", 0), "bonus")
        basic = Decimal(compensation.basic_salary).quantize(CENT)
        # Preserve the pre-2026 manual payroll contract; housing/transport terms
        # enter statutory emoluments only when an approved NG ruleset applies.
        housing = Decimal(compensation.housing_allowance).quantize(CENT) if statutory_rules else ZERO
        transport = Decimal(compensation.transport_allowance).quantize(CENT) if statutory_rules else ZERO
        gross = (basic + housing + transport + overtime_pay + _components_total(allowances) + bonus).quantize(CENT)
        employee_pension = employer_pension = pensionable_pay = paye_tax = ZERO
        nhf = nhis = mortgage_interest = life_premium = rent_paid_attributable = rent_relief = ZERO
        claims_snapshot = {}
        tax_snapshot = {}
        benefits_snapshot = []
        taxable_benefits = ZERO
        taxable_gross_pay = gross
        deductions = list(manual_deductions)

        if statutory_rules:
            reserved_labels = {"employee pension", "pension", "paye", "pay as you earn", "nhf", "nhf contribution", "nhis", "nhis contribution"}
            if any(str(row["label"]).strip().casefold() in reserved_labels for row in manual_deductions):
                raise ValidationError({"deductions": "Do not enter PAYE, pension, NHF or NHIS in manual deductions; the statutory calculation records those separately."})
            claims_snapshot = _claims_snapshot(inputs.get("tax_claims", {}), tax_year=tax_year, rule_set=statutory_rules)
            nhf = claims_snapshot["nhf_contribution"]
            nhis = claims_snapshot["nhis_contribution"]
            mortgage_interest = claims_snapshot["mortgage_interest"]
            life_premium = claims_snapshot["life_insurance_premium"]
            rent_paid_attributable = claims_snapshot["rent_paid_attributable"]
            if mortgage_interest > ZERO and not claims_snapshot["mortgage_owner_occupied"]:
                raise ValidationError({"tax_claims": "Owner-occupied mortgage interest is required for this tax deduction."})
            if not claims_snapshot["mortgage_evidence_reference"] and mortgage_interest > ZERO:
                raise ValidationError({"tax_claims": "Mortgage-interest claims need a retained evidence reference."})
            if not claims_snapshot["nhf_evidence_reference"] and nhf > ZERO:
                raise ValidationError({"tax_claims": "NHF claims need evidence that the actual contribution was made/remitted."})
            if not claims_snapshot["nhis_evidence_reference"] and nhis > ZERO:
                raise ValidationError({"tax_claims": "NHIS claims need evidence that the actual contribution was deducted/remitted."})
            if not claims_snapshot["life_insurance_evidence_reference"] and life_premium > ZERO:
                raise ValidationError({"tax_claims": "Life-insurance claims need a retained evidence reference."})
            if not claims_snapshot["rent_evidence_reference"] and claims_snapshot["rent_payment_amount"] > ZERO:
                raise ValidationError({"tax_claims": "Rent claims need a retained evidence reference."})

            pensionable_pay = (basic + housing + transport).quantize(CENT)
            if compensation.pension_applicable:
                employee_pension = (pensionable_pay * Decimal(statutory_rules.employee_pension_rate)).quantize(CENT, rounding=ROUND_HALF_UP)
                employer_pension = (pensionable_pay * Decimal(statutory_rules.employer_pension_rate)).quantize(CENT, rounding=ROUND_HALF_UP)

            prior_lines = list(PayrollLine.objects.select_for_update().filter(
                staff_id=staff_id, period__ends_on__year=tax_year,
                period__ends_on__lt=starts_on,
                period__status__in=[
                    PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.PARTIALLY_PAID, PayrollPeriod.Status.PAID,
                ],
            ).select_related("period").order_by("period__ends_on", "pk"))
            opening = inputs.get("prior_ytd")
            if prior_lines and opening:
                raise ValidationError({"prior_ytd": f"An opening YTD balance cannot be added after payroll history exists for {profile.employee_code}."})
            if prior_lines:
                prior_snapshot = prior_lines[-1].tax_snapshot or {}
                prior_periods = int(prior_snapshot.get("pay_periods_ytd", len(prior_lines)))
                prior_gross = _snapshot_amount(prior_snapshot, "gross_emoluments_ytd")
                prior_cash_gross = _snapshot_amount(prior_snapshot, "cash_emoluments_ytd") if "cash_emoluments_ytd" in prior_snapshot else prior_gross
                prior_pension = _snapshot_amount(prior_snapshot, "employee_pension_ytd")
                prior_nhf = _snapshot_amount(prior_snapshot, "nhf_contribution_ytd")
                prior_nhis = _snapshot_amount(prior_snapshot, "nhis_contribution_ytd")
                prior_mortgage = _snapshot_amount(prior_snapshot, "mortgage_interest_ytd")
                prior_life = _snapshot_amount(prior_snapshot, "life_insurance_premium_ytd")
                prior_rent = _snapshot_amount(prior_snapshot, "rent_paid_attributable_ytd")
                prior_paye = _snapshot_amount(prior_snapshot, "paye_withheld_ytd")
            else:
                prior_periods = 0
                prior_gross = prior_cash_gross = prior_pension = prior_nhf = prior_nhis = ZERO
                prior_mortgage = prior_life = prior_rent = prior_paye = ZERO
                if opening:
                    if not str(opening.get("evidence_reference") or "").strip():
                        raise ValidationError({"prior_ytd": "Opening YTD totals require a retained evidence reference."})
                    prior_periods = int(opening["pay_periods"])
                    if prior_periods >= ends_on.month:
                        raise ValidationError({"prior_ytd": "Opening YTD pay-period count must be less than the current calendar month."})
                    prior_gross = Decimal(opening["gross_emoluments"]).quantize(CENT)
                    prior_cash_gross = Decimal(opening.get("cash_emoluments", opening["gross_emoluments"])).quantize(CENT)
                    prior_pension = Decimal(opening["employee_pension"]).quantize(CENT)
                    prior_nhf = Decimal(opening["nhf_contribution"]).quantize(CENT)
                    prior_nhis = Decimal(opening["nhis_contribution"]).quantize(CENT)
                    prior_mortgage = Decimal(opening["mortgage_interest"]).quantize(CENT)
                    prior_life = Decimal(opening["life_insurance_premium"]).quantize(CENT)
                    prior_rent = Decimal(opening["rent_paid_attributable"]).quantize(CENT)
                    prior_paye = Decimal(opening["paye_withheld"]).quantize(CENT)
                else:
                    start_dates = [compensation.effective_from]
                    if profile.employment_start:
                        start_dates.append(profile.employment_start)
                    earliest_start = min(start_dates)
                    first_employment_month = 1 if earliest_start.year < tax_year else earliest_start.month
                    if ends_on.month > first_employment_month:
                        raise ValidationError({"prior_ytd": f"Verified opening YTD totals from earlier 2026 payroll are required for {profile.employee_code}; attach an evidence reference and prior period count."})
            if prior_cash_gross > prior_gross:
                raise ValidationError({"prior_ytd": f"Prior cash emoluments cannot exceed prior gross emoluments for {profile.employee_code}."})
            prior_rent_relief = min(
                (prior_rent * Decimal(statutory_rules.rent_relief_rate)).quantize(CENT, rounding=ROUND_HALF_UP),
                Decimal(statutory_rules.rent_relief_cap),
            )
            prior_claims_total = (prior_pension + prior_nhf + prior_nhis + prior_mortgage + prior_life + prior_rent_relief).quantize(CENT)
            if prior_claims_total > prior_gross:
                raise ValidationError({"prior_ytd": f"Opening reliefs and deductions exceed prior YTD gross emoluments for {profile.employee_code}."})
            if life_premium > ZERO and prior_life > ZERO:
                raise ValidationError({"tax_claims": f"A qualifying prior-year life-insurance premium was already included in YTD for {profile.employee_code}; do not claim the same annual premium twice."})

            rent_paid_ytd = (prior_rent + rent_paid_attributable).quantize(CENT)
            rent_relief = min(
                (rent_paid_ytd * Decimal(statutory_rules.rent_relief_rate)).quantize(CENT, rounding=ROUND_HALF_UP),
                Decimal(statutory_rules.rent_relief_cap),
            )
            employee_pension_ytd = (prior_pension + employee_pension).quantize(CENT)
            nhf_ytd = (prior_nhf + nhf).quantize(CENT)
            nhis_ytd = (prior_nhis + nhis).quantize(CENT)
            mortgage_ytd = (prior_mortgage + mortgage_interest).quantize(CENT)
            life_ytd = (prior_life + life_premium).quantize(CENT)

            remaining_periods = 12 - ends_on.month
            pay_periods_elapsed = prior_periods + 1
            expected_periods = pay_periods_elapsed + remaining_periods
            if expected_periods < pay_periods_elapsed or expected_periods <= 0:
                raise ValidationError({"prior_ytd": "YTD payroll period count is inconsistent with the tax year."})
            regular_monthly_gross = (basic + housing + transport).quantize(CENT)
            regular_employee_pension = (
                regular_monthly_gross * Decimal(statutory_rules.employee_pension_rate)
            ).quantize(CENT, rounding=ROUND_HALF_UP) if compensation.pension_applicable else ZERO
            projected_cash_gross = (prior_cash_gross + gross + regular_monthly_gross * remaining_periods).quantize(CENT)
            benefits_snapshot, taxable_benefits = _benefit_snapshot(
                inputs.get("benefits_in_kind", []),
                projected_annual_cash=projected_cash_gross,
                rule_set=statutory_rules,
            )
            taxable_gross_pay = (gross + taxable_benefits).quantize(CENT)
            cash_emoluments_ytd = (prior_cash_gross + gross).quantize(CENT)
            gross_ytd = (prior_gross + taxable_gross_pay).quantize(CENT)
            taxable_benefits_ytd = max(gross_ytd - cash_emoluments_ytd, ZERO).quantize(CENT)
            chargeable_ytd = max(
                gross_ytd - employee_pension_ytd - nhf_ytd - nhis_ytd - mortgage_ytd - life_ytd - rent_relief,
                ZERO,
            ).quantize(CENT)
            projected_gross = (gross_ytd + (regular_monthly_gross + taxable_benefits) * remaining_periods).quantize(CENT)
            projected_pension = (employee_pension_ytd + regular_employee_pension * remaining_periods).quantize(CENT)
            projected_nhf = (nhf_ytd + nhf * remaining_periods).quantize(CENT)
            projected_nhis = (nhis_ytd + nhis * remaining_periods).quantize(CENT)
            projected_mortgage = (mortgage_ytd + mortgage_interest * remaining_periods).quantize(CENT)
            projected_chargeable = max(
                projected_gross - projected_pension - projected_nhf - projected_nhis
                - projected_mortgage - life_ytd - rent_relief,
                ZERO,
            ).quantize(CENT)
            minimum_wage_exempt = bool(
                compensation.minimum_wage_applicable and taxable_gross_pay <= Decimal(statutory_rules.minimum_wage_monthly)
            )
            annual_tax, band_breakdown = progressive_tax(projected_chargeable, statutory_rules.paye_bands)
            if minimum_wage_exempt:
                annual_tax = ZERO
            cumulative_tax_target = (annual_tax * Decimal(pay_periods_elapsed) / Decimal(expected_periods)).quantize(CENT, rounding=ROUND_HALF_UP)
            paye_tax = max(cumulative_tax_target - prior_paye, ZERO).quantize(CENT)
            paye_ytd = (prior_paye + paye_tax).quantize(CENT)
            potential_tax_repayment = max(paye_ytd - cumulative_tax_target, ZERO).quantize(CENT)
            tax_snapshot = {
                "tax_year": tax_year,
                "ruleset_reference": statutory_rules.reference,
                "ruleset_effective_from": statutory_rules.effective_from.isoformat(),
                "tax_identity_reference": tax_identity.reference if tax_identity else "",
                "tax_identity_effective_from": tax_identity.effective_from.isoformat() if tax_identity else "",
                "gross_emoluments_ytd": str(gross_ytd),
                "cash_emoluments_ytd": str(cash_emoluments_ytd),
                "taxable_benefits_ytd": str(taxable_benefits_ytd),
                "employee_pension_ytd": str(employee_pension_ytd),
                "nhf_contribution_ytd": str(nhf_ytd),
                "nhis_contribution_ytd": str(nhis_ytd),
                "mortgage_interest_ytd": str(mortgage_ytd),
                "life_insurance_premium_ytd": str(life_ytd),
                "rent_paid_attributable_ytd": str(rent_paid_ytd),
                "rent_relief_ytd": str(rent_relief),
                "taxable_emoluments_ytd": str(max(gross_ytd - rent_relief, ZERO).quantize(CENT)),
                "chargeable_income_ytd": str(chargeable_ytd),
                "projected_annual_gross": str(projected_gross),
                "projected_annual_cash_emoluments": str(projected_cash_gross),
                "projected_annual_taxable_benefits": str(taxable_benefits_ytd + taxable_benefits * remaining_periods),
                "projected_annual_pension": str(projected_pension),
                "projected_annual_chargeable_income": str(projected_chargeable),
                "projected_annual_tax": str(annual_tax),
                "pay_periods_ytd": pay_periods_elapsed,
                "expected_pay_periods": expected_periods,
                "cumulative_tax_target_ytd": str(cumulative_tax_target),
                "paye_withheld_ytd": str(paye_ytd),
                "potential_tax_repayment_ytd": str(potential_tax_repayment),
                "minimum_wage_exempt": minimum_wage_exempt,
                "owned_asset_benefit_rate": str(statutory_rules.owned_asset_benefit_rate),
                "accommodation_income_cap_rate": str(statutory_rules.accommodation_income_cap_rate),
                "benefits_in_kind": benefits_snapshot,
                "band_breakdown": band_breakdown,
                "opening_balance_evidence_reference": str((opening or {}).get("evidence_reference") or ""),
            }
            claims_snapshot = {
                key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in claims_snapshot.items()
            }
            claims_snapshot["rent_relief"] = str(rent_relief)
            deductions.extend([
                {"label": "Employee pension contribution", "amount": str(employee_pension)},
                {"label": "PAYE", "amount": str(paye_tax)},
                {"label": "National Housing Fund contribution", "amount": str(nhf)},
                {"label": "National Health Insurance Scheme contribution", "amount": str(nhis)},
            ])
            deductions = [row for row in deductions if Decimal(row["amount"]) > ZERO]

        deductions_total = _components_total(deductions)
        net = (gross - deductions_total).quantize(CENT)
        if net < ZERO:
            raise ValidationError({"deductions": f"Statutory and other deductions exceed gross pay for {profile.employee_code}."})
        PayrollLine.objects.create(
            period=period,
            staff=profile.user,
            compensation=compensation,
            tax_identity=tax_identity,
            employee_code=profile.employee_code,
            employee_name=profile.user.full_name[:201],
            department=profile.department,
            currency=currency,
            basic_pay=basic,
            housing_pay=housing,
            transport_pay=transport,
            overtime_hours=hours,
            overtime_rate=overtime_rate,
            overtime_pay=overtime_pay,
            allowances=allowances,
            bonus=bonus,
            benefits_in_kind=benefits_snapshot,
            taxable_benefits=taxable_benefits,
            taxable_gross_pay=taxable_gross_pay,
            deductions=deductions,
            pensionable_pay=pensionable_pay,
            employee_pension=employee_pension,
            employer_pension=employer_pension,
            paye_tax=paye_tax,
            nhf_contribution=nhf,
            nhis_contribution=nhis,
            mortgage_interest_claim=mortgage_interest,
            life_insurance_premium_claim=life_premium,
            rent_paid_attributable=rent_paid_attributable,
            rent_relief=rent_relief,
            tax_claims=claims_snapshot,
            tax_snapshot=tax_snapshot,
            gross_pay=gross,
            deductions_total=deductions_total,
            net_pay=net,
        )
        totals["gross"] += gross
        totals["deductions"] += deductions_total
        totals["net"] += net
        totals["paye"] += paye_tax
        totals["employer_pension"] += employer_pension

    if totals["gross"] <= ZERO:
        raise ValidationError("Payroll total gross pay must be greater than zero.")
    if totals["net"] <= ZERO:
        raise ValidationError("Payroll total net pay must be greater than zero before it can be approved or settled.")
    period.total_gross = totals["gross"].quantize(CENT)
    period.total_deductions = totals["deductions"].quantize(CENT)
    period.total_net = totals["net"].quantize(CENT)
    period.total_paye = totals["paye"].quantize(CENT)
    period.total_employer_pension = totals["employer_pension"].quantize(CENT)
    period.save(update_fields=["total_gross", "total_deductions", "total_net", "total_paye", "total_employer_pension", "updated_at"])
    _event(period, PayrollEvent.Type.CREATED, actor, {
        "line_count": len(user_ids), "replaces": replaces.reference if replaces else "",
        "statutory_rules": statutory_rules.reference if statutory_rules else "",
        "total_paye": str(period.total_paye), "total_employer_pension": str(period.total_employer_pension),
    })
    return period, True


@transaction.atomic
def submit_payroll_period(*, period, actor):
    period = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    if period.status == PayrollPeriod.Status.SUBMITTED:
        return period
    if period.status != PayrollPeriod.Status.DRAFT:
        raise ValidationError(f"Payroll run cannot be submitted from {period.status}.")
    if period.created_by_id != actor.pk:
        raise PermissionDenied("Only the payroll run creator can submit it for review.")
    if period.ends_on > hotel_today():
        raise ValidationError({"ends_on": "A payroll period cannot be submitted before its end date."})
    if not period.lines.exists() or period.total_gross <= ZERO or period.total_net <= ZERO:
        raise ValidationError("Payroll run must contain positive, calculated employee lines before submission.")
    period.status = PayrollPeriod.Status.SUBMITTED
    period.submitted_at = timezone.now()
    period.save(update_fields=["status", "submitted_at", "updated_at"])
    _event(period, PayrollEvent.Type.SUBMITTED, actor, {"line_count": period.lines.count()})
    return period


@transaction.atomic
def review_payroll_period(*, period, reviewer, approved: bool, review_note=""):
    period = PayrollPeriod.objects.select_for_update().get(pk=period.pk)
    if period.status in {PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.REJECTED}:
        expected = PayrollPeriod.Status.APPROVED if approved else PayrollPeriod.Status.REJECTED
        if period.status == expected:
            return period
        raise ValidationError("A final payroll review decision cannot be changed.")
    if period.status != PayrollPeriod.Status.SUBMITTED:
        raise ValidationError(f"Payroll run cannot be reviewed from {period.status}.")
    if period.created_by_id == reviewer.pk:
        raise PermissionDenied("The preparer cannot approve or reject their own payroll run.")
    if not has_capability(reviewer, "payroll.approve"):
        raise PermissionDenied("This user cannot review payroll.")

    now = timezone.now()
    period.reviewed_by = reviewer
    period.reviewed_at = now
    period.review_note = (review_note or "")[:1000]
    if approved:
        employee_pension_total = sum((line.employee_pension for line in period.lines.all()), ZERO).quantize(CENT)
        pension_payable = (employee_pension_total + period.total_employer_pension).quantize(CENT)
        other_deductions = (period.total_deductions - period.total_paye - employee_pension_total).quantize(CENT)
        if other_deductions < ZERO:
            raise ValidationError("Payroll statutory deductions do not reconcile; reject and correct the run before approval.")
        lines = [{
            "account_code": accounting.PAYROLL_EXPENSE,
            "direction": FinancialLine.Direction.DEBIT,
            "amount": period.total_gross,
            "description": f"Gross wages and compensation for {period.reference}",
        }]
        if period.total_employer_pension > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_PENSION_EXPENSE,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": period.total_employer_pension,
                "description": f"Employer pension contributions for {period.reference}",
            })
        if period.total_net > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": period.total_net,
                "description": f"Net staff pay payable for {period.reference}",
            })
        if period.total_paye > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_TAX_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": period.total_paye,
                "description": f"PAYE withheld for {period.reference}",
            })
        if pension_payable > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_PENSION_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": pension_payable,
                "description": f"Employee and employer pension contributions payable for {period.reference}",
            })
        if other_deductions > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_DEDUCTIONS_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": other_deductions,
                "description": f"Other payroll deductions withheld for {period.reference}",
            })
        source_key = f"payroll-accrual:{period.reference}"
        transaction_record, _ = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.PAYROLL_ACCRUAL,
            lines=lines,
            actor=reviewer,
            source_key=source_key,
            idempotency_key=source_key,
            source_reference=period.reference,
            narrative=f"Approved payroll accrual for {period.starts_on} through {period.ends_on}",
            currency=period.currency,
            business_date=period.ends_on,
            metadata={"payroll_period": period.reference, "employee_count": period.lines.count()},
        )
        period.status = PayrollPeriod.Status.APPROVED
        period.approved_by = reviewer
        period.approved_at = now
        period.accrual_transaction = transaction_record
        _event(period, PayrollEvent.Type.APPROVED, reviewer, {
            "financial_reference": transaction_record.reference,
            "gross": str(period.total_gross),
            "deductions": str(period.total_deductions),
            "net": str(period.total_net),
        })
    else:
        period.status = PayrollPeriod.Status.REJECTED
        _event(period, PayrollEvent.Type.REJECTED, reviewer, {"review_note": period.review_note})
    period.save(update_fields=[
        "status", "reviewed_by", "reviewed_at", "review_note", "approved_by", "approved_at",
        "accrual_transaction", "updated_at",
    ])
    return period


def _salary_payment_fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _effective_salary_paid(*, line=None, period=None):
    records = PayrollSalaryPayment.objects.all()
    if line is not None:
        records = records.filter(line_id=line.pk)
    elif period is not None:
        records = records.filter(line__period_id=period.pk)
    else:
        raise ValueError("A payroll line or period is required.")
    totals = records.aggregate(
        recorded=Sum("amount", filter=Q(reversal_of__isnull=True)),
        reversed=Sum("amount", filter=Q(reversal_of__isnull=False)),
    )
    return (Decimal(totals["recorded"] or ZERO) - Decimal(totals["reversed"] or ZERO)).quantize(CENT)


def _idempotent_salary_payment(*, key, fingerprint):
    existing = PayrollSalaryPayment.objects.select_for_update().filter(idempotency_key=key).first()
    if existing and existing.idempotency_fingerprint != fingerprint:
        raise ValidationError({"idempotency_key": "This key was already used for a different salary-payment request."})
    return existing


def _locked_salary_cash_session(*, reference, actor, amount, cash_in=False):
    reference = str(reference or "").strip()
    if not reference:
        raise ValidationError({"cash_session_reference": "An open cash-session reference is required for cash payroll."})
    session = CashSession.objects.select_for_update().filter(reference=reference).first()
    if session is None:
        raise ValidationError({"cash_session_reference": "Cash session not found."})
    if session.status != CashSession.Status.OPEN:
        raise ValidationError({"cash_session_reference": "Cash salary entries require an open cash session."})
    if session.cashier_id != actor.pk:
        raise PermissionDenied("Use your own open cash session for a salary payment or cash return.")
    if not cash_in and Decimal(session.expected_cash) < amount:
        raise ValidationError({"cash_session_reference": "Cash session expected balance is insufficient for this payment."})
    return session


def _require_posted_payroll_accrual(period):
    if period.accrual_transaction_id is None:
        raise ValidationError("The approved payroll run has no linked accrual transaction.")
    if period.accrual_transaction.status != FinancialTransaction.Status.POSTED:
        raise ValidationError("The payroll accrual is not posted; reconcile the approved run before recording payment.")


def _sync_payroll_payment_status(*, period, actor):
    """Reconcile run status from immutable salary-payment and reversal rows."""
    total_net = Decimal(period.total_net).quantize(CENT)
    paid = _effective_salary_paid(period=period)
    if paid < ZERO or paid > total_net:
        raise ValidationError("Recorded salary payments do not reconcile to the approved run total.")
    if total_net <= ZERO:
        new_status = PayrollPeriod.Status.APPROVED if period.status != PayrollPeriod.Status.PAID else PayrollPeriod.Status.PAID
    elif paid == total_net:
        new_status = PayrollPeriod.Status.PAID
    elif paid > ZERO:
        new_status = PayrollPeriod.Status.PARTIALLY_PAID
    else:
        new_status = PayrollPeriod.Status.APPROVED
    if period.status != new_status:
        period.status = new_status
        update_fields = ["status", "updated_at"]
        if new_status == PayrollPeriod.Status.PAID:
            period.paid_by = actor
            period.paid_at = timezone.now()
            update_fields.extend(["paid_by", "paid_at"])
        period.save(update_fields=update_fields)
    return new_status, paid, (total_net - paid).quantize(CENT)


def _record_period_salary_payment_lines(*, period, payment, actor, method, external_reference, cash_session, recorded_at):
    """Project the newly posted full-run settlement into employee payment rows."""
    line_net_total = PayrollLine.objects.filter(period=period).aggregate(total=Sum("net_pay"))["total"] or ZERO
    if Decimal(line_net_total).quantize(CENT) != Decimal(period.total_net).quantize(CENT):
        raise ValidationError("Employee payroll lines do not reconcile to the full-run settlement total.")
    payment_date = timezone.localdate(recorded_at)
    for line in PayrollLine.objects.select_for_update().filter(period=period).order_by("pk"):
        amount = Decimal(line.net_pay).quantize(CENT)
        if amount <= ZERO:
            continue
        key = f"payroll-full-run-salary-payment:{period.reference}:{line.pk}"
        fingerprint = _salary_payment_fingerprint({
            "period": period.reference, "line_id": line.pk, "amount": str(amount),
            "method": method, "external_reference": external_reference or "",
            "cash_session": getattr(cash_session, "reference", ""), "payment_date": payment_date.isoformat(),
        })
        PayrollSalaryPayment.objects.create(
            reference=f"SAL-FULLRUN-{line.pk}",
            idempotency_key=key,
            idempotency_fingerprint=fingerprint,
            line=line,
            amount=amount,
            currency=period.currency,
            payment_date=payment_date,
            method=method,
            external_reference=(external_reference or "")[:160],
            notes=f"Employee allocation from full-run settlement {period.reference}.",
            recorded_by=actor,
            financial_transaction=payment,
            cash_session=cash_session,
            is_legacy_import=False,
        )


@transaction.atomic
def record_payroll_salary_payment(
    *, period, line, actor, amount, method, payment_date, external_reference="",
    evidence_reference="", notes="", idempotency_key="", cash_session_reference="",
):
    """Record an already completed employee payment; never initiates a payout."""
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot record salary payments.")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "A salary-payment idempotency key is required."})
    amount = _decimal(amount, "amount", maximum=Decimal("9999999999.99"))
    if amount <= ZERO:
        raise ValidationError({"amount": "Salary payment amount must be greater than zero."})
    method = str(method or "").upper()
    if method not in PayrollSalaryPayment.Method.values:
        raise ValidationError({"method": "Choose CASH or BANK_TRANSFER."})
    payment_date = payment_date or hotel_today()
    if payment_date > hotel_today():
        raise ValidationError({"payment_date": "A salary payment cannot be dated in the future."})
    external_reference = str(external_reference or "").strip()[:160]
    evidence_reference = str(evidence_reference or "").strip()[:160]
    notes = str(notes or "").strip()[:1000]
    if method == PayrollSalaryPayment.Method.BANK_TRANSFER:
        if not external_reference:
            raise ValidationError({"external_reference": "A bank/payment reference is required for a bank transfer."})
        if cash_session_reference:
            raise ValidationError({"cash_session_reference": "A cash session is only valid for CASH payments."})
    fingerprint_payload = {
        "operation": "record", "period": period.reference, "line_id": line.pk,
        "amount": str(amount), "method": method, "payment_date": payment_date.isoformat(),
        "external_reference": external_reference, "evidence_reference": evidence_reference,
        "notes": notes, "cash_session_reference": str(cash_session_reference or "").strip(),
        "actor_id": actor.pk,
    }
    fingerprint = _salary_payment_fingerprint(fingerprint_payload)
    existing = _idempotent_salary_payment(key=key, fingerprint=fingerprint)
    if existing:
        if existing.line_id != line.pk or existing.reversal_of_id is not None:
            raise ValidationError({"idempotency_key": "This key belongs to a different payroll operation."})
        return existing, False

    period = PayrollPeriod.objects.select_for_update().select_related("accrual_transaction").get(pk=period.pk)
    line = PayrollLine.objects.select_for_update().get(pk=line.pk, period=period)
    # Recheck after taking the period lock so concurrent retries converge to one
    # record before checking the now-updated run balance.
    existing = _idempotent_salary_payment(key=key, fingerprint=fingerprint)
    if existing:
        if existing.line_id != line.pk or existing.reversal_of_id is not None:
            raise ValidationError({"idempotency_key": "This key belongs to a different payroll operation."})
        return existing, False
    if period.status not in {PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.PARTIALLY_PAID}:
        raise ValidationError(f"Salary payments cannot be recorded for a payroll run in {period.status}.")
    _require_posted_payroll_accrual(period)
    outstanding = (Decimal(line.net_pay) - _effective_salary_paid(line=line)).quantize(CENT)
    if outstanding <= ZERO:
        raise ValidationError({"amount": "This employee has no outstanding salary balance."})
    if amount > outstanding:
        raise ValidationError({"amount": f"Payment exceeds the employee's outstanding balance of {outstanding}."})

    session = None
    credit_account = accounting.BANK_CLEARING
    if method == PayrollSalaryPayment.Method.CASH:
        session = _locked_salary_cash_session(
            reference=cash_session_reference, actor=actor, amount=amount,
        )
        credit_account = accounting.CASH_ON_HAND

    from apps.finance.services.references import generate_finance_reference
    payment_reference = generate_finance_reference("SAL")
    finance_key = "payroll-salary-payment:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    financial_payment, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYROLL_PAYMENT,
        lines=[
            {
                "account_code": accounting.PAYROLL_PAYABLE,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "description": f"Salary payment for {line.employee_code or line.employee_name}",
            },
            {
                "account_code": credit_account,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": f"Manually recorded salary payment via {method}",
            },
        ],
        actor=actor,
        source_key=finance_key,
        idempotency_key=finance_key,
        source_reference=payment_reference,
        external_reference=external_reference,
        narrative=f"Manual salary payment for {line.employee_code or line.employee_name} · {period.reference}",
        currency=period.currency,
        business_date=payment_date,
        metadata={
            "payroll_period": period.reference, "payroll_line_id": line.pk,
            "salary_payment_reference": payment_reference, "method": method,
        },
    )
    try:
        with transaction.atomic():
            salary_payment = PayrollSalaryPayment.objects.create(
                reference=payment_reference,
                idempotency_key=key,
                idempotency_fingerprint=fingerprint,
                line=line,
                amount=amount,
                currency=period.currency,
                payment_date=payment_date,
                method=method,
                external_reference=external_reference,
                evidence_reference=evidence_reference,
                notes=notes,
                recorded_by=actor,
                financial_transaction=financial_payment,
                cash_session=session,
            )
    except IntegrityError:
        existing = _idempotent_salary_payment(key=key, fingerprint=fingerprint)
        if existing:
            return existing, False
        raise

    if session:
        CashMovement.objects.create(
            cash_session=session,
            transaction=financial_payment,
            type=CashMovement.Type.PAID_OUT,
            amount=amount,
            currency=period.currency,
            actor=actor,
            source_reference=salary_payment.reference,
            notes=f"Manually recorded salary payment {salary_payment.reference}",
            metadata={"payroll_period": period.reference, "salary_payment_reference": salary_payment.reference},
        )
        session.expected_cash = (Decimal(session.expected_cash) - amount).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])

    previous_status = period.status
    new_status, cumulative_paid, balance = _sync_payroll_payment_status(period=period, actor=actor)
    # Keep the run timeline bounded to payment-state transitions; every record
    # remains available in the paginated employee history and audit log.
    if previous_status != new_status:
        _event(period, PayrollEvent.Type.PAYMENT_RECORDED, actor, {
            "payment_reference": salary_payment.reference,
            "financial_reference": financial_payment.reference,
            "line_id": line.pk,
            "amount": str(amount),
            "method": method,
            "payment_date": payment_date.isoformat(),
            "cumulative_paid": str(cumulative_paid),
            "outstanding": str(balance),
            "period_status": new_status,
        })
    if new_status == PayrollPeriod.Status.PAID and previous_status != PayrollPeriod.Status.PAID:
        _event(period, PayrollEvent.Type.PAID, actor, {
            "settlement_mode": "employee_level_manual_records",
            "cumulative_paid": str(cumulative_paid),
            "financial_reference": financial_payment.reference,
        })
    return salary_payment, True


@transaction.atomic
def reverse_payroll_salary_payment(
    *, payment, actor, correction_reason, idempotency_key, payment_date=None,
    external_reference="", evidence_reference="", cash_session_reference="",
):
    """Append an independently authorized full reversal and ledger correction."""
    if not has_capability(actor, "payroll.approve"):
        raise PermissionDenied("This user cannot authorize salary-payment corrections.")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "A reversal idempotency key is required."})
    correction_reason = str(correction_reason or "").strip()[:500]
    if len(correction_reason) < 5:
        raise ValidationError({"correction_reason": "Provide a correction reason of at least five characters."})
    payment_date = payment_date or hotel_today()
    if payment_date > hotel_today():
        raise ValidationError({"payment_date": "A payment reversal cannot be dated in the future."})
    external_reference = str(external_reference or "").strip()[:160]
    evidence_reference = str(evidence_reference or "").strip()[:160]
    cash_session_reference = str(cash_session_reference or "").strip()
    fingerprint_payload = {
        "operation": "reverse", "payment_reference": payment.reference,
        "correction_reason": correction_reason, "payment_date": payment_date.isoformat(),
        "external_reference": external_reference, "evidence_reference": evidence_reference,
        "cash_session_reference": cash_session_reference, "actor_id": actor.pk,
    }
    fingerprint = _salary_payment_fingerprint(fingerprint_payload)
    existing = _idempotent_salary_payment(key=key, fingerprint=fingerprint)
    if existing:
        if existing.reversal_of_id != payment.pk:
            raise ValidationError({"idempotency_key": "This key belongs to a different payroll operation."})
        return existing, False

    payment = PayrollSalaryPayment.objects.select_for_update().select_related(
        "line", "line__period", "financial_transaction", "cash_session",
    ).get(pk=payment.pk)
    period = PayrollPeriod.objects.select_for_update().get(pk=payment.line.period_id)
    existing = _idempotent_salary_payment(key=key, fingerprint=fingerprint)
    if existing:
        if existing.reversal_of_id != payment.pk:
            raise ValidationError({"idempotency_key": "This key belongs to a different payroll operation."})
        return existing, False
    reversal = PayrollSalaryPayment.objects.select_for_update().filter(reversal_of=payment).first()
    if reversal:
        raise ValidationError({"payment_reference": "This salary payment has already been reversed."})
    if payment.reversal_of_id is not None:
        raise ValidationError({"payment_reference": "A reversal entry cannot itself be reversed; contact finance to correct a reversal."})
    if payment.recorded_by_id == actor.pk:
        raise PermissionDenied("A salary-payment correction must be authorized by someone other than its recorder.")
    if payment.financial_transaction_id is None or payment.financial_transaction.status != FinancialTransaction.Status.POSTED:
        raise ValidationError("This salary payment has no posted financial transaction to reverse; use a separately reviewed finance adjustment.")
    if (
        payment.financial_transaction.type != FinancialTransaction.Type.PAYROLL_PAYMENT
        or payment.financial_transaction.currency != payment.currency
    ):
        raise ValidationError("The linked ledger transaction does not match this salary payment; reconcile it before reversal.")
    if period.status not in {
        PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.PARTIALLY_PAID, PayrollPeriod.Status.PAID,
    }:
        raise ValidationError(f"Salary payments cannot be corrected for a payroll run in {period.status}.")
    if payment_date < payment.payment_date:
        raise ValidationError({"payment_date": "A payment correction cannot predate the original salary payment."})

    if payment.method not in PayrollSalaryPayment.Method.values:
        raise ValidationError("The original salary-payment method is missing or invalid; reconcile it before reversal.")
    session = None
    account = accounting.BANK_CLEARING
    if payment.method == PayrollSalaryPayment.Method.CASH:
        session = _locked_salary_cash_session(
            reference=cash_session_reference, actor=actor, amount=Decimal(payment.amount), cash_in=True,
        )
        account = accounting.CASH_ON_HAND
    elif cash_session_reference:
        raise ValidationError({"cash_session_reference": "A cash session is only valid when returning cash."})

    from apps.finance.services.references import generate_finance_reference
    reversal_reference = generate_finance_reference("SALREV")
    finance_key = "payroll-salary-reversal:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    amount = Decimal(payment.amount).quantize(CENT)
    financial_reversal, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYROLL_PAYMENT,
        lines=[
            {
                "account_code": account,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "description": f"Reverse salary payment {payment.reference}",
            },
            {
                "account_code": accounting.PAYROLL_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": f"Restore payroll payable for {payment.reference}",
            },
        ],
        actor=actor,
        source_key=finance_key,
        idempotency_key=finance_key,
        source_reference=payment.reference,
        external_reference=external_reference,
        narrative=f"Authorized salary-payment reversal {payment.reference} · {period.reference}",
        currency=payment.currency,
        business_date=payment_date,
        metadata={
            "payroll_period": period.reference, "salary_payment_reversal_of": payment.reference,
            "salary_payment_reversal_reference": reversal_reference,
            "correction_reason": correction_reason, "method": payment.method,
        },
        reversal_of=payment.financial_transaction,
    )
    try:
        with transaction.atomic():
            reversal_record = PayrollSalaryPayment.objects.create(
                reference=reversal_reference,
                idempotency_key=key,
                idempotency_fingerprint=fingerprint,
                line=payment.line,
                amount=amount,
                currency=payment.currency,
                payment_date=payment_date,
                method=payment.method,
                external_reference=external_reference,
                evidence_reference=evidence_reference,
                notes=f"Reversal of {payment.reference}.",
                recorded_by=actor,
                financial_transaction=financial_reversal,
                cash_session=session,
                reversal_of=payment,
                correction_reason=correction_reason,
            )
    except IntegrityError:
        existing = _idempotent_salary_payment(key=key, fingerprint=fingerprint)
        if existing and existing.reversal_of_id == payment.pk:
            return existing, False
        raise

    if session:
        CashMovement.objects.create(
            cash_session=session,
            transaction=financial_reversal,
            type=CashMovement.Type.CASH_IN,
            amount=amount,
            currency=payment.currency,
            actor=actor,
            source_reference=payment.reference,
            notes=f"Returned cash for salary-payment reversal {payment.reference}",
            metadata={
                "payroll_period": period.reference, "salary_payment_reference": payment.reference,
                "salary_payment_reversal_reference": reversal_record.reference,
            },
        )
        session.expected_cash = (Decimal(session.expected_cash) + amount).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])

    previous_status = period.status
    new_status, cumulative_paid, balance = _sync_payroll_payment_status(period=period, actor=actor)
    if previous_status != new_status:
        _event(period, PayrollEvent.Type.PAYMENT_REVERSED, actor, {
            "payment_reference": payment.reference,
            "reversal_reference": reversal_record.reference,
            "financial_reference": financial_reversal.reference,
            "amount": str(amount),
            "correction_reason": correction_reason,
            "cumulative_paid": str(cumulative_paid),
            "outstanding": str(balance),
            "period_status": new_status,
        })
    return reversal_record, True


@transaction.atomic
def pay_payroll_period(*, period, actor, method, external_reference="", cash_session=None):
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot settle payroll.")
    period = PayrollPeriod.objects.select_for_update().select_related("accrual_transaction").get(pk=period.pk)
    if period.status == PayrollPeriod.Status.PAID:
        return period
    if period.status != PayrollPeriod.Status.APPROVED:
        raise ValidationError(f"Payroll run cannot be paid from {period.status}.")
    _require_posted_payroll_accrual(period)
    if method not in PayrollPeriod.PaymentMethod.values:
        raise ValidationError({"method": "Choose CASH or BANK_TRANSFER."})
    if method == PayrollPeriod.PaymentMethod.BANK_TRANSFER and not str(external_reference or "").strip():
        raise ValidationError({"external_reference": "A bank transfer reference is required."})

    credit_account = accounting.BANK_CLEARING
    session = None
    if method == PayrollPeriod.PaymentMethod.CASH:
        if cash_session is None:
            raise ValidationError({"cash_session_reference": "Cash payroll settlement requires an open cashier session."})
        session = CashSession.objects.select_for_update().filter(pk=cash_session.pk).first()
        if session is None or session.status != CashSession.Status.OPEN or session.cashier_id != actor.pk:
            raise ValidationError("Use your own open cash session to settle payroll in cash.")
        if Decimal(session.expected_cash) < Decimal(period.total_net):
            raise ValidationError("Cash session expected balance is insufficient for this payroll settlement.")
        credit_account = accounting.CASH_ON_HAND

    source_key = f"payroll-payment:{period.reference}"
    payment, _ = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.PAYROLL_PAYMENT,
        lines=[
            {
                "account_code": accounting.PAYROLL_PAYABLE,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": period.total_net,
                "description": f"Settle net payroll payable for {period.reference}",
            },
            {
                "account_code": credit_account,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": period.total_net,
                "description": f"Payroll payment via {method} for {period.reference}",
            },
        ],
        actor=actor,
        source_key=source_key,
        idempotency_key=source_key,
        source_reference=period.reference,
        external_reference=(external_reference or "")[:160],
        narrative=f"Payroll settlement for {period.starts_on} through {period.ends_on}",
        currency=period.currency,
        business_date=period.ends_on,
        metadata={"payroll_period": period.reference, "method": method},
    )
    if session:
        CashMovement.objects.create(
            cash_session=session,
            transaction=payment,
            type=CashMovement.Type.PAID_OUT,
            amount=period.total_net,
            currency=period.currency,
            actor=actor,
            source_reference=period.reference,
            notes=f"Payroll settlement {period.reference}",
            metadata={"payroll_period": period.reference, "method": method},
        )
        session.expected_cash = (Decimal(session.expected_cash) - Decimal(period.total_net)).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])

    now = timezone.now()
    period.status = PayrollPeriod.Status.PAID
    period.paid_by = actor
    period.paid_at = now
    period.payment_method = method
    period.external_reference = (external_reference or "")[:160]
    period.cash_session = session
    period.payment_transaction = payment
    period.save(update_fields=[
        "status", "paid_by", "paid_at", "payment_method", "external_reference", "cash_session",
        "payment_transaction", "updated_at",
    ])
    _record_period_salary_payment_lines(
        period=period,
        payment=payment,
        actor=actor,
        method=method,
        external_reference=external_reference,
        cash_session=session,
        recorded_at=now,
    )
    _event(period, PayrollEvent.Type.PAID, actor, {
        "financial_reference": payment.reference,
        "method": method,
        "external_reference": (external_reference or "")[:160],
        "employee_payment_records_created": True,
    })
    return period
