"""Salary adjustments: partial-month proration, unpaid absence, disciplinary and other.

An adjustment is a controlled, approved instruction for ONE employee and ONE
payroll month. The payroll run for that month consumes approved adjustments
when it freezes its employee lines (``payroll_service.create_payroll_period``);
nothing here computes pay outside that run, posts to the ledger, or records a
payment. The employee's monthly salary terms (``StaffCompensation``) are never
modified: every record keeps its own snapshot of the terms it was calculated
from, so history stays readable after a later salary change.

All money is ``Decimal`` and rounded once, half-up, to the cent. Amounts sent by
a client are inputs only; the server recalculates every figure it stores.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.core.approvals import override_event_details, override_note, require_independent_approver
from apps.core.utils import hotel_today

from ..models import (
    LeaveRequest, PayrollLine, PayrollPeriod, PayrollSalaryAdjustment, PayrollSalaryAdjustmentEvent,
    ShiftAssignment, StaffCompensation, StaffProfile,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("9999999999.99")

Type = PayrollSalaryAdjustment.Type
Basis = PayrollSalaryAdjustment.Basis
Direction = PayrollSalaryAdjustment.Direction
Status = PayrollSalaryAdjustment.Status
EventType = PayrollSalaryAdjustmentEvent.Type

#: Categories an accountant can choose for a disciplinary deduction. They are
#: labels for the record only — no category carries an automatic amount.
VIOLATION_CATEGORIES = (
    ("ABSENTEEISM", "Unauthorised absence / abandonment of post"),
    ("LATENESS", "Persistent lateness"),
    ("MISCONDUCT", "Misconduct / breach of house rules"),
    ("NEGLIGENCE", "Negligence or damage to hotel property"),
    ("CASH_SHORTAGE", "Cash or stock shortage (individual responsibility)"),
    ("UNIFORM_GROOMING", "Uniform / grooming standards"),
    ("GUEST_COMPLAINT", "Verified guest complaint"),
    ("POLICY_BREACH", "Policy or procedure breach"),
    ("OTHER", "Other (describe in the reason)"),
)
VIOLATION_CATEGORY_CODES = {code for code, _ in VIOLATION_CATEGORIES}

#: Statuses that take part in the month's calculation summary. Drafts are an
#: intention only; rejected and reversed records are history.
EFFECTIVE_STATUSES = (Status.PENDING_APPROVAL, Status.APPROVED, Status.PAID)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _q(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value, label, *, maximum=MAX_AMOUNT) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({label: "A valid two-decimal amount is required."}) from exc
    if amount < ZERO:
        raise ValidationError({label: "Amount must not be negative."})
    if maximum is not None and amount > maximum:
        raise ValidationError({label: "Amount exceeds the supported maximum."})
    return amount


def _int_days(value, label, *, minimum=0, maximum=31, required=True):
    if value in (None, ""):
        if required:
            raise ValidationError({label: "This number of days is required for the selected adjustment type."})
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({label: "A whole number of days is required."}) from exc
    if days < minimum or days > maximum:
        raise ValidationError({label: f"Days must be between {minimum} and {maximum}."})
    return days


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def month_label(year: int, month: int) -> str:
    return f"{calendar.month_name[int(month)]} {year}"


def _person(user_or_profile, *, include_department=True) -> str:
    from .payroll_service import staff_display_name  # local import: payroll_service imports this module
    profile = user_or_profile
    if isinstance(user_or_profile, User):
        profile = getattr(user_or_profile, "staff_profile", None) or user_or_profile
    return staff_display_name(profile, include_department=include_department)


def _event(adjustment, event_type, actor, details=None):
    return PayrollSalaryAdjustmentEvent.objects.create(
        adjustment=adjustment, type=event_type, actor=actor, details=dict(details or {}),
    )


def validate_payroll_month(year, month):
    try:
        year, month = int(year), int(month)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"payroll_month": "A valid payroll month and year are required."}) from exc
    if not 1 <= month <= 12:
        raise ValidationError({"payroll_month": "Payroll month must be between 1 and 12."})
    today = hotel_today()
    if year < today.year - 5 or year > today.year + 1:
        raise ValidationError({"payroll_year": "Payroll year is outside the supported range."})
    # Up to one month ahead so a known mid-month exit can be prepared early;
    # anything later is almost certainly a typing error.
    next_year, next_month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    if (year, month) > (next_year, next_month):
        raise ValidationError({"payroll_month": "A salary adjustment can be recorded for the current month, a past month, or next month only."})
    return year, month


# ---------------------------------------------------------------------------
# Reads used by the preview/context endpoint and by validation
# ---------------------------------------------------------------------------
def compensation_for_month(staff_id, year, month):
    """Latest salary terms effective on or before the last day of the month."""
    _, last = month_bounds(year, month)
    return (
        StaffCompensation.objects.filter(staff_id=staff_id, effective_from__lte=last)
        .order_by("-effective_from", "-pk").first()
    )


def scheduled_working_days(staff_id, year, month) -> int:
    """Distinct hotel-local dates with a non-cancelled shift in the month (roster evidence)."""
    first, last = month_bounds(year, month)
    tz = timezone.get_current_timezone()
    days = set()
    for value in (
        ShiftAssignment.objects.filter(staff_id=staff_id)
        .exclude(status=ShiftAssignment.Status.CANCELLED)
        .filter(planned_start_at__date__gte=first, planned_start_at__date__lte=last)
        .values_list("planned_start_at", flat=True)
    ):
        local = timezone.localtime(value, tz) if timezone.is_aware(value) else value
        days.add(local.date())
    return len(days)


def approved_leave_days(staff_id, year, month) -> dict:
    """Approved leave days inside the month, split into paid and unpaid types.

    Approved paid leave is never an unpaid absence, so the service refuses an
    absence count that would have to include those days. Approved UNPAID leave
    is reported separately: an accountant may legitimately record it as an
    unpaid absence, but nothing is deducted automatically.
    """
    first, last = month_bounds(year, month)
    paid = unpaid = 0
    for leave in LeaveRequest.objects.filter(
        staff_id=staff_id, status=LeaveRequest.Status.APPROVED, start_date__lte=last, end_date__gte=first,
    ):
        start = max(leave.start_date, first)
        end = min(leave.end_date, last)
        overlap = (end - start).days + 1
        if overlap <= 0:
            continue
        if leave.type == LeaveRequest.Type.UNPAID:
            unpaid += overlap
        else:
            paid += overlap
    days_in_month = (last - first).days + 1
    return {"paid": min(paid, days_in_month), "unpaid": min(unpaid, days_in_month)}


def processed_run_for(staff_id, year, month):
    """The non-rejected payroll run that already includes this employee for the month, if any."""
    first, last = month_bounds(year, month)
    line = (
        PayrollLine.objects.select_related("period")
        .filter(staff_id=staff_id, period__starts_on__lte=last, period__ends_on__gte=first)
        .exclude(period__status=PayrollPeriod.Status.REJECTED)
        .order_by("-period__starts_on", "-pk").first()
    )
    return line.period if line else None


def eligible_staff(staff_id):
    """The staff account an adjustment may be recorded for, or a ValidationError."""
    staff = User.objects.select_related("staff_profile").filter(pk=staff_id).first()
    if staff is None or not staff.is_active:
        raise ValidationError({"staff_id": "Staff account not found or inactive."})
    if staff.is_superuser or staff.role in {User.Role.GUEST, User.Role.ADMIN}:
        raise ValidationError({"staff_id": "Salary adjustments can only be recorded for payroll-eligible staff."})
    profile = getattr(staff, "staff_profile", None)
    if profile is None:
        raise ValidationError({"staff_id": "This account has no staff profile and cannot be paid through payroll."})
    return staff


# ---------------------------------------------------------------------------
# The calculation engine (pure: no database writes)
# ---------------------------------------------------------------------------
@dataclass
class AdjustmentCalculation:
    adjustment_type: str
    calculation_basis: str
    currency: str
    monthly_basic: Decimal
    monthly_housing: Decimal
    monthly_transport: Decimal
    monthly_salary: Decimal
    calendar_days: int
    applicable_days: int
    days_worked: int | None
    absence_days: int | None
    daily_rate: Decimal
    prorated_basic: Decimal
    prorated_housing: Decimal
    prorated_transport: Decimal
    prorated_salary: Decimal
    absence_deduction: Decimal = ZERO
    disciplinary_deduction: Decimal = ZERO
    other_deduction: Decimal = ZERO
    additional_payment: Decimal = ZERO
    final_payable: Decimal = ZERO
    formula: str = ""
    fields: dict = field(default_factory=dict)

    def as_dict(self):
        data = {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in self.__dict__.items() if key != "fields"
        }
        data["total_deductions"] = str(self.total_deductions)
        return data

    @property
    def total_deductions(self) -> Decimal:
        return _q(self.absence_deduction + self.disciplinary_deduction + self.other_deduction)


def _applicable_days(calculation_basis, year, month, applicable_days):
    calendar_days = calendar.monthrange(year, month)[1]
    if calculation_basis == Basis.CALENDAR_DAYS:
        return calendar_days, calendar_days
    if calculation_basis == Basis.FIXED_30:
        return calendar_days, 30
    if calculation_basis == Basis.SCHEDULED_DAYS:
        days = _int_days(applicable_days, "applicable_days", minimum=1, maximum=31)
        return calendar_days, days
    raise ValidationError({"calculation_basis": "Choose calendar days, a fixed 30-day month, or scheduled working days."})


def _prorate(monthly_basic, monthly_housing, monthly_transport, monthly_salary, days, applicable):
    """Split a prorated total into components so the parts always add up exactly."""
    total = _q(monthly_salary * days / applicable)
    basic = _q(monthly_basic * days / applicable)
    housing = _q(monthly_housing * days / applicable)
    transport = _q(total - basic - housing)
    if transport < ZERO:  # cent-level rounding drift lands on basic, never on a negative component
        basic = _q(basic + transport)
        transport = ZERO
    return basic, housing, transport, total


def calculate_adjustment(
    *, compensation, adjustment_type, calculation_basis, payroll_year, payroll_month,
    days_worked=None, absence_days=None, applicable_days=None, amount=None, other_direction=None,
    additional_payment=None,
) -> AdjustmentCalculation:
    """Compute every stored figure from the salary terms and the day/amount inputs."""
    if adjustment_type not in Type.values:
        raise ValidationError({"adjustment_type": "Choose a supported adjustment type."})
    year, month = int(payroll_year), int(payroll_month)
    monthly_basic = _q(compensation.basic_salary)
    monthly_housing = _q(compensation.housing_allowance)
    monthly_transport = _q(compensation.transport_allowance)
    monthly_salary = _q(monthly_basic + monthly_housing + monthly_transport)
    calendar_days, applicable = _applicable_days(calculation_basis, year, month, applicable_days)
    daily_rate = _q(monthly_salary / applicable)
    extra = _decimal(additional_payment, "additional_payment")

    calc = AdjustmentCalculation(
        adjustment_type=adjustment_type, calculation_basis=calculation_basis,
        currency=str(compensation.currency or "NGN").upper(),
        monthly_basic=monthly_basic, monthly_housing=monthly_housing, monthly_transport=monthly_transport,
        monthly_salary=monthly_salary, calendar_days=calendar_days, applicable_days=applicable,
        days_worked=None, absence_days=None, daily_rate=daily_rate,
        prorated_basic=monthly_basic, prorated_housing=monthly_housing, prorated_transport=monthly_transport,
        prorated_salary=monthly_salary, additional_payment=extra,
    )

    if adjustment_type == Type.PRORATED:
        worked = _int_days(days_worked, "days_worked", minimum=1, maximum=applicable)
        calc.days_worked = worked
        (calc.prorated_basic, calc.prorated_housing, calc.prorated_transport, calc.prorated_salary) = _prorate(
            monthly_basic, monthly_housing, monthly_transport, monthly_salary, worked, applicable,
        )
        calc.formula = f"{monthly_salary} ÷ {applicable} × {worked} = {calc.prorated_salary}"
    elif adjustment_type == Type.ABSENCE:
        absent = _int_days(absence_days, "absence_days", minimum=1, maximum=applicable)
        calc.absence_days = absent
        calc.absence_deduction = _q(monthly_salary * absent / applicable)
        calc.formula = f"{monthly_salary} ÷ {applicable} × {absent} = {calc.absence_deduction} deducted"
    elif adjustment_type == Type.DISCIPLINARY:
        value = _decimal(amount, "amount")
        if value <= ZERO:
            raise ValidationError({"amount": "Enter the disciplinary deduction amount."})
        calc.disciplinary_deduction = value
        calc.formula = f"Manual disciplinary deduction {value}"
    else:  # OTHER
        value = _decimal(amount, "amount")
        if value <= ZERO:
            raise ValidationError({"amount": "Enter the adjustment amount."})
        if other_direction not in Direction.values:
            raise ValidationError({"other_direction": "Choose whether the amount is a deduction or an additional payment."})
        if other_direction == Direction.DEDUCTION:
            calc.other_deduction = value
            calc.formula = f"Other deduction {value}"
        else:
            calc.additional_payment = _q(calc.additional_payment + value)
            calc.formula = f"Other additional payment {value}"

    calc.final_payable = _q(calc.prorated_salary - calc.total_deductions + calc.additional_payment)
    if calc.total_deductions > calc.prorated_salary:
        raise ValidationError({
            "amount": f"Deductions ({calc.total_deductions}) exceed the eligible salary for the month ({calc.prorated_salary}). "
                      "Reduce the deduction or spread it over later months; no automatic carry-over is applied.",
        })
    if calc.final_payable < ZERO:
        raise ValidationError({"amount": "The final payable salary cannot be negative."})
    return calc


def month_summary(*, staff_id, payroll_year, payroll_month, extra: AdjustmentCalculation | None = None,
                  exclude_pk=None, statuses=EFFECTIVE_STATUSES, strict=False, monthly_salary=None, currency=None):
    """The month's calculation summary across every effective adjustment (plus ``extra``).

    Normal monthly salary, prorated salary, absence / disciplinary / other
    deductions, additional payments and the final payable salary — exactly
    the figures the payroll run will apply to this employee's line. With
    ``strict`` the aggregate is validated (no negative pay, deductions never
    above the eligible salary).
    """
    rows = list(
        PayrollSalaryAdjustment.objects.filter(
            staff_id=staff_id, payroll_year=payroll_year, payroll_month=payroll_month, status__in=statuses,
        ).exclude(pk=exclude_pk).order_by("created_at", "pk")
    )
    prorated_row = next((r for r in rows if r.adjustment_type == Type.PRORATED), None)
    if monthly_salary is None:
        source = extra or prorated_row or (rows[0] if rows else None)
        monthly_salary = _q(source.monthly_salary) if source is not None else None
    if currency is None:
        source = extra or (rows[0] if rows else None)
        currency = source.currency if source is not None else "NGN"
    prorated = None
    if extra is not None and extra.adjustment_type == Type.PRORATED:
        prorated = extra.prorated_salary
    elif prorated_row is not None:
        prorated = _q(prorated_row.prorated_salary)
    elif monthly_salary is not None:
        prorated = monthly_salary
    absence = sum((_q(r.absence_deduction) for r in rows), ZERO)
    disciplinary = sum((_q(r.disciplinary_deduction) for r in rows), ZERO)
    other = sum((_q(r.other_deduction) for r in rows), ZERO)
    additional = sum((_q(r.additional_payment) for r in rows), ZERO)
    if extra is not None:
        absence += extra.absence_deduction
        disciplinary += extra.disciplinary_deduction
        other += extra.other_deduction
        additional += extra.additional_payment
    total_deductions = _q(absence + disciplinary + other)
    final = _q((prorated or ZERO) - total_deductions + additional) if prorated is not None else None
    summary = {
        "currency": currency,
        "normal_monthly_salary": str(monthly_salary) if monthly_salary is not None else None,
        "prorated_salary": str(prorated) if prorated is not None else None,
        "absence_deduction": str(_q(absence)),
        "disciplinary_deduction": str(_q(disciplinary)),
        "other_deduction": str(_q(other)),
        "total_deductions": str(total_deductions),
        "additional_payment": str(_q(additional)),
        "final_payable_salary": str(final) if final is not None else None,
        "record_count": len(rows) + (1 if extra is not None else 0),
        "references": [r.reference for r in rows],
    }
    if strict and prorated is not None:
        if total_deductions > prorated:
            raise ValidationError({
                "amount": f"Together with the month's other adjustments ({', '.join(summary['references']) or 'none'}), deductions "
                          f"({total_deductions}) would exceed the eligible salary ({prorated}) for {month_label(payroll_year, payroll_month)}.",
            })
        if final is not None and final < ZERO:
            raise ValidationError({"amount": "The combined adjustments would make the final payable salary negative."})
    return summary


# ---------------------------------------------------------------------------
# Context for the UI (auto-loaded when a staff member is selected)
# ---------------------------------------------------------------------------
def adjustment_context(*, staff, payroll_year, payroll_month):
    year, month = validate_payroll_month(payroll_year, payroll_month)
    profile = getattr(staff, "staff_profile", None)
    compensation = compensation_for_month(staff.pk, year, month)
    first, last = month_bounds(year, month)
    leave = approved_leave_days(staff.pk, year, month)
    run = processed_run_for(staff.pk, year, month)
    existing = list(
        PayrollSalaryAdjustment.objects.filter(staff_id=staff.pk, payroll_year=year, payroll_month=month)
        .order_by("-created_at", "-pk")
    )
    context = {
        "staff": {
            "id": staff.pk, "staff_id": staff.pk, "full_name": staff.full_name,
            "employee_code": getattr(profile, "employee_code", ""), "department": getattr(profile, "department", ""),
        },
        "payroll_year": year, "payroll_month": month, "month_label": month_label(year, month),
        "calendar_days": (last - first).days + 1,
        "fixed_days": 30,
        "scheduled_working_days": scheduled_working_days(staff.pk, year, month),
        "approved_paid_leave_days": leave["paid"],
        "approved_unpaid_leave_days": leave["unpaid"],
        "processed_run": {"reference": run.reference, "status": run.status} if run else None,
        "compensation": None,
        "existing_adjustments": [
            {
                "reference": r.reference, "adjustment_type": r.adjustment_type, "status": r.status,
                "final_payable": str(r.final_payable), "total_deductions": str(r.total_deductions),
                "additional_payment": str(r.additional_payment), "created_at": r.created_at,
            }
            for r in existing
        ],
        "summary": None,
        "violation_categories": [{"value": code, "label": label} for code, label in VIOLATION_CATEGORIES],
    }
    if compensation is not None:
        monthly_salary = _q(_q(compensation.basic_salary) + _q(compensation.housing_allowance) + _q(compensation.transport_allowance))
        context["compensation"] = {
            "id": compensation.pk, "effective_from": compensation.effective_from, "currency": compensation.currency,
            "basic_salary": str(_q(compensation.basic_salary)), "housing_allowance": str(_q(compensation.housing_allowance)),
            "transport_allowance": str(_q(compensation.transport_allowance)), "monthly_salary": str(monthly_salary),
            "daily_rate_calendar": str(_q(monthly_salary / context["calendar_days"])),
            "daily_rate_fixed_30": str(_q(monthly_salary / 30)),
        }
        context["summary"] = month_summary(
            staff_id=staff.pk, payroll_year=year, payroll_month=month,
            monthly_salary=monthly_salary, currency=compensation.currency,
        )
    return context


# ---------------------------------------------------------------------------
# Recording and approval
# ---------------------------------------------------------------------------
def _clean_inputs(data: dict) -> dict:
    cleaned = {
        "reason": str(data.get("reason") or "").strip()[:2000],
        "violation_category": str(data.get("violation_category") or "").strip().upper()[:60],
        "incident_date": data.get("incident_date"),
        "evidence_reference": str(data.get("evidence_reference") or "").strip()[:160],
        "additional_payment_reason": str(data.get("additional_payment_reason") or "").strip()[:500],
        "notes": str(data.get("notes") or "").strip()[:2000],
    }
    return cleaned


def _validate_justification(adjustment_type, calc: AdjustmentCalculation, cleaned: dict):
    errors = {}
    needs_reason = adjustment_type in {Type.ABSENCE, Type.DISCIPLINARY, Type.OTHER}
    if needs_reason and len(cleaned["reason"]) < 5:
        errors["reason"] = "A reason of at least five characters is required for a deduction or other adjustment."
    if adjustment_type == Type.DISCIPLINARY:
        if cleaned["violation_category"] not in VIOLATION_CATEGORY_CODES:
            errors["violation_category"] = "Choose the rule-violation category."
        if not cleaned["incident_date"]:
            errors["incident_date"] = "Record the incident date."
        elif cleaned["incident_date"] > hotel_today():
            errors["incident_date"] = "The incident date cannot be in the future."
        if not cleaned["evidence_reference"] and not cleaned["notes"]:
            errors["evidence_reference"] = "Record the evidence reference or remarks supporting the deduction."
    else:
        cleaned["violation_category"] = ""
    if calc.additional_payment > ZERO and adjustment_type != Type.OTHER and len(cleaned["additional_payment_reason"]) < 3:
        errors["additional_payment_reason"] = "Explain the additional payment."
    if errors:
        raise ValidationError(errors)


def _duplicate_guard(staff_id, year, month, adjustment_type, cleaned, *, exclude_pk=None):
    probe = PayrollSalaryAdjustment(
        staff_id=staff_id, payroll_year=year, payroll_month=month, adjustment_type=adjustment_type,
        violation_category=cleaned.get("violation_category", ""), incident_date=cleaned.get("incident_date"),
    )
    key = probe.build_active_key()
    clash = PayrollSalaryAdjustment.objects.filter(active_key=key).exclude(pk=exclude_pk).first()
    if clash:
        raise ValidationError({
            "adjustment_type": f"{clash.get_adjustment_type_display()} for {month_label(year, month)} already exists "
                               f"({clash.reference}, {clash.get_status_display()}). Reject or reverse it before recording another.",
        })
    return key


def _require_month_open(staff, year, month):
    run = processed_run_for(staff.pk, year, month)
    if run is not None:
        raise ValidationError({
            "payroll_month": f"Payroll run {run.reference} ({run.get_status_display()}) already includes "
                             f"{_person(staff)} for {month_label(year, month)}. Record a correction for a later month instead; "
                             "approved payroll history is never recalculated.",
        })


@transaction.atomic
def record_salary_adjustment(*, actor, staff_id, payroll_year, payroll_month, adjustment_type, calculation_basis=Basis.CALENDAR_DAYS,
                             days_worked=None, absence_days=None, applicable_days=None, amount=None, other_direction=None,
                             additional_payment=None, submit=True, idempotency_key="", **justification):
    """Record (and by default submit for approval) one salary adjustment. Returns ``(record, created)``."""
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot record salary adjustments.")
    key = str(idempotency_key or "").strip()[:128] or None
    if key:
        existing = PayrollSalaryAdjustment.objects.select_for_update().filter(idempotency_key=key).first()
        if existing:
            if (existing.staff_id, existing.payroll_year, existing.payroll_month, existing.adjustment_type) != (
                int(staff_id), int(payroll_year), int(payroll_month), adjustment_type,
            ):
                raise ValidationError({"idempotency_key": "This key was already used for a different salary adjustment."})
            return existing, False
    year, month = validate_payroll_month(payroll_year, payroll_month)
    staff = eligible_staff(staff_id)
    compensation = compensation_for_month(staff.pk, year, month)
    if compensation is None:
        raise ValidationError({"staff_id": f"No salary terms are effective for {_person(staff)} in {month_label(year, month)}; record compensation first."})
    _require_month_open(staff, year, month)
    cleaned = _clean_inputs(justification)
    calc = calculate_adjustment(
        compensation=compensation, adjustment_type=adjustment_type, calculation_basis=calculation_basis,
        payroll_year=year, payroll_month=month, days_worked=days_worked, absence_days=absence_days,
        applicable_days=applicable_days, amount=amount, other_direction=other_direction, additional_payment=additional_payment,
    )
    _validate_justification(adjustment_type, calc, cleaned)
    if adjustment_type == Type.ABSENCE:
        leave = approved_leave_days(staff.pk, year, month)
        if calc.absence_days > calc.calendar_days - leave["paid"]:
            raise ValidationError({
                "absence_days": f"{_person(staff)} has {leave['paid']} approved leave day(s) in {month_label(year, month)}; "
                                "approved leave is never an unpaid absence, so at most "
                                f"{calc.calendar_days - leave['paid']} unpaid absence day(s) can be recorded.",
            })
    _duplicate_guard(staff.pk, year, month, adjustment_type, cleaned)
    month_summary(staff_id=staff.pk, payroll_year=year, payroll_month=month, extra=calc, strict=True)

    now = timezone.now()
    status = Status.PENDING_APPROVAL if submit else Status.DRAFT
    try:
        with transaction.atomic():
            record = PayrollSalaryAdjustment.objects.create(
                idempotency_key=key, staff=staff, compensation=compensation,
                payroll_year=year, payroll_month=month, adjustment_type=adjustment_type,
                calculation_basis=calculation_basis, currency=calc.currency,
                monthly_basic=calc.monthly_basic, monthly_housing=calc.monthly_housing,
                monthly_transport=calc.monthly_transport, monthly_salary=calc.monthly_salary,
                applicable_days=calc.applicable_days, days_worked=calc.days_worked, absence_days=calc.absence_days,
                daily_rate=calc.daily_rate, prorated_basic=calc.prorated_basic, prorated_housing=calc.prorated_housing,
                prorated_transport=calc.prorated_transport, prorated_salary=calc.prorated_salary,
                absence_deduction=calc.absence_deduction, disciplinary_deduction=calc.disciplinary_deduction,
                other_deduction=calc.other_deduction, additional_payment=calc.additional_payment,
                final_payable=calc.final_payable, status=status, created_by=actor,
                submitted_at=now if submit else None, **cleaned,
            )
    except IntegrityError:
        _duplicate_guard(staff.pk, year, month, adjustment_type, cleaned)
        raise
    _event(record, EventType.CREATED, actor, {"formula": calc.formula, "final_payable": str(calc.final_payable), "status": status})
    if submit:
        _event(record, EventType.SUBMITTED, actor, {})
    return record, True


@transaction.atomic
def submit_salary_adjustment(*, adjustment, actor):
    if not has_capability(actor, "payroll.manage"):
        raise PermissionDenied("This user cannot submit salary adjustments.")
    adjustment = PayrollSalaryAdjustment.objects.select_for_update().select_related("staff__staff_profile").get(pk=adjustment.pk)
    if adjustment.status == Status.PENDING_APPROVAL:
        return adjustment
    if adjustment.status != Status.DRAFT:
        raise ValidationError(f"A salary adjustment cannot be submitted from {adjustment.get_status_display()}.")
    _require_month_open(adjustment.staff, adjustment.payroll_year, adjustment.payroll_month)
    month_summary(
        staff_id=adjustment.staff_id, payroll_year=adjustment.payroll_year, payroll_month=adjustment.payroll_month,
        extra=_calc_from_record(adjustment), exclude_pk=adjustment.pk, strict=True,
    )
    adjustment.status = Status.PENDING_APPROVAL
    adjustment.submitted_at = timezone.now()
    adjustment.save(update_fields=["status", "submitted_at", "active_key", "updated_at"])
    _event(adjustment, EventType.SUBMITTED, actor, {})
    return adjustment


def _calc_from_record(record) -> AdjustmentCalculation:
    return AdjustmentCalculation(
        adjustment_type=record.adjustment_type, calculation_basis=record.calculation_basis, currency=record.currency,
        monthly_basic=_q(record.monthly_basic), monthly_housing=_q(record.monthly_housing),
        monthly_transport=_q(record.monthly_transport), monthly_salary=_q(record.monthly_salary),
        calendar_days=calendar.monthrange(record.payroll_year, record.payroll_month)[1],
        applicable_days=record.applicable_days, days_worked=record.days_worked, absence_days=record.absence_days,
        daily_rate=_q(record.daily_rate), prorated_basic=_q(record.prorated_basic), prorated_housing=_q(record.prorated_housing),
        prorated_transport=_q(record.prorated_transport), prorated_salary=_q(record.prorated_salary),
        absence_deduction=_q(record.absence_deduction), disciplinary_deduction=_q(record.disciplinary_deduction),
        other_deduction=_q(record.other_deduction), additional_payment=_q(record.additional_payment),
        final_payable=_q(record.final_payable),
    )


@transaction.atomic
def decide_salary_adjustment(*, adjustment, actor, approved: bool, decision_note=""):
    """Independent approval or rejection. Approval never pays or posts anything."""
    if not has_capability(actor, "payroll.approve"):
        raise PermissionDenied("This user cannot approve salary adjustments.")
    adjustment = PayrollSalaryAdjustment.objects.select_for_update().select_related("staff__staff_profile", "created_by").get(pk=adjustment.pk)
    expected = Status.APPROVED if approved else Status.REJECTED
    if adjustment.status in {Status.APPROVED, Status.REJECTED, Status.PAID, Status.REVERSED}:
        if adjustment.status == expected:
            return adjustment
        raise ValidationError("A final decision on this salary adjustment cannot be changed.")
    if adjustment.status != Status.PENDING_APPROVAL:
        raise ValidationError(f"A salary adjustment cannot be decided from {adjustment.get_status_display()}.")
    note = str(decision_note or "").strip()
    if len(note) < 5:
        raise ValidationError({"decision_note": "Record why the adjustment was approved or rejected (at least 5 characters)."})
    override = require_independent_approver(requester=adjustment.created_by, actor=actor, workflow="salary adjustment")
    if approved:
        _require_month_open(adjustment.staff, adjustment.payroll_year, adjustment.payroll_month)
        month_summary(
            staff_id=adjustment.staff_id, payroll_year=adjustment.payroll_year, payroll_month=adjustment.payroll_month,
            extra=_calc_from_record(adjustment), exclude_pk=adjustment.pk, statuses=(Status.APPROVED, Status.PAID), strict=True,
        )
    adjustment.status = expected
    adjustment.decided_by = actor
    adjustment.decided_at = timezone.now()
    adjustment.decision_note = override_note(note[:1000], override)
    adjustment.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "active_key", "updated_at"])
    _event(adjustment, EventType.APPROVED if approved else EventType.REJECTED, actor,
           override_event_details({"decision_note": adjustment.decision_note, "final_payable": str(adjustment.final_payable)}, override))
    return adjustment


@transaction.atomic
def reverse_salary_adjustment(*, adjustment, actor, reason=""):
    """Take an adjustment out of effect with an audited reason; the record itself is kept.

    * A draft or pending record can be withdrawn by a payroll officer.
    * An approved record needs the approval capability and must not already
      be inside a live payroll run — once a run has frozen it, the correction
      belongs to payroll (reject the run, or record a later-month adjustment).
    * Paid records are never reversed here; a salary-payment reversal reopens them.
    """
    adjustment = PayrollSalaryAdjustment.objects.select_for_update().select_related("applied_payroll_line__period").get(pk=adjustment.pk)
    reason = str(reason or "").strip()
    if len(reason) < 5:
        raise ValidationError({"reason": "Record why the adjustment is being reversed (at least 5 characters)."})
    if adjustment.status in {Status.DRAFT, Status.PENDING_APPROVAL}:
        if not has_capability(actor, "payroll.manage"):
            raise PermissionDenied("This user cannot withdraw salary adjustments.")
    elif adjustment.status == Status.APPROVED:
        if not has_capability(actor, "payroll.approve"):
            raise PermissionDenied("Only an authorised approver can reverse an approved salary adjustment.")
        line = adjustment.applied_payroll_line
        if line is not None and line.period.status != PayrollPeriod.Status.REJECTED:
            raise ValidationError(
                f"This adjustment is already included in payroll run {line.period.reference} ({line.period.get_status_display()}). "
                "Correct it through payroll: reject the run before approval, or record a later-month adjustment."
            )
    else:
        raise ValidationError(f"A salary adjustment cannot be reversed from {adjustment.get_status_display()}.")
    previous = adjustment.status
    adjustment.status = Status.REVERSED
    adjustment.reversed_by = actor
    adjustment.reversed_at = timezone.now()
    adjustment.reversal_reason = reason[:2000]
    adjustment.save(update_fields=["status", "reversed_by", "reversed_at", "reversal_reason", "active_key", "updated_at"])
    _event(adjustment, EventType.REVERSED, actor, {"reason": adjustment.reversal_reason, "previous_status": previous})
    return adjustment


# ---------------------------------------------------------------------------
# Payroll-run integration (called only from payroll_service)
# ---------------------------------------------------------------------------
@dataclass
class SalaryAdjustmentBundle:
    basic: Decimal
    housing: Decimal
    transport: Decimal
    allowances: list = field(default_factory=list)
    deductions: list = field(default_factory=list)
    adjustments: list = field(default_factory=list)
    prorated: bool = False


def consume_salary_adjustments(*, staff_id, profile, compensation, starts_on, ends_on, currency) -> SalaryAdjustmentBundle:
    """Lock and return the approved adjustments the run must apply to this employee.

    Rules: an adjustment belongs to the run that covers its payroll month; a
    pending adjustment blocks the run (so a full salary is never frozen while a
    reduction awaits a decision); and the snapshot must still match the salary
    terms the run is using. Nothing is written here — the caller links the
    records to the frozen line with :func:`mark_salary_adjustments_applied`.
    """
    basic = _q(compensation.basic_salary)
    housing = _q(compensation.housing_allowance)
    transport = _q(compensation.transport_allowance)
    bundle = SalaryAdjustmentBundle(basic=basic, housing=housing, transport=transport)
    candidates = list(
        PayrollSalaryAdjustment.objects.select_for_update()
        .filter(staff_id=staff_id, status__in=[Status.PENDING_APPROVAL, Status.APPROVED], applied_payroll_line__isnull=True)
        .order_by("created_at", "pk")
    )
    candidates = [
        c for c in candidates
        if month_bounds(c.payroll_year, c.payroll_month)[0] <= ends_on and month_bounds(c.payroll_year, c.payroll_month)[1] >= starts_on
    ]
    if not candidates:
        return bundle
    who = _person(profile)
    pending = [c for c in candidates if c.status == Status.PENDING_APPROVAL]
    if pending:
        refs = ", ".join(c.reference for c in pending)
        raise ValidationError({"adjustments": f"Salary adjustment {refs} for {who} is awaiting approval; approve or reject it before preparing this payroll run."})
    if (starts_on.year, starts_on.month) != (ends_on.year, ends_on.month):
        raise ValidationError({"starts_on": f"Approved salary adjustments for {who} apply to one payroll month; this run spans two months — split it at the month boundary."})
    for record in candidates:
        label = month_label(record.payroll_year, record.payroll_month)
        if record.currency.upper() != str(currency).upper():
            raise ValidationError({"adjustments": f"Salary adjustment {record.reference} for {who} is in {record.currency}, not {currency}."})
        if (_q(record.monthly_basic), _q(record.monthly_housing), _q(record.monthly_transport)) != (basic, housing, transport):
            raise ValidationError({
                "adjustments": f"Salary terms for {who} changed after adjustment {record.reference} ({label}) was recorded; "
                               "reverse it and record it again from the current terms.",
            })
        if record.adjustment_type == Type.PRORATED:
            bundle.basic, bundle.housing, bundle.transport = _q(record.prorated_basic), _q(record.prorated_housing), _q(record.prorated_transport)
            bundle.prorated = True
            if record.additional_payment > ZERO:
                bundle.allowances.append({"label": f"Additional payment ({record.reference})"[:100], "amount": str(_q(record.additional_payment))})
        elif record.adjustment_type == Type.ABSENCE:
            bundle.deductions.append({
                "label": f"Unpaid absence deduction · {record.absence_days} day(s) ({record.reference})"[:100],
                "amount": str(_q(record.absence_deduction)),
            })
            if record.additional_payment > ZERO:
                bundle.allowances.append({"label": f"Additional payment ({record.reference})"[:100], "amount": str(_q(record.additional_payment))})
        elif record.adjustment_type == Type.DISCIPLINARY:
            bundle.deductions.append({
                "label": f"Disciplinary deduction · {record.violation_category.replace('_', ' ').title()} ({record.reference})"[:100],
                "amount": str(_q(record.disciplinary_deduction)),
            })
            if record.additional_payment > ZERO:
                bundle.allowances.append({"label": f"Additional payment ({record.reference})"[:100], "amount": str(_q(record.additional_payment))})
        else:
            if record.other_deduction > ZERO:
                bundle.deductions.append({"label": f"Salary adjustment deduction ({record.reference})"[:100], "amount": str(_q(record.other_deduction))})
            if record.additional_payment > ZERO:
                bundle.allowances.append({"label": f"Salary adjustment addition ({record.reference})"[:100], "amount": str(_q(record.additional_payment))})
        bundle.adjustments.append(record)
    return bundle


def mark_salary_adjustments_applied(*, adjustments, line, actor):
    if not adjustments:
        return
    now = timezone.now()
    PayrollSalaryAdjustment.objects.filter(pk__in=[a.pk for a in adjustments]).update(applied_payroll_line=line, applied_at=now, updated_at=now)
    for record in adjustments:
        _event(record, EventType.APPLIED, actor, {"payroll_period": line.period.reference, "payroll_line_id": line.pk})


def release_salary_adjustments_for_period(*, period, actor):
    """A rejected run never took effect: free its adjustments for the replacement run."""
    now = timezone.now()
    records = list(
        PayrollSalaryAdjustment.objects.select_for_update()
        .filter(applied_payroll_line__period_id=period.pk, status=Status.APPROVED)
    )
    if not records:
        return 0
    PayrollSalaryAdjustment.objects.filter(pk__in=[r.pk for r in records]).update(applied_payroll_line=None, applied_at=None, updated_at=now)
    for record in records:
        _event(record, EventType.RELEASED, actor, {"payroll_period": period.reference})
    return len(records)


def sync_salary_adjustment_payment_status(*, line=None, period=None, actor=None):
    """Mirror the employee line's settlement onto its adjustments (APPROVED <-> PAID).

    Derived purely from the immutable salary-payment rows, so a reopened or
    refreshed run can never leave an adjustment marked paid without the money
    having been recorded as paid.
    """
    from .payroll_service import _effective_salary_paid  # local import: payroll_service imports this module
    lines = [line] if line is not None else list(PayrollLine.objects.filter(period_id=period.pk))
    now = timezone.now()
    for current in lines:
        records = list(
            PayrollSalaryAdjustment.objects.select_for_update()
            .filter(applied_payroll_line_id=current.pk, status__in=[Status.APPROVED, Status.PAID])
        )
        if not records:
            continue
        paid = _effective_salary_paid(line=current)
        fully_paid = Decimal(current.net_pay).quantize(CENT) > ZERO and paid >= Decimal(current.net_pay).quantize(CENT)
        for record in records:
            if fully_paid and record.status == Status.APPROVED:
                record.status = Status.PAID
                record.paid_at = now
                record.save(update_fields=["status", "paid_at", "active_key", "updated_at"])
                _event(record, EventType.PAID, actor, {"payroll_period": current.period.reference, "salary_paid": str(paid)})
            elif not fully_paid and record.status == Status.PAID:
                record.status = Status.APPROVED
                record.paid_at = None
                record.save(update_fields=["status", "paid_at", "active_key", "updated_at"])
                _event(record, EventType.PAYMENT_REVERSED, actor, {"payroll_period": current.period.reference, "salary_paid": str(paid)})
