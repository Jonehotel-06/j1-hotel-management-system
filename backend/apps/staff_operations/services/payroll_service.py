"""Locked payroll preparation, maker-checker approval, and ledger settlement."""
from __future__ import annotations

import calendar
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.core.approvals import override_event_details, override_note, require_independent_approver
from apps.core.utils import hotel_today
from apps.finance.models import CashMovement, CashSession, FinancialLine, FinancialTransaction
from apps.finance.services import accounting
from apps.finance.services.ledger_service import create_posted_transaction
from apps.inventory.models import StockCount

from ..models import (
    AttendanceRecord, Department, LatenessDeductionPolicy, PayrollEvent, PayrollLine, PayrollPeriod,
    PayrollSalaryPayment, PayrollStockShortageAllocation, PayrollStockShortageCase, StaffCompensation,
    StaffLatenessDeduction, StaffProfile,
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


def _components_total(rows):
    return sum((Decimal(row["amount"]) for row in rows), ZERO).quantize(CENT)


_STATUTORY_DEDUCTION_LABELS = {
    "paye", "pay as you earn", "income tax", "tax", "pension", "employee pension",
    "employer pension", "pension contribution", "employee pension contribution",
    "nhf", "national housing fund", "national housing fund contribution",
    "nhis", "national health insurance", "national health insurance scheme",
    "national health insurance scheme contribution",
}


def _validate_manual_deduction_labels(rows):
    for row in rows:
        normalized = " ".join(str(row.get("label") or "").strip().casefold().replace("-", " ").split())
        if any(term in normalized for term in _STATUTORY_DEDUCTION_LABELS):
            raise ValidationError({
                "deductions": "Government tax and pension deductions are no longer calculated or recorded in payroll; use an internal deduction label instead."
            })


def _event(period, event_type, actor, details=None):
    return PayrollEvent.objects.create(
        period=period,
        type=event_type,
        actor=actor,
        details=dict(details or {}),
    )


@transaction.atomic
def create_compensation(
    *, staff, actor, effective_from, currency, basic_salary, housing_allowance=ZERO,
    transport_allowance=ZERO, overtime_rate=ZERO, notes="",
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
        created_by=actor,
        notes=(notes or "")[:500],
    )
    compensation.full_clean(exclude=["created_by"])
    try:
        compensation.save()
    except IntegrityError as exc:
        raise ValidationError({"effective_from": "Compensation already exists for this staff member on that date."}) from exc
    return compensation


def _department_staff_profiles(department, *, for_update=False):
    queryset = StaffProfile.objects.select_related("user", "department_ref")
    if for_update:
        queryset = queryset.select_for_update()
    return list(
        queryset.filter(
            user__is_active=True,
            employment_status__in=[
                StaffProfile.EmploymentStatus.ACTIVE,
                StaffProfile.EmploymentStatus.ON_LEAVE,
            ],
        )
        .exclude(user__role=User.Role.GUEST)
        .filter(
            Q(department_ref=department)
            | Q(department__iexact=department.name)
            | Q(department__iexact=department.code)
        )
        .order_by("employee_code", "user_id")
    )


@transaction.atomic
def create_stock_shortage_case(
    *, actor, department_id, item_summary="", total_amount=None, stock_count_reference="",
    incident_date, effective_date, evidence_reference="", notes="",
):
    """Record a proposed department-wide missing-stock recovery case.

    This only records the proposal.  No employee's pay changes until an
    ADMIN-role account approves the case and a later payroll run consumes the
    resulting one-time allocations.
    """
    if not (has_capability(actor, "payroll.manage") or has_capability(actor, "inventory.manage")):
        raise PermissionDenied("This user cannot record stock-shortage cases.")
    department = Department.objects.select_for_update().filter(pk=department_id, is_active=True).first()
    if department is None:
        raise ValidationError({"department_id": "Select an active department."})
    if incident_date > hotel_today():
        raise ValidationError({"incident_date": "The incident date cannot be in the future."})
    if effective_date < incident_date:
        raise ValidationError({"effective_date": "The payroll effective date cannot precede the incident date."})

    item_summary = str(item_summary or "").strip()[:500]
    evidence_reference = str(evidence_reference or "").strip()[:160]
    shortage_lines = []
    stock_count = None
    currency = "NGN"
    stock_count_reference = str(stock_count_reference or "").strip()
    if stock_count_reference:
        stock_count = (
            StockCount.objects.select_for_update()
            .select_related("location")
            .prefetch_related("lines__item")
            .filter(reference=stock_count_reference)
            .first()
        )
        if stock_count is None:
            raise ValidationError({"stock_count_reference": "Stock count not found."})
        if stock_count.status != StockCount.Status.POSTED:
            raise ValidationError({"stock_count_reference": "Only a posted stock count can support a shortage case."})
        if PayrollStockShortageCase.objects.filter(stock_count=stock_count).exclude(
            status=PayrollStockShortageCase.Status.REJECTED,
        ).exists():
            raise ValidationError({"stock_count_reference": "This stock count already has a stock-shortage case."})
        currencies = set()
        computed_total = ZERO
        for line in stock_count.lines.all():
            variance = Decimal(line.variance or ZERO)
            if variance >= ZERO:
                continue
            missing_quantity = (-variance).quantize(Decimal("0.0001"))
            unit_cost = Decimal(line.item.standard_cost or ZERO).quantize(CENT)
            if unit_cost <= ZERO:
                raise ValidationError({
                    "stock_count_reference": f"Stock item {line.item.sku} has no standard cost; set its cost before using the count as the value source."
                })
            currencies.add(str(line.item.currency or "NGN").upper())
            line_value = (missing_quantity * unit_cost).quantize(CENT, rounding=ROUND_HALF_UP)
            computed_total += line_value
            shortage_lines.append({
                "item_sku": line.item.sku,
                "item_name": line.item.name,
                "missing_quantity": str(missing_quantity),
                "unit_cost": str(unit_cost),
                "amount": str(line_value),
            })
        if not shortage_lines or computed_total <= ZERO:
            raise ValidationError({"stock_count_reference": "This posted stock count has no valued missing-stock lines."})
        if len(currencies) != 1:
            raise ValidationError({"stock_count_reference": "The missing stock lines use more than one currency."})
        currency = next(iter(currencies))
        if total_amount is not None and _decimal(total_amount, "total_amount") != computed_total:
            raise ValidationError({"total_amount": f"The stock count values the missing stock at {computed_total}; use that total or remove the stock-count link."})
        total_amount = computed_total
        if not item_summary:
            item_summary = "; ".join(
                f"{row['item_sku']} × {row['missing_quantity']}" for row in shortage_lines
            )[:500]
        evidence_reference = evidence_reference or stock_count.reference
    else:
        if not item_summary:
            raise ValidationError({"item_summary": "Describe the missing item(s)."})
        if total_amount is None:
            raise ValidationError({"total_amount": "Enter the total missing-stock value."})
        if not evidence_reference:
            raise ValidationError({"evidence_reference": "Record the incident or investigation evidence reference."})
        total_amount = _decimal(total_amount, "total_amount")
    if total_amount is None or _decimal(total_amount, "total_amount") <= ZERO:
        raise ValidationError({"total_amount": "The total missing-stock value must be greater than zero."})

    case = PayrollStockShortageCase(
        department=department,
        department_code=department.code,
        department_name=department.name,
        stock_count=stock_count,
        item_summary=item_summary,
        shortage_lines=shortage_lines,
        incident_date=incident_date,
        effective_date=effective_date,
        total_amount=_decimal(total_amount, "total_amount"),
        currency=currency,
        evidence_reference=evidence_reference,
        notes=str(notes or "").strip()[:1000],
        created_by=actor,
    )
    case.save()
    return case


@transaction.atomic
def decide_stock_shortage_case(*, case, actor, approved: bool, decision_note=""):
    """Approve/reject a shortage case; ADMIN is the only deciding role."""
    if not getattr(actor, "is_active", False) or getattr(actor, "role", None) != User.Role.ADMIN:
        raise PermissionDenied("Only an Admin can approve department-wide stock-shortage deductions.")
    case = PayrollStockShortageCase.objects.select_for_update().select_related("department").get(pk=case.pk)
    target_status = (
        PayrollStockShortageCase.Status.APPROVED if approved else PayrollStockShortageCase.Status.REJECTED
    )
    if case.status == target_status:
        return case
    if case.status != PayrollStockShortageCase.Status.PENDING_ADMIN_APPROVAL:
        raise ValidationError("This stock-shortage case has already been decided.")
    note = str(decision_note or "").strip()
    if len(note) < 5:
        raise ValidationError({"decision_note": "Record why the Admin approved or rejected this case."})

    allocations = []
    if approved:
        profiles = _department_staff_profiles(case.department, for_update=True)
        if not profiles:
            raise ValidationError("No active staff profiles are currently assigned to this department.")
        total_cents = int((Decimal(case.total_amount) * Decimal("100")).to_integral_value(rounding=ROUND_DOWN))
        if total_cents < len(profiles):
            raise ValidationError("The total is too small to allocate at least NGN 0.01 to every staff member in the department.")
        base_cents, remainder = divmod(total_cents, len(profiles))
        for index, profile in enumerate(profiles):
            cents = base_cents + (1 if index < remainder else 0)
            allocations.append(PayrollStockShortageAllocation(
                case=case,
                staff=profile.user,
                employee_code=profile.employee_code,
                employee_name=profile.user.full_name[:201],
                department=(profile.department or case.department_name)[:120],
                amount=(Decimal(cents) / Decimal("100")).quantize(CENT),
            ))

    case.status = target_status
    case.decided_by = actor
    case.decided_at = timezone.now()
    case.decision_note = note[:1000]
    case.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    if allocations:
        PayrollStockShortageAllocation.objects.bulk_create(allocations)
    return case


# ---------------------------------------------------------------------------
# Automatic lateness deductions
# ---------------------------------------------------------------------------

def get_lateness_policy() -> LatenessDeductionPolicy:
    return LatenessDeductionPolicy.get_policy()


@transaction.atomic
def update_lateness_policy(*, actor, is_enabled, grace_minutes, deduction_percent):
    """Only an ADMIN-role account may configure the automatic lateness rule."""
    if not getattr(actor, "is_active", False) or getattr(actor, "role", None) != User.Role.ADMIN:
        raise PermissionDenied("Only an Admin can configure the automatic lateness deduction policy.")
    try:
        grace = int(grace_minutes)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"grace_minutes": "Grace minutes must be a whole number."}) from exc
    if grace < 0 or grace > 1440:
        raise ValidationError({"grace_minutes": "Grace minutes must be between 0 and 1440."})
    percent = _decimal(deduction_percent, "deduction_percent")
    if percent > Decimal("100.00"):
        raise ValidationError({"deduction_percent": "The deduction percentage cannot exceed 100."})
    if is_enabled and percent <= ZERO:
        raise ValidationError({"deduction_percent": "Set a percentage above zero before enabling the policy."})
    policy = LatenessDeductionPolicy.objects.select_for_update().get_or_create(pk=1)[0]
    policy.is_enabled = bool(is_enabled)
    policy.grace_minutes = grace
    policy.deduction_percent = percent
    policy.updated_by = actor
    policy.save(update_fields=["is_enabled", "grace_minutes", "deduction_percent", "updated_by", "updated_at"])
    return policy


