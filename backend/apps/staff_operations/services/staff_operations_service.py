"""Locked workforce scheduling, attendance, and leave workflow commands."""
from __future__ import annotations

import hashlib
import secrets
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.core.utils import hotel_date_bounds, hotel_today

from ..models import (
    AttendanceEvent, AttendanceRecord, LeaveRequest, LeaveRequestEvent,
    ShiftAssignment, ShiftAssignmentEvent, StaffProfile,
)


def _reference(prefix: str) -> str:
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def scoped_key(*, prefix: str, scope: str, raw_key) -> str | None:
    raw_key = str(raw_key or "").strip()
    if not raw_key:
        return None
    if len(raw_key) > 128:
        raise ValidationError({"idempotency_key": "Idempotency key must be at most 128 characters."})
    return f"{prefix}:{hashlib.sha256(f'{scope}:{raw_key}'.encode()).hexdigest()}"


def _locked_operational_staff(staff: User) -> User:
    staff = User.objects.select_for_update().get(pk=staff.pk)
    if not staff.is_active or not staff.is_staff_member:
        raise ValidationError("An active operational staff account is required.")
    return staff


def _event(*, shift, event_type, actor=None, details=None):
    return ShiftAssignmentEvent.objects.create(
        shift=shift, type=event_type, actor=actor, details=dict(details or {}),
    )


def _leave_event(*, leave_request, event_type, actor=None, details=None):
    return LeaveRequestEvent.objects.create(
        leave_request=leave_request, type=event_type, actor=actor, details=dict(details or {}),
    )


@transaction.atomic
def ensure_staff_profile(*, staff: User) -> tuple[StaffProfile, bool]:
    """Create a minimal profile lazily without mutating the account role."""
    staff = _locked_operational_staff(staff)
    existing = StaffProfile.objects.select_for_update().filter(user=staff).first()
    if existing:
        return existing, False
    for _ in range(8):
        try:
            with transaction.atomic():
                profile = StaffProfile.objects.create(
                    user=staff,
                    employee_code=_reference("STF"),
                    department=staff.get_role_display(),
                    job_title=staff.get_role_display(),
                )
            return profile, True
        except IntegrityError:
            existing = StaffProfile.objects.select_for_update().filter(user=staff).first()
            if existing:
                return existing, False
    raise ValidationError("Could not allocate a staff employee code; please retry.")


@transaction.atomic
def create_shift_assignment(
    *, staff: User, planned_start_at, planned_end_at, actor: User, template=None,
    department="", location="", notes="", idempotency_key=None,
) -> tuple[ShiftAssignment, bool]:
    """Assign a bounded non-overlapping shift, respecting approved leave."""
    if not has_capability(actor, "shift.manage"):
        raise PermissionDenied("This user cannot schedule staff shifts.")
    key = str(idempotency_key or "").strip() or None
    if key:
        existing = ShiftAssignment.objects.select_for_update().filter(idempotency_key=key).first()
        if existing:
            return existing, False
    staff = _locked_operational_staff(staff)
    if not timezone.is_aware(planned_start_at) or not timezone.is_aware(planned_end_at):
        raise ValidationError("Shift start and end must include a timezone.")
    duration = planned_end_at - planned_start_at
    if duration <= timedelta(0) or duration > timedelta(hours=24):
        raise ValidationError("A shift must be longer than zero and no more than 24 hours.")
    active_overlap = ShiftAssignment.objects.select_for_update().filter(
        staff=staff, status=ShiftAssignment.Status.SCHEDULED,
        planned_start_at__lt=planned_end_at, planned_end_at__gt=planned_start_at,
    ).exists()
    if active_overlap:
        raise ValidationError("This staff member already has an overlapping scheduled shift.")
    local_start = timezone.localtime(planned_start_at).date()
    local_end = timezone.localtime(planned_end_at).date()
    approved_leave = LeaveRequest.objects.select_for_update().filter(
        staff=staff, status=LeaveRequest.Status.APPROVED,
        start_date__lte=local_end, end_date__gte=local_start,
    ).exists()
    if approved_leave:
        raise ValidationError("This shift overlaps approved leave for the staff member.")
    if template is not None and not template.is_active:
        raise ValidationError("An inactive shift template cannot be assigned.")
    for _ in range(8):
        try:
            with transaction.atomic():
                shift = ShiftAssignment.objects.create(
                    reference=_reference("SHF"), idempotency_key=key, staff=staff, template=template,
                    planned_start_at=planned_start_at, planned_end_at=planned_end_at,
                    department=(department or getattr(template, "department", ""))[:120],
                    location=(location or "")[:120], assigned_by=actor, notes=notes or "",
                )
            break
        except IntegrityError:
            if key:
                existing = ShiftAssignment.objects.select_for_update().filter(idempotency_key=key).first()
                if existing:
                    return existing, False
    else:
        raise ValidationError("Could not allocate a shift reference; please retry.")
    _event(shift=shift, event_type=ShiftAssignmentEvent.Type.CREATED, actor=actor,
           details={"staff_id": staff.pk, "planned_start_at": planned_start_at.isoformat(), "planned_end_at": planned_end_at.isoformat()})
    return shift, True


