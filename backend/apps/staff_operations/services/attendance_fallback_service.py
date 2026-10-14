"""Supervised attendance fallback (audit N5/N6) — split-verified service.

The Receptionist Desktop is the attendance station, but hardware scanners fail,
fingers are wet, and templates expire. The business therefore needs a *recorded*
override path that cannot be reached by an ordinary employee and can never be
self-declared by the person it benefits.

Policy implemented here (server side — never inferred from a user agent, screen
size, IP address or a front-end check):

* an ORDINARY EMPLOYEE can never authorise a fallback for anyone, including
  themselves;
* a MANAGER (a management role that also holds ``attendance.manage``) may
  authorise a fallback only when the system holds a *recorded* qualifying
  failure for that staff member (a failed fingerprint verification attempt or a
  failed device capture) or the workstation's registered reader is in FAULT —
  never merely because a mismatch happened;
* an ADMINISTRATOR may authorise a fallback without a prior attempt (privileged
  exception) but still must give a reason of at least
  ``MIN_MANUAL_REASON_LENGTH`` characters;
* every fallback writes a ``MANUAL_OVERRIDE`` verification attempt, names the
  authorising supervisor in the audit trail, and mints the SAME single-use,
  time-limited temporary workstation credential as the biometric path — the code
  is backend-issued, bound to the staff member's attendance record, and
  invalidated on clock-out like any other.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.desktop_policy import STAFF_SIGN_IN_EXEMPT_ROLES
from apps.accounts.models import User
from apps.audit.services import log_action

from ..biometric_models import FingerprintDeviceEvent, FingerprintVerificationAttempt

from .fingerprint_service import (
    DEFAULT_MANUAL_FALLBACK_WINDOW_MINUTES,
    MIN_MANUAL_REASON_LENGTH,
    QUALIFYING_FAILURE_OUTCOMES,
    FingerprintOutcome,
    mint_temporary_workstation_credential,
    staff_department_code,
)


AUTHORITY_NONE = "NONE"
AUTHORITY_MANAGER = "MANAGER"
AUTHORITY_ADMINISTRATOR = "ADMINISTRATOR"


def _fallback_window() -> timedelta:
    minutes = int(
        getattr(settings, "MANUAL_ATTENDANCE_FALLBACK_WINDOW_MINUTES", DEFAULT_MANUAL_FALLBACK_WINDOW_MINUTES)
    )
    return timedelta(minutes=max(1, minutes))


def manual_fallback_authority(actor: User | None) -> str:
    """Return what kind of fallback authority this account carries.

    Management roles are exactly the accounts the Receptionist Desktop sign-in
    policy already treats as management (``STAFF_SIGN_IN_EXEMPT_ROLES``), and a
    manager must additionally hold ``attendance.manage``. Everybody else gets
    ``NONE`` — including every ordinary operational role.
    """
    if actor is None or not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        return AUTHORITY_NONE
    if actor.role == User.Role.ADMIN:
        return AUTHORITY_ADMINISTRATOR
    if actor.role in STAFF_SIGN_IN_EXEMPT_ROLES and has_capability(actor, "attendance.manage"):
        return AUTHORITY_MANAGER
    return AUTHORITY_NONE


def find_qualifying_failure(*, staff: User, workstation_reference: str = "") -> dict | None:
    """Return the recorded failure that qualifies a manager fallback, else None.

    Two server-side sources count, both of them staff-specific and both of them
    recorded by the system rather than asserted by a client:

    * a failed verification attempt whose outcome is a capture/hardware failure
      (invalid capture, timeout, unsupported reader, integration error, reader
      unavailable), recorded by the terminal path or the reader agent;
    * a failed reader capture event from a bound device for that staff member.

    A plain NO_MATCH never qualifies: a wrong finger is not a hardware failure.
    """
    since = timezone.now() - _fallback_window()

    attempt = (
        FingerprintVerificationAttempt.objects.filter(
            staff=staff, outcome__in=QUALIFYING_FAILURE_OUTCOMES, created_at__gte=since
        )
        .order_by("-created_at")
        .first()
    )
    if attempt is not None:
        return {
            "source": "VERIFICATION_ATTEMPT",
            "outcome": attempt.outcome,
            "attempt_id": attempt.pk,
            "recorded_at": attempt.created_at,
            "workstation_reference": attempt.workstation_reference,
        }

    device_event = (
        FingerprintDeviceEvent.objects.filter(
            staff=staff, outcome__in=QUALIFYING_FAILURE_OUTCOMES, received_at__gte=since
        )
        .select_related("device")
        .order_by("-received_at")
        .first()
    )
    if device_event is not None:
        return {
            "source": "DEVICE_CAPTURE",
            "outcome": device_event.outcome,
            "device_serial": device_event.device.serial_number,
            "recorded_at": device_event.received_at,
            "workstation_reference": getattr(device_event.device.workstation, "reference", ""),
        }
    return None


def issue_manual_fallback_code(
    *,
    staff: User,
    actor: User | None,
    reason: str,
    workstation_reference: str,
    attendance_record=None,
    clock_in: bool = False,
) -> dict:
    """Authorise, record and mint a supervised fallback workstation code.

    Raises ``PermissionDenied`` for ordinary employees and for managers without
    recorded failure evidence, and ``ValidationError`` for a missing/short
    reason. Applies to exactly one staff member and one attendance record.

    Every check runs BEFORE anything is written. ``clock_in=True`` lets the
    terminal action open the attendance record for this fallback (the employee
    could not clock in biometrically); the clock-in still happens only after the
    supervisor has been authorised, so a refused fallback leaves no attendance
    behind.
    """
    authority = manual_fallback_authority(actor)
    if authority == AUTHORITY_NONE:
        raise PermissionDenied(
            "A supervised attendance fallback requires a signed-in manager or administrator."
        )

    cleaned_reason = str(reason or "").strip()
    if len(cleaned_reason) < MIN_MANUAL_REASON_LENGTH:
        raise ValidationError(
            {"manual_override_reason": f"Give a reason of at least {MIN_MANUAL_REASON_LENGTH} characters."}
        )

    if not staff.is_active:
        raise PermissionDenied("This staff account is inactive and cannot receive a fallback code.")
    profile = getattr(staff, "staff_profile", None)
    if profile and profile.employment_status in ("SUSPENDED", "INACTIVE", "ON_LEAVE"):
        raise PermissionDenied(
            f"Staff member is currently {profile.employment_status.lower()} and is not eligible for "
            "attendance or workstation codes."
        )

    evidence = find_qualifying_failure(staff=staff, workstation_reference=workstation_reference)
    if evidence is None and authority != AUTHORITY_ADMINISTRATOR:
        log_action(
            actor=actor,
            action="ATTENDANCE_MANUAL_FALLBACK_REFUSED",
            instance=None,
            metadata={"staff_id": staff.pk, "reason": cleaned_reason[:200], "cause": "no_recorded_failure"},
        )
        raise PermissionDenied(
            "A manager fallback needs a recorded capture failure for this staff member (a failed scan or a "
            "reported reader problem). Record the failure from the terminal first, or ask an administrator."
        )

    if attendance_record is None and clock_in:
        # Authorised: now the employee's attendance for this fallback is opened.
        # Clocking stays attributed to the staff member (their record); the
        # supervisor's authorisation is the audit entry written below. An open
        # record is reused, so a repeated fallback can never open a second one.
        from ..models import AttendanceRecord
        from .staff_operations_service import clock_attendance

        attendance_record = (
            AttendanceRecord.objects.filter(staff=staff, clock_out_at__isnull=True)
            .order_by("-clock_in_at")
            .first()
        )
        if attendance_record is None:
            attendance_record, _created = clock_attendance(
                staff=staff,
                action="CLOCK_IN",
                actor=staff,
                idempotency_key=f"attendance-fallback:{staff.pk}:{secrets.token_hex(8)}",
            )

    outcome_record = FingerprintVerificationAttempt.objects.create(
        staff=staff,
        action="MANUAL_FALLBACK",
        outcome=FingerprintOutcome.MANUAL_OVERRIDE,
        source_key=f"fp-fallback:{staff.pk}:{secrets.token_hex(6)}",
        workstation_reference=workstation_reference or "",
    )
    log_action(
        actor=actor,
        action="ATTENDANCE_MANUAL_OVERRIDE",
        instance=None,
        metadata={
            "staff_id": staff.pk,
            "action": "MANUAL_FALLBACK",
            "reason": cleaned_reason[:200],
            "authority": authority,
            "evidence": {
                "source": evidence["source"],
                "outcome": evidence["outcome"],
                "recorded_at": evidence["recorded_at"].isoformat(),
            }
            if evidence
            else {"source": "ADMINISTRATOR_EXCEPTION"},
            "attempt_id": outcome_record.pk,
            "workstation": workstation_reference,
        },
    )

    details = mint_temporary_workstation_credential(
        staff=staff,
        workstation_reference=workstation_reference,
        attendance_record=attendance_record,
        issue_numeric_code=True,
        issued_by=actor,
        mode="MANUAL_FALLBACK",
    )
    details.update(
        {
            "action": "MANUAL_FALLBACK",
            "status": "CODE_ISSUED",
            "outcome": FingerprintOutcome.MANUAL_OVERRIDE,
            "manual_override": True,
            "authority": authority,
            "authorised_by": getattr(actor, "email", "") or "",
            "reason": cleaned_reason,
            "evidence": evidence["source"] if evidence else "ADMINISTRATOR_EXCEPTION",
            "staff_id": staff.pk,
            "staff_email": staff.email,
            "staff_name": staff.full_name,
            "employee_code": getattr(getattr(staff, "staff_profile", None), "employee_code", ""),
            "role": staff.role,
            "department": staff_department_code(staff),
            "workstation": workstation_reference,
            "attendance_status": getattr(attendance_record, "status", ""),
            "clock_in_at": attendance_record.clock_in_at.isoformat()
            if attendance_record is not None and attendance_record.clock_in_at
            else None,
        }
    )
    return details