def _lateness_amounts(*, compensation, business_date, deduction_percent):
    """Percentage of one day's pay for the month containing ``business_date``."""
    basis = (
        Decimal(compensation.basic_salary) + Decimal(compensation.housing_allowance)
        + Decimal(compensation.transport_allowance)
    ).quantize(CENT)
    days = calendar.monthrange(business_date.year, business_date.month)[1]
    daily = (basis / Decimal(days)).quantize(CENT, rounding=ROUND_HALF_UP) if days else ZERO
    amount = (daily * Decimal(deduction_percent) / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    return basis, days, daily, amount


def _compensation_for_date(*, staff_id, business_date):
    return (
        StaffCompensation.objects.filter(staff_id=staff_id, effective_from__lte=business_date)
        .order_by("-effective_from", "-pk").first()
    )


@transaction.atomic
def create_lateness_deduction_for_attendance(*, attendance_record, compensation=None, policy=None):
    """Create the snapshot deduction for one late clock-in, if any is due.

    Returns ``None`` when there is nothing to deduct: no linked scheduled
    shift, the policy is disabled, or the clock-in is inside the grace period.
    One attendance record can only ever produce one deduction.
    """
    record = (
        AttendanceRecord.objects.select_for_update()
        .select_related("staff", "shift")
        .filter(pk=attendance_record.pk).first()
    )
    if record is None or record.shift_id is None or record.shift.planned_start_at is None:
        return None
    existing = StaffLatenessDeduction.objects.filter(attendance_record=record).first()
    if existing:
        return existing
    policy = policy or get_lateness_policy()
    if not policy.is_enabled or Decimal(policy.deduction_percent) <= ZERO:
        return None
    late_minutes = int((record.clock_in_at - record.shift.planned_start_at).total_seconds() // 60)
    if late_minutes <= int(policy.grace_minutes):
        return None
    compensation = compensation or _compensation_for_date(
        staff_id=record.staff_id, business_date=record.business_date,
    )
    basis, days, daily, amount = ZERO, 0, ZERO, ZERO
    currency = "NGN"
    if compensation is not None:
        basis, days, daily, amount = _lateness_amounts(
            compensation=compensation, business_date=record.business_date,
            deduction_percent=policy.deduction_percent,
        )
        currency = str(compensation.currency or "NGN").upper()
    return StaffLatenessDeduction.objects.create(
        staff=record.staff,
        attendance_record=record,
        shift=record.shift,
        business_date=record.business_date,
        scheduled_start_at=record.shift.planned_start_at,
        clock_in_at=record.clock_in_at,
        late_minutes=late_minutes,
        grace_minutes=int(policy.grace_minutes),
        deduction_percent=Decimal(policy.deduction_percent),
        monthly_pay_basis=basis,
        days_in_month=days,
        daily_pay=daily,
        amount=amount,
        currency=currency,
    )


def sync_lateness_deductions(*, staff_id, compensation, starts_on, ends_on):
    """Generate/fill deductions for a staff member's attendance in a period.

    Called while preparing a payroll run so late days recorded before the run
    (including records created before this feature existed, or clock-ins made
    while no salary terms were available yet) are priced from the salary terms
    the run itself is using. Existing snapshots with a calculated amount are
    never repriced.
    """
    policy = get_lateness_policy()
    records = list(
        AttendanceRecord.objects.select_related("shift")
        .filter(staff_id=staff_id, business_date__gte=starts_on, business_date__lte=ends_on, shift__isnull=False)
        .order_by("business_date", "pk")
    )
    for record in records:
        deduction = create_lateness_deduction_for_attendance(
            attendance_record=record, compensation=compensation, policy=policy,
        )
        if deduction is None:
            continue
        if (
            deduction.status == StaffLatenessDeduction.Status.ACTIVE
            and deduction.applied_payroll_line_id is None
            and Decimal(deduction.amount) <= ZERO
            and compensation is not None
        ):
            basis, days, daily, amount = _lateness_amounts(
                compensation=compensation, business_date=deduction.business_date,
                deduction_percent=deduction.deduction_percent,
            )
            deduction.monthly_pay_basis = basis
            deduction.days_in_month = days
            deduction.daily_pay = daily
            deduction.amount = amount
            deduction.currency = str(compensation.currency or "NGN").upper()
            deduction.save(update_fields=[
                "monthly_pay_basis", "days_in_month", "daily_pay", "amount", "currency", "updated_at",
            ])


@transaction.atomic
def request_lateness_revocation(*, deduction, actor, reason, evidence_reference=""):
    """A payroll manager records the staff member's tangible reason.

    The deduction is held out of payroll runs while the request awaits the
    Admin decision.
    """
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot record a lateness-deduction revocation request.")
    deduction = StaffLatenessDeduction.objects.select_for_update().get(pk=deduction.pk)
    if deduction.status == StaffLatenessDeduction.Status.REVOCATION_REQUESTED:
        return deduction
    if deduction.status not in {
        StaffLatenessDeduction.Status.ACTIVE, StaffLatenessDeduction.Status.APPLIED,
    }:
        raise ValidationError(f"A revocation cannot be requested from {deduction.status}.")
    reason = str(reason or "").strip()
    if len(reason) < 5:
        raise ValidationError({"reason": "Record the staff member's reason (at least 5 characters)."})
    deduction.status = StaffLatenessDeduction.Status.REVOCATION_REQUESTED
    deduction.revocation_reason = reason[:2000]
    deduction.revocation_evidence_reference = str(evidence_reference or "").strip()[:160]
    deduction.revocation_requested_by = actor
    deduction.revocation_requested_at = timezone.now()
    deduction.decided_by = None
    deduction.decided_at = None
    deduction.decision_note = ""
    deduction.save(update_fields=[
        "status", "revocation_reason", "revocation_evidence_reference", "revocation_requested_by",
        "revocation_requested_at", "decided_by", "decided_at", "decision_note", "updated_at",
    ])
    return deduction


@transaction.atomic
def decide_lateness_revocation(*, deduction, actor, approved: bool, decision_note=""):
    """Final Admin decision on a revocation request.

    Approved before any payroll run included the deduction -> cancelled.
    Approved after a payroll run included it -> the amount is added back on a
    later payroll run. Rejected -> the deduction stands.
    """
    if not getattr(actor, "is_active", False) or getattr(actor, "role", None) != User.Role.ADMIN:
        raise PermissionDenied("Only an Admin can approve or reject a lateness-deduction revocation.")
    deduction = StaffLatenessDeduction.objects.select_for_update().get(pk=deduction.pk)
    if deduction.status != StaffLatenessDeduction.Status.REVOCATION_REQUESTED:
        raise ValidationError("This lateness deduction has no revocation request awaiting an Admin decision.")
    if deduction.revocation_requested_by_id and deduction.revocation_requested_by_id == actor.pk:
        raise PermissionDenied("The Admin who recorded the revocation request cannot also decide it.")
    note = str(decision_note or "").strip()
    if len(note) < 5:
        raise ValidationError({"decision_note": "Record why the Admin approved or rejected the revocation."})
    was_applied = deduction.applied_payroll_line_id is not None
    if approved:
        deduction.status = (
            StaffLatenessDeduction.Status.REIMBURSEMENT_PENDING if was_applied
            else StaffLatenessDeduction.Status.REVOKED
        )
    else:
        deduction.status = (
            StaffLatenessDeduction.Status.APPLIED if was_applied
            else StaffLatenessDeduction.Status.ACTIVE
        )
    deduction.decided_by = actor
    deduction.decided_at = timezone.now()
    deduction.decision_note = note[:1000]
    deduction.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    return deduction


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

    try:
        with transaction.atomic():
            period = PayrollPeriod.objects.create(
                idempotency_key=key,
                idempotency_fingerprint=fingerprint,
                starts_on=starts_on,
                ends_on=ends_on,
                department=(department or "")[:120],
                currency=currency,
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
    totals = {"gross": ZERO, "deductions": ZERO, "net": ZERO}
    for staff_id in user_ids:
        profile = profile_by_user[staff_id]
        compensation = compensation_by_user[staff_id]
        inputs = adjustments_by_staff.get(staff_id, {})
        hours = _hours(inputs.get("overtime_hours", 0), "overtime_hours")
        overtime_rate = Decimal(compensation.overtime_rate).quantize(CENT)
        overtime_pay = (hours * overtime_rate).quantize(CENT, rounding=ROUND_HALF_UP)
        allowances = _component_rows(inputs.get("allowances", []), "allowances")
        manual_deductions = _component_rows(inputs.get("deductions", []), "deductions")
        _validate_manual_deduction_labels(manual_deductions)

        # Automatic lateness: price this period's late clock-ins from the same
        # salary terms this run uses, then add back any Admin-approved
        # revocations whose original deduction already went through a payroll
        # run. A revocation still awaiting its original run's outcome stays
        # pending and is never added back early.
        sync_lateness_deductions(
            staff_id=staff_id, compensation=compensation, starts_on=starts_on, ends_on=ends_on,
        )
        lateness_reimbursements = []
        for reimbursement in (
            StaffLatenessDeduction.objects.select_for_update()
            .select_related("applied_payroll_line__period")
            .filter(
                staff_id=staff_id,
                status=StaffLatenessDeduction.Status.REIMBURSEMENT_PENDING,
                amount__gt=ZERO,
                currency=currency,
                reimbursement_payroll_line__isnull=True,
            )
            .order_by("business_date", "pk")
        ):
            original_status = (
                reimbursement.applied_payroll_line.period.status
                if reimbursement.applied_payroll_line_id else None
            )
            if original_status == PayrollPeriod.Status.REJECTED:
                # The original deduction never took effect, so there is
                # nothing to add back; the revocation simply cancels it.
                reimbursement.status = StaffLatenessDeduction.Status.REVOKED
                reimbursement.save(update_fields=["status", "updated_at"])
                continue
            if original_status not in {
                PayrollPeriod.Status.APPROVED, PayrollPeriod.Status.PARTIALLY_PAID, PayrollPeriod.Status.PAID,
            }:
                continue
            lateness_reimbursements.append(reimbursement)
            label = f"Lateness deduction reversal ({reimbursement.reference})"
            if not any(reimbursement.reference in str(row.get("label", "")) for row in allowances):
                allowances.append({
                    "label": label[:100],
                    "amount": str(Decimal(reimbursement.amount).quantize(CENT)),
                })

        bonus = _decimal(inputs.get("bonus", 0), "bonus")
        basic = Decimal(compensation.basic_salary).quantize(CENT)
        housing = Decimal(compensation.housing_allowance).quantize(CENT)
        transport = Decimal(compensation.transport_allowance).quantize(CENT)
        gross = (basic + housing + transport + overtime_pay + _components_total(allowances) + bonus).quantize(CENT)
        deductions = list(manual_deductions)

        from ..models import StaffDisciplinaryRecord

        approved_disciplinary = list(
            StaffDisciplinaryRecord.objects.select_for_update()
            .filter(
                staff_id=staff_id,
                deduction_approved=True,
                approved_deduction_amount__gt=ZERO,
                applied_payroll_line__isnull=True,
                status=StaffDisciplinaryRecord.Status.RESOLVED,
            )
            .exclude(appeal_status=StaffDisciplinaryRecord.AppealStatus.OVERTURNED)
            .filter(Q(effective_date__isnull=True) | Q(effective_date__lte=ends_on))
            .order_by("incident_at", "pk")
        )
        for disc in approved_disciplinary:
            disc_label = f"Approved disciplinary deduction ({disc.reference})"
            if not any(disc.reference in str(row.get("label", "")) for row in deductions):
                deductions.append({"label": disc_label[:100], "amount": str(disc.approved_deduction_amount.quantize(CENT))})

        approved_shortages = list(
            PayrollStockShortageAllocation.objects.select_for_update()
            .select_related("case")
            .filter(
                staff_id=staff_id,
                case__status=PayrollStockShortageCase.Status.APPROVED,
                case__effective_date__lte=ends_on,
                case__currency=currency,
                applied_payroll_line__isnull=True,
            )
            .order_by("case__effective_date", "case_id", "pk")
        )
        for allocation in approved_shortages:
            shortage_label = f"Department stock shortage ({allocation.case.reference})"
            if not any(allocation.case.reference in str(row.get("label", "")) for row in deductions):
                deductions.append({"label": shortage_label[:100], "amount": str(Decimal(allocation.amount).quantize(CENT))})

        lateness_deductions = list(
            StaffLatenessDeduction.objects.select_for_update()
            .filter(
                staff_id=staff_id,
                status=StaffLatenessDeduction.Status.ACTIVE,
                amount__gt=ZERO,
                currency=currency,
                applied_payroll_line__isnull=True,
            )
            .order_by("business_date", "pk")
        )
        for lateness in lateness_deductions:
            lateness_label = f"Lateness deduction ({lateness.reference})"
            if not any(lateness.reference in str(row.get("label", "")) for row in deductions):
                deductions.append({
                    "label": lateness_label[:100],
                    "amount": str(Decimal(lateness.amount).quantize(CENT)),
                })

        deductions_total = _components_total(deductions)
        net = (gross - deductions_total).quantize(CENT)
        if net < ZERO:
            raise ValidationError({"deductions": f"Internal deductions exceed gross pay for {profile.employee_code}."})
        line = PayrollLine.objects.create(
            period=period,
            staff=profile.user,
            compensation=compensation,
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
            deductions=deductions,
            gross_pay=gross,
            deductions_total=deductions_total,
            net_pay=net,
        )
        if approved_disciplinary:
            StaffDisciplinaryRecord.objects.filter(
                pk__in=[d.pk for d in approved_disciplinary]
            ).update(applied_payroll_line=line)
        if approved_shortages:
            PayrollStockShortageAllocation.objects.filter(
                pk__in=[a.pk for a in approved_shortages]
            ).update(applied_payroll_line=line)
        if lateness_deductions:
            StaffLatenessDeduction.objects.filter(
                pk__in=[d.pk for d in lateness_deductions]
            ).update(
                applied_payroll_line=line,
                applied_at=timezone.now(),
                status=StaffLatenessDeduction.Status.APPLIED,
            )
        if lateness_reimbursements:
            StaffLatenessDeduction.objects.filter(
                pk__in=[d.pk for d in lateness_reimbursements]
            ).update(
                reimbursement_payroll_line=line,
                reimbursed_at=timezone.now(),
                status=StaffLatenessDeduction.Status.REIMBURSED,
            )
        totals["gross"] += gross
        totals["deductions"] += deductions_total
        totals["net"] += net

    if totals["gross"] <= ZERO:
        raise ValidationError("Payroll total gross pay must be greater than zero.")
    if totals["net"] <= ZERO:
        raise ValidationError("Payroll total net pay must be greater than zero before it can be approved or settled.")
    period.total_gross = totals["gross"].quantize(CENT)
    period.total_deductions = totals["deductions"].quantize(CENT)
    period.total_net = totals["net"].quantize(CENT)
    period.total_paye = ZERO
    period.total_employer_pension = ZERO
    period.save(update_fields=["total_gross", "total_deductions", "total_net", "total_paye", "total_employer_pension", "updated_at"])
    _event(period, PayrollEvent.Type.CREATED, actor, {
        "line_count": len(user_ids), "replaces": replaces.reference if replaces else "",
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
    if not has_capability(reviewer, "payroll.approve"):
        raise PermissionDenied("This user cannot review payroll.")
    # One central maker-checker decision (apps.core.approvals): a separate
    # reviewer is mandatory for every ordinary role, and an administrator may
    # review a run they prepared under the configured override policy — with the
    # override captured on the append-only payroll event below.
    override = require_independent_approver(
        requester=period.created_by, actor=reviewer, workflow="payroll run",
    )

    now = timezone.now()
    period.reviewed_by = reviewer
    period.reviewed_at = now
    period.review_note = override_note((review_note or "")[:1000], override)
    if approved:
        other_deductions = Decimal(period.total_deductions).quantize(CENT)
        lines = [{
            "account_code": accounting.PAYROLL_EXPENSE,
            "direction": FinancialLine.Direction.DEBIT,
            "amount": period.total_gross,
            "description": f"Gross wages and compensation for {period.reference}",
        }]
        if period.total_net > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": period.total_net,
                "description": f"Net staff pay payable for {period.reference}",
            })
        if other_deductions > ZERO:
            lines.append({
                "account_code": accounting.PAYROLL_DEDUCTIONS_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": other_deductions,
                "description": f"Internal payroll deductions withheld for {period.reference}",
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
        _event(period, PayrollEvent.Type.APPROVED, reviewer, override_event_details({
            "financial_reference": transaction_record.reference,
            "gross": str(period.total_gross),
            "deductions": str(period.total_deductions),
            "net": str(period.total_net),
        }, override))
    else:
        period.status = PayrollPeriod.Status.REJECTED
        _event(period, PayrollEvent.Type.REJECTED, reviewer,
               override_event_details({"review_note": period.review_note}, override))
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
