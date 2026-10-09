"""Facial verification for staff attendance (descriptor matching, no liveness claim).

Rules:
* Enrollment is self-service to request, and must be approved by a different
  user holding ``attendance.manage``. A staff member can never enrol against
  another account.
* Only one ACTIVE template per staff member (DB constraint + service lock).
* A probe that does not match, is not enrolled, or is expired never creates an
  attendance record. Every verification attempt is logged with its outcome.
* Manual fallback needs ``attendance.manage`` on the clocking account itself, and a
  reason (the clock endpoint is self-only). It is audited as ATTENDANCE_MANUAL_OVERRIDE.
* Repeating a clock request with the same idempotency key returns the recorded
  verification outcome rather than matching again.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action

from ..face_models import FaceTemplate, FaceVerificationAttempt

DESCRIPTOR_LENGTH = 128
MIN_MANUAL_REASON_LENGTH = 10


class FaceOutcome:
    VERIFIED = "VERIFIED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    NOT_ENROLLED = "NOT_ENROLLED"
    TEMPLATE_EXPIRED = "TEMPLATE_EXPIRED"
    NO_MATCH = "NO_MATCH"
    PROBE_INVALID = "PROBE_INVALID"
    FACE_REQUIRED = "FACE_REQUIRED"

    PASSING = frozenset({VERIFIED, MANUAL_OVERRIDE})


def face_verification_required() -> bool:
    return bool(getattr(settings, "STAFF_FACE_VERIFICATION_REQUIRED", False))


def match_threshold() -> float:
    return float(getattr(settings, "FACE_MATCH_MAX_DISTANCE", 0.5))


def retention_days() -> int:
    return int(getattr(settings, "FACE_TEMPLATE_RETENTION_DAYS", 365))


def clock_source_key(*, staff: User, action: str, raw_key) -> str:
    """Bounded, scoped key: one verification per staff, action and client idempotency key."""
    import hashlib
    digest = hashlib.sha256(f"{staff.pk}:{action}:{raw_key}".encode()).hexdigest()
    return f"face-clock:{digest}"


def validate_descriptor(raw) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != DESCRIPTOR_LENGTH:
        raise ValidationError({"descriptor": f"A face descriptor must contain exactly {DESCRIPTOR_LENGTH} numbers."})
    values: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValidationError({"descriptor": "Face descriptor values must be numbers."})
        value = float(item)
        if not math.isfinite(value) or abs(value) > 10:
            raise ValidationError({"descriptor": "Face descriptor values are out of range."})
        values.append(value)
    return values


def distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _clear(template: FaceTemplate, status: str, *, decided_by: User | None, note: str = "") -> None:
    template.status = status
    template.clear_descriptor()
    template.decided_at = timezone.now()
    template.decided_by = decided_by
    template.decision_note = note[:200]
    template.save(update_fields=["status", "descriptor", "decided_at", "decided_by", "decision_note", "updated_at"])


def request_enrollment(*, staff: User, actor: User, descriptor) -> FaceTemplate:
    if actor.pk != staff.pk:
        log_action(actor=actor, action="FACE_ENROLLMENT_DENIED", instance=None,
                   metadata={"staff_id": staff.pk, "reason": "cross_account"})
        raise PermissionDenied("Facial enrollment can only be requested for your own account.")
    values = validate_descriptor(descriptor)
    with transaction.atomic():
        # Lock the staff member's rows so concurrent requests cannot both become pending-and-active.
        list(FaceTemplate.objects.select_for_update().filter(staff=staff).values_list("pk", flat=True))
        for old in FaceTemplate.objects.filter(staff=staff, status=FaceTemplate.Status.PENDING):
            _clear(old, FaceTemplate.Status.REJECTED, decided_by=None, note="superseded by a newer request")
        template = FaceTemplate.objects.create(staff=staff, status=FaceTemplate.Status.PENDING, descriptor=values)
    log_action(actor=actor, action="FACE_ENROLLMENT_REQUESTED", instance=template, metadata={"staff_id": staff.pk})
    return template


def approve_enrollment(*, template: FaceTemplate, approver: User) -> FaceTemplate:
    if not has_capability(approver, "attendance.manage"):
        raise PermissionDenied("Approving facial enrollment requires attendance management rights.")
    if approver.pk == template.staff_id:
        log_action(actor=approver, action="FACE_ENROLLMENT_DENIED", instance=template,
                   metadata={"staff_id": template.staff_id, "reason": "self_approval"})
        raise PermissionDenied("Facial enrollment must be approved by a different user.")
    with transaction.atomic():
        locked = FaceTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status != FaceTemplate.Status.PENDING:
            raise ValidationError({"status": "Only pending enrollments can be approved."})
        for previous in FaceTemplate.objects.select_for_update().filter(staff_id=locked.staff_id, status=FaceTemplate.Status.ACTIVE):
            _clear(previous, FaceTemplate.Status.REVOKED, decided_by=approver, note="replaced by a newer approved enrollment")
        locked.status = FaceTemplate.Status.ACTIVE
        locked.decided_at = timezone.now()
        locked.decided_by = approver
        locked.expires_at = locked.decided_at + timedelta(days=retention_days())
        try:
            locked.save(update_fields=["status", "decided_at", "decided_by", "expires_at", "updated_at"])
        except IntegrityError as exc:  # pragma: no cover - guarded by the lock above
            raise ValidationError({"status": "This staff member already has an active enrollment."}) from exc
    log_action(actor=approver, action="FACE_ENROLLMENT_APPROVED", instance=locked, metadata={"staff_id": locked.staff_id})
    return locked


def reject_or_revoke(*, template: FaceTemplate, actor: User, revoke: bool, note: str = "") -> FaceTemplate:
    if not has_capability(actor, "attendance.manage"):
        raise PermissionDenied("Managing facial enrollment requires attendance management rights.")
    allowed = {FaceTemplate.Status.PENDING} | ({FaceTemplate.Status.ACTIVE} if revoke else set())
    with transaction.atomic():
        locked = FaceTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status not in allowed:
            raise ValidationError({"status": "This enrollment cannot be changed in its current state."})
        _clear(locked, FaceTemplate.Status.REVOKED if revoke else FaceTemplate.Status.REJECTED, decided_by=actor, note=note)
    action = "FACE_ENROLLMENT_REVOKED" if revoke else "FACE_ENROLLMENT_REJECTED"
    log_action(actor=actor, action=action, instance=locked, metadata={"staff_id": locked.staff_id})
    return locked


def expire_due_templates(now=None) -> int:
    now = now or timezone.now()
    count = 0
    for template in FaceTemplate.objects.filter(status=FaceTemplate.Status.ACTIVE, expires_at__lte=now):
        _clear(template, FaceTemplate.Status.EXPIRED, decided_by=None, note="retention period ended")
        count += 1
    return count


def _record(*, staff: User, action: str, outcome: str, source_key: str, distance_value=None) -> FaceVerificationAttempt:
    attempt, _ = FaceVerificationAttempt.objects.get_or_create(
        source_key=source_key,
        defaults={"staff": staff, "action": action, "outcome": outcome, "distance": distance_value},
    )
    return attempt


def verify_for_clock(*, staff: User, actor: User, action: str, source_key: str, probe, manual_reason: str = "") -> tuple[str, bool]:
    """Return ``(outcome, passed)``. Records every attempt; never writes attendance itself."""
    prior = FaceVerificationAttempt.objects.filter(source_key=source_key).first()
    if prior:
        return prior.outcome, prior.outcome in FaceOutcome.PASSING

    if manual_reason:
        # The clock endpoint is self-only, so the override is a supervisor's own-shift fallback.
        if actor.pk != staff.pk or not has_capability(actor, "attendance.manage"):
            raise PermissionDenied("Manual attendance fallback is limited to supervisors clocking their own shift.")
        reason = str(manual_reason).strip()
        if len(reason) < MIN_MANUAL_REASON_LENGTH:
            raise ValidationError({"manual_override_reason": f"Give a reason of at least {MIN_MANUAL_REASON_LENGTH} characters."})
        _record(staff=staff, action=action, outcome=FaceOutcome.MANUAL_OVERRIDE, source_key=source_key)
        log_action(actor=actor, action="ATTENDANCE_MANUAL_OVERRIDE", instance=None,
                   metadata={"staff_id": staff.pk, "action": action, "reason": reason[:200]})
        return FaceOutcome.MANUAL_OVERRIDE, True

    if probe is None:
        return FaceOutcome.FACE_REQUIRED, False

    try:
        values = validate_descriptor(probe)
    except ValidationError:
        _record(staff=staff, action=action, outcome=FaceOutcome.PROBE_INVALID, source_key=source_key)
        return FaceOutcome.PROBE_INVALID, False

    template = FaceTemplate.objects.filter(staff=staff, status=FaceTemplate.Status.ACTIVE).first()
    if template is None:
        _record(staff=staff, action=action, outcome=FaceOutcome.NOT_ENROLLED, source_key=source_key)
        return FaceOutcome.NOT_ENROLLED, False
    if template.expires_at and template.expires_at <= timezone.now():
        expire_due_templates()
        _record(staff=staff, action=action, outcome=FaceOutcome.TEMPLATE_EXPIRED, source_key=source_key)
        return FaceOutcome.TEMPLATE_EXPIRED, False

    gap = distance(values, list(template.descriptor))
    outcome = FaceOutcome.VERIFIED if gap <= match_threshold() else FaceOutcome.NO_MATCH
    _record(staff=staff, action=action, outcome=outcome, source_key=source_key, distance_value=round(gap, 4))
    return outcome, outcome == FaceOutcome.VERIFIED
