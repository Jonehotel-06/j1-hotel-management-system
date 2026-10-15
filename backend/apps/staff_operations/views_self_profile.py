"""Authenticated staff self-service profile (owner-scoped, read-only).

This is deliberately separate from ``StaffComprehensiveProfileView`` (the
manager/HR 10-tab record, gated by ``staff.view``/``staff.profile.manage``).
Here the employee is resolved *only* from ``request.user``: no staff id,
user id or object id supplied by the browser can widen the scope, and every
collection below is filtered to that one employee. The endpoint is GET-only
— salary, disciplinary outcomes, roster and review records stay writable
solely through their existing controlled workflows.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import UserSerializer
from apps.core.responses import success_response
from apps.core.utils import hotel_today

from .models import (
    AttendanceRecord, LeaveRequest, PayrollLine, PayrollPeriod, PayrollSalaryPayment,
    ShiftAssignment, StaffCompensation, StaffDisciplinaryRecord, StaffDocument,
    StaffLatenessDeduction, StaffPerformanceReview, StaffProfile,
)
from .payroll_serializers import MyPayslipSerializer
from .serializers import (
    AttendanceRecordListSerializer, LeaveRequestListSerializer, ShiftAssignmentListSerializer,
    StaffPerformanceReviewSerializer, StaffProfileSerializer,
)
from .services.staff_operations_service import ensure_staff_profile

MAX_ROWS = 50


def _parse_date(request, name):
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError({name: "Use an ISO date (YYYY-MM-DD)."}) from exc


def _disciplinary_for_employee(record: StaffDisciplinaryRecord) -> dict:
    """Employee-safe disciplinary projection.

    Pending matters are labelled as allegations under review — never as
    proven offences — and investigation internals (evidence notes, audit
    trail, reporter identity beyond a display name) are not exposed. A
    deduction amount is shown only once formally approved and recorded.
    """
    confirmed = record.status in {
        StaffDisciplinaryRecord.Status.RESOLVED,
        StaffDisciplinaryRecord.Status.CLOSED,
    } and record.disciplinary_action not in {
        StaffDisciplinaryRecord.ActionType.NONE,
        StaffDisciplinaryRecord.ActionType.EXONERATED,
    }
    return {
        "reference": record.reference,
        "incident_at": record.incident_at,
        "category": record.category,
        "summary": record.allegation_summary,
        "description": record.allegation_detail,
        "reported_by_name": record.reported_by.full_name if record.reported_by else "",
        "status": record.status,
        "finding": "CONFIRMED" if confirmed else ("DISMISSED" if record.status == StaffDisciplinaryRecord.Status.DISMISSED else "PENDING"),
        "investigation_outcome": record.investigation_outcome if record.status not in {
            StaffDisciplinaryRecord.Status.OPEN, StaffDisciplinaryRecord.Status.UNDER_REVIEW,
        } else "",
        "disciplinary_action": record.disciplinary_action,
        "warning_level": record.warning_level,
        "approved_deduction_amount": str(record.approved_deduction_amount) if record.deduction_approved else None,
        "deduction_approved": record.deduction_approved,
        "effective_date": record.effective_date,
        "employee_response": record.employee_response,
        "appeal_status": record.appeal_status,
        "finalized_at": record.reviewed_at,
    }


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    first = _month_start(value)
    return date(first.year + (first.month == 12), first.month % 12 + 1, 1)


def _month_label(value: date) -> str:
    return value.strftime("%B %Y")


def _build_monthly_work_history(*, user, today, salary_by_month):
    """Monthly worked / scheduled / missed / approved-leave day counts.

    A *worked* day is a distinct attendance ``business_date``. A *missed* day
    is a distinct, non-cancelled scheduled-shift date that has already started,
    has no attendance record, and is not covered by approved leave. Approved
    leave days are counted only when the employee did not also attend that
    date, so the three categories never double-count a calendar day.
    """
    worked_dates = defaultdict(set)
    worked_minutes = defaultdict(int)
    for row in AttendanceRecord.objects.filter(staff=user).values("business_date", "worked_minutes"):
        business_date = row["business_date"]
        if business_date is None:
            continue
        worked_dates[business_date.strftime("%Y-%m")].add(business_date)
        worked_minutes[business_date.strftime("%Y-%m")] += int(row["worked_minutes"] or 0)

    scheduled_dates = defaultdict(set)
    scheduled_started_dates = defaultdict(set)
    now = timezone.now()
    shift_rows = (
        ShiftAssignment.objects.filter(staff=user)
        .exclude(status=ShiftAssignment.Status.CANCELLED)
        .values("planned_start_at")
    )
    for row in shift_rows:
        planned_start = row["planned_start_at"]
        if planned_start is None:
            continue
        local_start = timezone.localtime(planned_start)
        planned_date = local_start.date()
        key = planned_date.strftime("%Y-%m")
        scheduled_dates[key].add(planned_date)
        if planned_date < today or (planned_date == today and local_start <= now):
            scheduled_started_dates[key].add(planned_date)

    leave_dates = defaultdict(set)
    approved_leave = LeaveRequest.objects.filter(
        staff=user, status=LeaveRequest.Status.APPROVED,
    ).values("start_date", "end_date")
    for row in approved_leave:
        start_date, end_date = row["start_date"], row["end_date"]
        if start_date is None or end_date is None or end_date < start_date:
            continue
        cursor = start_date
        # A single leave request cannot legitimately span years; the bound
        # also protects this read-only profile from pathological legacy rows.
        for _ in range(3660):
            if cursor > end_date:
                break
            if cursor <= today:
                leave_dates[cursor.strftime("%Y-%m")].add(cursor)
            cursor += timedelta(days=1)

    current_key = today.strftime("%Y-%m")
    month_keys = set(worked_dates) | set(scheduled_dates) | set(leave_dates) | set(salary_by_month)
    month_keys.add(current_key)
    history = []
    for key in month_keys:
        year, month = (int(part) for part in key.split("-", 1))
        first = date(year, month, 1)
        if first > today:
            continue
        last = _next_month(first) - timedelta(days=1)
        through = min(last, today)
        worked = {day for day in worked_dates.get(key, set()) if day <= through}
        scheduled = {day for day in scheduled_started_dates.get(key, set()) if day <= through}
        leave = {day for day in leave_dates.get(key, set()) if day <= through and day not in worked}
        missed = {day for day in scheduled if day not in worked and day not in leave}
        salary = salary_by_month.get(key) or {}
        net = Decimal(str(salary.get("net_pay") or "0.00"))
        paid = Decimal(str(salary.get("salary_amount_paid") or "0.00"))
        if not salary:
            payment_status = "NOT_CALCULATED"
        elif paid >= net:
            payment_status = "PAID"
        elif paid > 0:
            payment_status = "PARTIALLY_PAID"
        else:
            payment_status = "UNPAID"
        history.append({
            "month": key,
            "month_label": _month_label(first),
            "starts_on": first,
            "through_date": through,
            "is_current_month": key == current_key,
            "worked_days": len(worked),
            "scheduled_days": len(scheduled),
            "missed_days": len(missed),
            "leave_days": len(leave),
            "unscheduled_worked_days": len(worked - scheduled),
            "worked_minutes": worked_minutes.get(key, 0),
            "payment_status": payment_status,
            "payroll_period_references": salary.get("payroll_period_references", []),
            "payroll_periods": salary.get("payroll_periods", []),
            "currency": salary.get("currency") or "NGN",
            "gross_pay": str(salary.get("gross_pay") or Decimal("0.00")),
            "deductions_total": str(salary.get("deductions_total") or Decimal("0.00")),
            "net_pay": str(net),
            "salary_amount_paid": str(paid),
            "salary_balance": str(max(net - paid, Decimal("0.00"))),
        })
    history.sort(key=lambda row: row["month"], reverse=True)
    return history[:24]


@extend_schema(tags=["Admin · Staff operations"], summary="My staff self-service profile")
class MyStaffProfileView(APIView):
    """Return the caller's own employment records, and only theirs."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user: User = request.user
        if not user.is_active or getattr(user, "role", None) == User.Role.GUEST or not user.is_staff_member:
            raise PermissionDenied("Staff self-service is available to staff accounts only.")

        attendance_from = _parse_date(request, "attendance_from")
        attendance_to = _parse_date(request, "attendance_to")
        if attendance_from and attendance_to and attendance_to < attendance_from:
            raise ValidationError({"attendance_to": "Attendance range must end on or after it starts."})

        profile = (
            StaffProfile.objects.select_related("user", "department_ref", "reporting_manager", "default_shift_template", "assigned_workstation")
            .filter(user=user)
            .first()
        )
        if profile is None:
            # Same lazy minimal-profile behaviour as the manager profile and
            # the account serializer; creates no payroll or HR facts.
            profile, _ = ensure_staff_profile(staff=user)
            profile = (
                StaffProfile.objects.select_related("user", "department_ref", "reporting_manager", "default_shift_template", "assigned_workstation")
                .get(pk=profile.pk)
            )

        today = hotel_today()
        now = timezone.now()

        # --- Attendance (authoritative records only) -----------------------
        attendance_qs = AttendanceRecord.objects.select_related("staff", "shift").filter(staff=user)
        if attendance_from:
            attendance_qs = attendance_qs.filter(business_date__gte=attendance_from)
        if attendance_to:
            attendance_qs = attendance_qs.filter(business_date__lte=attendance_to)
        attendance_rows = list(attendance_qs.order_by("-clock_in_at")[:MAX_ROWS])
        month_start = today.replace(day=1)
        attendance_summary = (
            AttendanceRecord.objects.filter(staff=user, business_date__gte=month_start, business_date__lte=today)
            .aggregate(days=Count("id"), worked_minutes=Sum("worked_minutes"))
        )

        # --- Shifts (schedule, kept distinct from attendance) --------------
        shifts_qs = ShiftAssignment.objects.select_related("staff", "template", "assigned_by").filter(staff=user)
        upcoming_shifts = list(shifts_qs.filter(planned_start_at__gte=now).order_by("planned_start_at")[:10])
        recent_shifts = list(shifts_qs.order_by("-planned_start_at")[:MAX_ROWS])

        # --- Leave -----------------------------------------------------------
        leave_rows = list(
            LeaveRequest.objects.select_related("staff", "reviewer").filter(staff=user).order_by("-start_date", "-created_at")[:MAX_ROWS]
        )
        leave_payload = []
        for row in leave_rows:
            data = LeaveRequestListSerializer(row).data
            data["days"] = (row.end_date - row.start_date).days + 1
            data["approver_name"] = row.reviewer.full_name if row.reviewer else ""
            leave_payload.append(data)
        # No leave-balance/entitlement ledger exists in this system, so no
        # balance is calculated or invented here.
        upcoming_candidates = [
            row for row in leave_payload
            if row["status"] == LeaveRequest.Status.APPROVED and str(row["end_date"]) >= str(today)
        ]
        upcoming_leave = min(upcoming_candidates, key=lambda row: str(row["start_date"]), default=None)

        # --- Payroll: own compensation + approved payslips only ------------
        compensation = (
            StaffCompensation.objects.filter(staff=user).order_by("-effective_from", "-pk").first()
        )
        from .payroll_views import _payroll_line_queryset

        payslip_qs = (
            _payroll_line_queryset()
            .filter(
                staff=user,
                period__status__in=(
                    PayrollPeriod.Status.APPROVED,
                    PayrollPeriod.Status.PARTIALLY_PAID,
                    PayrollPeriod.Status.PAID,
                ),
            )
            .select_related("period")
            .order_by("-period__ends_on", "-pk")
        )
        payslip_rows = list(payslip_qs[:MAX_ROWS])
        payslips = []
        for line in payslip_rows:
            data = MyPayslipSerializer(line).data
            data["period_reference"] = line.period.reference
            # Distinguish "approved, not yet paid" from actually paid: the
            # payment status is derived from recorded salary payments, never
            # from the payroll calculation having completed.
            paid = Decimal(str(data.get("salary_amount_paid") or "0.00"))
            net = Decimal(str(line.net_pay))
            data["payment_status"] = "PAID" if net > 0 and paid >= net else ("PARTIALLY_PAID" if paid > 0 else "UNPAID")
            data["payslip_available"] = True
            payslips.append(data)
        payment_rows = list(
            PayrollSalaryPayment.objects.select_related("line", "line__period", "recorded_by")
            .filter(line__staff=user)
            .order_by("-payment_date", "-pk")[:MAX_ROWS]
        )

        salary_by_month = {}
        for line, slip in zip(payslip_rows, payslips):
            month_key = line.period.starts_on.strftime("%Y-%m")
            group = salary_by_month.setdefault(month_key, {
                "payroll_period_references": [], "payroll_periods": [], "currency": line.currency,
                "gross_pay": Decimal("0.00"), "deductions_total": Decimal("0.00"),
                "net_pay": Decimal("0.00"), "salary_amount_paid": Decimal("0.00"),
            })
            if line.period.reference not in group["payroll_period_references"]:
                group["payroll_period_references"].append(line.period.reference)
            group["payroll_periods"].append({
                "reference": line.period.reference,
                "starts_on": line.period.starts_on,
                "ends_on": line.period.ends_on,
                "status": line.period.status,
                "payment_status": slip["payment_status"],
                "net_pay": str(line.net_pay),
                "salary_amount_paid": slip["salary_amount_paid"],
                "salary_balance": slip["salary_balance"],
            })
            group["currency"] = line.currency
            group["gross_pay"] += Decimal(line.gross_pay)
            group["deductions_total"] += Decimal(line.deductions_total)
            group["net_pay"] += Decimal(line.net_pay)
            group["salary_amount_paid"] += Decimal(str(slip.get("salary_amount_paid") or "0.00"))
        monthly_history = _build_monthly_work_history(
            user=user, today=today, salary_by_month=salary_by_month,
        )
        current_month_work = next(
            (row for row in monthly_history if row["is_current_month"]), None,
        )

        lateness_deductions = [
            {
                "reference": deduction.reference,
                "business_date": deduction.business_date,
                "scheduled_start_at": deduction.scheduled_start_at,
                "clock_in_at": deduction.clock_in_at,
                "late_minutes": deduction.late_minutes,
                "grace_minutes": deduction.grace_minutes,
                "deduction_percent": str(deduction.deduction_percent),
                "monthly_pay_basis": str(deduction.monthly_pay_basis),
                "days_in_month": deduction.days_in_month,
                "daily_pay": str(deduction.daily_pay),
                "amount": str(deduction.amount),
                "currency": deduction.currency,
                "status": deduction.status,
                "applied_period_reference": (
                    deduction.applied_payroll_line.period.reference
                    if deduction.applied_payroll_line_id else None
                ),
                "reimbursement_period_reference": (
                    deduction.reimbursement_payroll_line.period.reference
                    if deduction.reimbursement_payroll_line_id else None
                ),
                "revocation_reason": deduction.revocation_reason,
                "decision_note": deduction.decision_note,
            }
            for deduction in StaffLatenessDeduction.objects.select_related(
                "applied_payroll_line__period", "reimbursement_payroll_line__period",
            ).filter(staff=user).order_by("-business_date", "-pk")[:MAX_ROWS]
        ]

        salary_section = {
            "current_month_work": current_month_work,
            "monthly_history": monthly_history,
            "lateness_deductions": lateness_deductions,
            "configured": compensation is not None,
            "message": None if compensation is not None else "No payroll compensation has been configured for your account yet. Contact HR or Accounts.",
            "current_compensation": {
                "currency": compensation.currency,
                "basic_salary": str(compensation.basic_salary),
                "housing_allowance": str(compensation.housing_allowance),
                "transport_allowance": str(compensation.transport_allowance),
                "overtime_rate": str(compensation.overtime_rate),
                "effective_from": compensation.effective_from,
            } if compensation else None,
            "payslips": payslips,
            "payments": [
                {
                    "reference": payment.reference,
                    "period_reference": payment.line.period.reference,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "method": payment.method,
                    "payment_date": payment.payment_date,
                    "status": "REVERSAL" if payment.reversal_of_id else "POSTED",
                }
                for payment in payment_rows
            ],
        }

        # --- Disciplinary, reviews, documents, other records ----------------
        disciplinary = [
            _disciplinary_for_employee(record)
            for record in StaffDisciplinaryRecord.objects.select_related("reported_by", "reviewed_by")
            .filter(staff=user).order_by("-incident_at")[:MAX_ROWS]
        ]
        performance_reviews = StaffPerformanceReviewSerializer(
            StaffPerformanceReview.objects.select_related("staff", "reviewer")
            .filter(staff=user, status=StaffPerformanceReview.Status.FINALIZED)
            .order_by("-review_date")[:MAX_ROWS],
            many=True,
        ).data
        documents = [
            {
                "reference": doc.reference,
                "category": doc.category,
                "title": doc.title,
                "description": doc.description,
                "issued_date": doc.issued_date,
                "expiry_date": doc.expiry_date,
                "is_confidential": False,
            }
            for doc in StaffDocument.objects.filter(staff=user, is_active=True, is_confidential=False)
            .order_by("-created_at")[:MAX_ROWS]
        ]
        training = [doc for doc in documents if doc["category"] in {"TRAINING", "CERTIFICATION"}]
        equipment = [doc for doc in documents if doc["category"] == "EQUIPMENT_ASSIGNMENT"]

        expense_claims = []
        try:
            from apps.finance.models import Expense

            expense_claims = [
                {
                    "reference": expense.reference,
                    "category": expense.category,
                    "amount": str(expense.amount),
                    "currency": expense.currency,
                    "status": expense.status,
                    "incurred_on": expense.incurred_on,
                }
                for expense in Expense.objects.filter(requested_by=user).order_by("-incurred_on")[:MAX_ROWS]
            ]
        except Exception:  # pragma: no cover - finance app always installed
            expense_claims = []

        profile_data = StaffProfileSerializer(profile, context={"request": request}).data
        # Internal HR notes and credential-delivery internals are not
        # employee self-service data.
        profile_data.pop("notes", None)
        profile_data.pop("credential_delivery_status", None)
        profile_data.pop("credential_delivered_at", None)

        next_shift = ShiftAssignmentListSerializer(upcoming_shifts[0]).data if upcoming_shifts else None
        latest_payslip = payslips[0] if payslips else None
        data = {
            "user": UserSerializer(user, context={"request": request}).data,
            "profile": profile_data,
            "overview": {
                "full_name": user.full_name,
                "employee_code": profile.employee_code,
                "department": profile_data.get("department_name") or profile.department,
                "job_title": profile.job_title,
                "employment_status": profile.employment_status,
                "employment_type": profile.employment_type,
                "next_shift": next_shift,
                "recent_attendance": AttendanceRecordListSerializer(attendance_rows[0]).data if attendance_rows else None,
                "upcoming_approved_leave": upcoming_leave,
                "latest_payslip": (
                    {"period_reference": latest_payslip["period_reference"], "payment_status": latest_payslip["payment_status"]}
                    if latest_payslip else None
                ),
                "pending_leave_requests": sum(1 for row in leave_rows if row.status == LeaveRequest.Status.PENDING),
                "attendance_this_month": {
                    "days_recorded": current_month_work["worked_days"] if current_month_work else (attendance_summary.get("days") or 0),
                    "worked_minutes": current_month_work["worked_minutes"] if current_month_work else (attendance_summary.get("worked_minutes") or 0),
                    "scheduled_days": current_month_work["scheduled_days"] if current_month_work else 0,
                    "missed_days": current_month_work["missed_days"] if current_month_work else 0,
                    "leave_days": current_month_work["leave_days"] if current_month_work else 0,
                },
            },
            "salary": salary_section,
            "attendance": AttendanceRecordListSerializer(attendance_rows, many=True).data,
            "attendance_summary": {
                "month": str(month_start),
                "days_recorded": current_month_work["worked_days"] if current_month_work else (attendance_summary.get("days") or 0),
                "worked_minutes": current_month_work["worked_minutes"] if current_month_work else (attendance_summary.get("worked_minutes") or 0),
                "scheduled_days": current_month_work["scheduled_days"] if current_month_work else 0,
                "missed_days": current_month_work["missed_days"] if current_month_work else 0,
                "leave_days": current_month_work["leave_days"] if current_month_work else 0,
            },
            "shifts": ShiftAssignmentListSerializer(recent_shifts, many=True).data,
            "upcoming_shifts": ShiftAssignmentListSerializer(upcoming_shifts, many=True).data,
            "leave": {
                "balance": None,
                "balance_note": "No leave-balance ledger is recorded in this system; approved and pending requests are shown from your leave records.",
                "requests": leave_payload,
                "upcoming_approved": upcoming_leave,
            },
            "disciplinary": disciplinary,
            "performance_reviews": performance_reviews,
            "guest_reviews": {
                "attribution_available": False,
                "note": "Guest reviews in this system are recorded against a stay/booking, not against an individual staff member, so no guest review is attributed to you here.",
                "items": [],
            },
            "documents": documents,
            "training_and_certifications": training,
            "assigned_equipment": equipment,
            "expense_claims": expense_claims,
        }
        response = success_response(data)
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["Pragma"] = "no-cache"
        response["X-Content-Type-Options"] = "nosniff"
        return response