@transaction.atomic
def cancel_shift_assignment(*, shift: ShiftAssignment, actor: User, reason="") -> ShiftAssignment:
    if not has_capability(actor, "shift.manage"):
        raise PermissionDenied("This user cannot cancel staff shifts.")
    shift = ShiftAssignment.objects.select_for_update().get(pk=shift.pk)
    if shift.status == ShiftAssignment.Status.CANCELLED:
        return shift
    if shift.status != ShiftAssignment.Status.SCHEDULED:
        raise ValidationError(f"Shift cannot be cancelled from {shift.status}.")
    if AttendanceRecord.objects.select_for_update().filter(
        shift=shift, status__in=[AttendanceRecord.Status.ON_DUTY, AttendanceRecord.Status.ON_BREAK]
    ).exists():
        raise ValidationError("An active attendance record must be closed before this shift can be cancelled.")
    shift.status = ShiftAssignment.Status.CANCELLED
    shift.cancelled_at = timezone.now(); shift.cancelled_by = actor; shift.cancellation_reason = (reason or "")[:500]
    shift.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancellation_reason", "updated_at"])
    _event(shift=shift, event_type=ShiftAssignmentEvent.Type.CANCELLED, actor=actor, details={"reason": shift.cancellation_reason})
    return shift


def _attendance_source_key(*, staff: User, event_type: str, raw_key) -> str:
    key = scoped_key(prefix="attendance", scope=f"staff:{staff.pk}:{event_type}", raw_key=raw_key)
    if not key:
        raise ValidationError({"idempotency_key": "An idempotency key is required for attendance clocking."})
    return key


def _open_attendance_record(*, staff: User) -> AttendanceRecord | None:
    records = list(AttendanceRecord.objects.select_for_update().filter(
        staff=staff, status__in=[AttendanceRecord.Status.ON_DUTY, AttendanceRecord.Status.ON_BREAK],
    ).order_by("-clock_in_at")[:2])
    if len(records) > 1:
        raise ValidationError("Multiple active attendance records require manager correction.")
    return records[0] if records else None


def _attendance_event(*, record, event_type, source_key, actor, occurred_at, metadata=None):
    return AttendanceEvent.objects.create(
        record=record, staff=record.staff, shift=record.shift, type=event_type,
        source_key=source_key, actor=actor, occurred_at=occurred_at, metadata=dict(metadata or {}),
    )


@transaction.atomic
def clock_attendance(*, staff: User, action: str, actor: User, idempotency_key, shift_reference="") -> tuple[AttendanceRecord, bool]:
    """Append a server-timestamped event and safely refresh its attendance projection."""
    action = str(action or "").upper()
    if action not in AttendanceEvent.Type.values:
        raise ValidationError({"action": "Invalid attendance clock action."})
    if actor.pk != staff.pk or not has_capability(actor, "attendance.clock"):
        raise PermissionDenied("Attendance clocking is limited to the active staff member.")
    staff = _locked_operational_staff(staff)
    source_key = _attendance_source_key(staff=staff, event_type=action, raw_key=idempotency_key)
    prior = AttendanceEvent.objects.select_for_update().select_related("record").filter(source_key=source_key).first()
    if prior:
        return prior.record, False
    now = timezone.now()
    current = _open_attendance_record(staff=staff)

    if action == AttendanceEvent.Type.CLOCK_IN:
        if current:
            raise ValidationError("Clock out the current attendance record before clocking in again.")
        shift = None
        if shift_reference:
            shift = ShiftAssignment.objects.select_for_update().filter(reference=shift_reference).first()
            if shift is None or shift.staff_id != staff.pk:
                raise ValidationError("The selected shift does not belong to this staff member.")
            if shift.status != ShiftAssignment.Status.SCHEDULED:
                raise ValidationError("Attendance can only start against a scheduled shift.")
            if AttendanceRecord.objects.select_for_update().filter(shift=shift).exists():
                raise ValidationError("This shift already has an attendance record.")
        business_date = timezone.localtime(shift.planned_start_at).date() if shift else hotel_today()
        for _ in range(8):
            try:
                with transaction.atomic():
                    record = AttendanceRecord.objects.create(
                        reference=_reference("ATT"), staff=staff, shift=shift, business_date=business_date,
                        status=AttendanceRecord.Status.ON_DUTY, clock_in_at=now, last_event_at=now,
                    )
                break
            except IntegrityError:
                if shift:
                    duplicate = AttendanceRecord.objects.select_for_update().filter(shift=shift).first()
                    if duplicate:
                        return duplicate, False
        else:
            raise ValidationError("Could not allocate an attendance reference; please retry.")
        _attendance_event(record=record, event_type=action, source_key=source_key, actor=actor, occurred_at=now)
        if shift and shift.actual_start_at is None:
            shift.actual_start_at = now
            shift.save(update_fields=["actual_start_at", "updated_at"])
        return record, True

    if current is None:
        raise ValidationError("Clock in before recording this attendance action.")
    if shift_reference and current.shift and current.shift.reference != shift_reference:
        raise ValidationError("Attendance action does not match the active shift.")

    if action == AttendanceEvent.Type.BREAK_STARTED:
        if current.status != AttendanceRecord.Status.ON_DUTY:
            raise ValidationError("A break can only start while on duty.")
        current.status = AttendanceRecord.Status.ON_BREAK
        current.active_break_started_at = now; current.last_event_at = now
        current.save(update_fields=["status", "active_break_started_at", "last_event_at", "updated_at"])
    elif action == AttendanceEvent.Type.BREAK_ENDED:
        if current.status != AttendanceRecord.Status.ON_BREAK or current.active_break_started_at is None:
            raise ValidationError("There is no active break to end.")
        break_minutes = max(0, int((now - current.active_break_started_at).total_seconds() // 60))
        current.status = AttendanceRecord.Status.ON_DUTY
        current.accumulated_break_minutes += break_minutes
        current.active_break_started_at = None; current.last_event_at = now
        current.save(update_fields=["status", "accumulated_break_minutes", "active_break_started_at", "last_event_at", "updated_at"])
    else:  # CLOCK_OUT
        if current.status == AttendanceRecord.Status.ON_BREAK:
            raise ValidationError("End the active break before clocking out.")
        if current.status != AttendanceRecord.Status.ON_DUTY:
            raise ValidationError("Only an active on-duty record can be clocked out.")
        elapsed_minutes = max(0, int((now - current.clock_in_at).total_seconds() // 60))
        current.status = AttendanceRecord.Status.COMPLETED
        current.clock_out_at = now; current.worked_minutes = max(0, elapsed_minutes - current.accumulated_break_minutes)
        current.last_event_at = now
        current.save(update_fields=["status", "clock_out_at", "worked_minutes", "last_event_at", "updated_at"])
        if current.shift:
            shift = ShiftAssignment.objects.select_for_update().get(pk=current.shift_id)
            if shift.status == ShiftAssignment.Status.SCHEDULED:
                shift.status = ShiftAssignment.Status.COMPLETED; shift.actual_end_at = now; shift.actual_worked_minutes = current.worked_minutes
                shift.save(update_fields=["status", "actual_end_at", "actual_worked_minutes", "updated_at"])
                _event(shift=shift, event_type=ShiftAssignmentEvent.Type.COMPLETED, actor=actor,
                       details={"attendance_reference": current.reference, "worked_minutes": current.worked_minutes})
    _attendance_event(record=current, event_type=action, source_key=source_key, actor=actor, occurred_at=now)
    return current, True


@transaction.atomic
def request_leave(*, staff: User, leave_type: str, start_date: date, end_date: date, reason: str, idempotency_key, actor: User) -> tuple[LeaveRequest, bool]:
    if actor.pk != staff.pk or not has_capability(actor, "leave.request"):
        raise PermissionDenied("Leave requests may only be submitted by the active staff member.")
    key = str(idempotency_key or "").strip() or None
    if not key:
        raise ValidationError({"idempotency_key": "A leave-request idempotency key is required."})
    existing = LeaveRequest.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        return existing, False
    staff = _locked_operational_staff(staff)
    if leave_type not in LeaveRequest.Type.values:
        raise ValidationError({"type": "Invalid leave type."})
    if start_date < hotel_today() or end_date < start_date:
        raise ValidationError("Leave dates must be a valid current/future inclusive range.")
    if end_date - start_date > timedelta(days=366):
        raise ValidationError("Leave request range cannot exceed 367 days.")
    overlap = LeaveRequest.objects.select_for_update().filter(
        staff=staff, status__in=[LeaveRequest.Status.PENDING, LeaveRequest.Status.APPROVED],
        start_date__lte=end_date, end_date__gte=start_date,
    ).exists()
    if overlap:
        raise ValidationError("This leave request overlaps an existing pending or approved request.")
    for _ in range(8):
        try:
            with transaction.atomic():
                leave_request = LeaveRequest.objects.create(
                    reference=_reference("LVE"), idempotency_key=key, staff=staff, type=leave_type,
                    start_date=start_date, end_date=end_date, reason=reason or "",
                )
            break
        except IntegrityError:
            existing = LeaveRequest.objects.select_for_update().filter(idempotency_key=key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate a leave-request reference; please retry.")
    _leave_event(leave_request=leave_request, event_type=LeaveRequestEvent.Type.REQUESTED, actor=actor,
                 details={"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "type": leave_type})
    return leave_request, True


@transaction.atomic
def review_leave(*, leave_request: LeaveRequest, reviewer: User, approved: bool, review_note="") -> LeaveRequest:
    if not has_capability(reviewer, "leave.approve"):
        raise PermissionDenied("This user cannot review leave requests.")
    leave_request = LeaveRequest.objects.select_for_update().select_related("staff").get(pk=leave_request.pk)
    if leave_request.status in {LeaveRequest.Status.APPROVED, LeaveRequest.Status.REJECTED}:
        return leave_request
    if leave_request.status != LeaveRequest.Status.PENDING:
        raise ValidationError(f"Leave request cannot be reviewed from {leave_request.status}.")
    if leave_request.staff_id == reviewer.pk:
        raise PermissionDenied("A staff member cannot review their own leave request.")
    if approved:
        lower, upper = hotel_date_bounds(leave_request.start_date, leave_request.end_date)
        conflict = ShiftAssignment.objects.select_for_update().filter(
            staff=leave_request.staff, status=ShiftAssignment.Status.SCHEDULED,
            planned_start_at__lt=upper, planned_end_at__gt=lower,
        ).exists()
        if conflict:
            raise ValidationError("Scheduled shifts overlap this leave period; resolve them before approval.")
    leave_request.status = LeaveRequest.Status.APPROVED if approved else LeaveRequest.Status.REJECTED
    leave_request.reviewer = reviewer; leave_request.reviewed_at = timezone.now(); leave_request.review_note = review_note or ""
    leave_request.save(update_fields=["status", "reviewer", "reviewed_at", "review_note", "updated_at"])
    _leave_event(leave_request=leave_request,
                 event_type=LeaveRequestEvent.Type.APPROVED if approved else LeaveRequestEvent.Type.REJECTED,
                 actor=reviewer, details={"review_note": leave_request.review_note})
    return leave_request


@transaction.atomic
def cancel_leave(*, leave_request: LeaveRequest, actor: User, note="") -> LeaveRequest:
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    is_approver = has_capability(actor, "leave.approve")
    if leave_request.staff_id != actor.pk and not is_approver:
        raise PermissionDenied("This user cannot cancel another staff member's leave request.")
    if leave_request.status == LeaveRequest.Status.CANCELLED:
        return leave_request
    if leave_request.status not in {LeaveRequest.Status.PENDING, LeaveRequest.Status.APPROVED}:
        raise ValidationError(f"Leave request cannot be cancelled from {leave_request.status}.")
    if leave_request.start_date <= hotel_today():
        raise ValidationError("Leave that has started cannot be cancelled through the routine workflow.")
    leave_request.status = LeaveRequest.Status.CANCELLED; leave_request.cancelled_by = actor; leave_request.cancelled_at = timezone.now()
    leave_request.cancellation_note = note or ""
    leave_request.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancellation_note", "updated_at"])
    _leave_event(leave_request=leave_request, event_type=LeaveRequestEvent.Type.CANCELLED, actor=actor,
                 details={"note": leave_request.cancellation_note})
    return leave_request
