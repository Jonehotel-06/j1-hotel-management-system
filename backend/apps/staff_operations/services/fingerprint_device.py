"""Fingerprint READER device integration boundary (mocked or physical).

The attendance hardware required by the hotel is a fingerprint reader.  This
module is the software contract a vendor SDK or local capture agent uses to
submit captures from a reader bound to a workstation:

1. Management registers the reader (serial → workstation) — ``register_device``.
2. The agent heartbeats and reports faults — ``report_device_status``.
3. The agent submits captures — ``submit_device_capture``:
   ``(device serial, event nonce, captured_at, action, staff, minutiae probe)``.

Security properties enforced here (in addition to the matching logic in
``fingerprint_service``):

* Only ACTIVE devices bound to a registered workstation may submit.
* Every event carries a per-device nonce; replays are refused (unique
  constraint + source-key idempotency) and duplicate clock-ins follow the
  attendance state machine's own idempotency rules.
* ``captured_at`` must be fresh (bounded clock skew) so a captured-but-delayed
  event cannot be replayed later.
* The employee identity comes from the biometric match — the agent never
  asserts "who" without presenting a probe that matches an ACTIVE template.
* Raw fingerprint images are never accepted or stored; only validated ISO/IEC
  19794-2 minutiae probes are processed and outcomes recorded.
* Every attempt is written to the append-only ``FingerprintDeviceEvent`` log
  with device, outcome, score, attendance reference and timestamps.

Physical-device acceptance remains BLOCKED until a real reader is attached and
this contract is exercised against the actual hardware; the test suite drives
this boundary with a simulated (mocked) reader only.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action

from ..biometric_models import FingerprintDevice, FingerprintDeviceEvent
from .fingerprint_service import FingerprintOutcome, verify_fingerprint_for_clock
from .staff_operations_service import clock_attendance

# Maximum accepted age of a device-side capture timestamp (anti-replay window).
CAPTURE_SKEW = timedelta(minutes=10)


def _device_or_none(serial_number: str) -> FingerprintDevice | None:
    serial = str(serial_number or "").strip()
    if not serial:
        return None
    return FingerprintDevice.objects.select_related("workstation").filter(serial_number=serial).first()


@transaction.atomic
def register_device(*, serial_number: str, label: str, workstation, actor: User,
                    vendor: str = "", model_name: str = "", notes: str = "") -> FingerprintDevice:
    """Register a fingerprint reader bound to an authorised workstation."""
    if not has_capability(actor, "attendance.manage"):
        raise PermissionDenied("This user cannot register attendance devices.")
    serial = str(serial_number or "").strip()
    label = str(label or "").strip()
    if not serial or not label:
        raise ValidationError({"serial_number": "A device serial number and label are required."})
    existing = FingerprintDevice.objects.filter(serial_number=serial).first()
    if existing:
        return existing
    device = FingerprintDevice.objects.create(
        serial_number=serial, label=label, workstation=workstation,
        vendor=str(vendor or "")[:80], model_name=str(model_name or "")[:80],
        notes=str(notes or "")[:500], registered_by=actor,
    )
    log_action(actor=actor, action="FINGERPRINT_DEVICE_REGISTERED", instance=device,
               metadata={"serial_number": serial, "workstation": getattr(workstation, "name", "")})
    return device


@transaction.atomic
def report_device_status(*, serial_number: str, status: str, actor: User | None = None,
                         note: str = "") -> FingerprintDevice:
    """Heartbeat / fault reporting from the capture agent."""
    device = _device_or_none(serial_number)
    if device is None:
        raise ValidationError({"serial_number": "Unknown fingerprint device."})
    status = str(status or "").upper()
    if status not in FingerprintDevice.Status.values:
        raise ValidationError({"status": "Invalid device status."})
    device.status = status
    device.last_seen_at = timezone.now()
    device.last_error = (str(note or "")[:255] if status == FingerprintDevice.Status.FAULT else "")
    device.save(update_fields=["status", "last_seen_at", "last_error", "updated_at"])
    if actor is not None:
        log_action(actor=actor, action="FINGERPRINT_DEVICE_STATUS", instance=device,
                   metadata={"status": status, "note": device.last_error})
    return device


def submit_device_capture(*, serial_number: str, event_nonce: str, captured_at, action: str,
                          staff: User, probe, actor: User | None = None):
    """Process one capture event from a bound reader.

    Returns ``(event, outcome, passed, attendance_record_or_None)``.
    Raises ValidationError for replays, stale captures, unknown or inactive
    devices; refused verifications return a failed outcome with the event
    recorded (attendance is NOT recorded).

    Not wrapped in one outer transaction on purpose: refused attendance
    transitions must leave their audit event committed even though the error
    surfaces to the device agent.
    """
    device = _device_or_none(serial_number)
    if device is None:
        raise ValidationError({"serial_number": "Unknown fingerprint device."})
    if device.status != FingerprintDevice.Status.ACTIVE:
        raise ValidationError({"serial_number": "This fingerprint device is not active."})

    nonce = str(event_nonce or "").strip()
    if not nonce or len(nonce) > 80:
        raise ValidationError({"event_nonce": "A capture event nonce is required (max 80 chars)."})
    source_key = f"fp-device:{device.serial_number}:{nonce}"
    if FingerprintDeviceEvent.objects.filter(source_key=source_key).exists():
        raise ValidationError({"event_nonce": "This capture event has already been processed (replay refused)."})

    try:
        captured = captured_at if timezone.is_aware(captured_at) else timezone.make_aware(captured_at)
    except Exception as exc:
        raise ValidationError({"captured_at": "A valid capture timestamp is required."}) from exc
    now = timezone.now()
    if captured > now + CAPTURE_SKEW or captured < now - CAPTURE_SKEW:
        raise ValidationError({"captured_at": "Capture timestamp is outside the accepted window (replay refused)."})

    action = str(action or "").upper()
    identified_staff = User.objects.filter(pk=staff.pk).first() if staff else None
    if identified_staff is None:
        raise ValidationError({"staff": "A staff member must be identified for the capture."})

    outcome, passed = verify_fingerprint_for_clock(
        staff=identified_staff,
        actor=actor or identified_staff,
        action=action or "CLOCK_IN",
        source_key=source_key,
        probe=probe,
    )
    match_score = None
    if passed and probe is not None:
        # score is recorded by verify_fingerprint_for_clock; read it back.
        from ..biometric_models import FingerprintVerificationAttempt

        attempt = FingerprintVerificationAttempt.objects.filter(source_key=source_key).first()
        match_score = getattr(attempt, "match_score", None)

    attendance_record = None
    attendance_reference = ""
    attendance_error = ""
    if passed and action:
        # The biometric match IS the employee's own authorisation: clocking is
        # attributed to the identified staff member, never to an operator.
        try:
            attendance_record, _created = clock_attendance(
                staff=identified_staff, action=action,
                actor=identified_staff,
                idempotency_key=f"{device.serial_number}:{nonce}",
            )
            attendance_reference = attendance_record.reference
        except ValidationError as exc:
            # Duplicate/invalid clock transitions are refused by the attendance
            # state machine — the attempt is still audited before the refusal
            # surfaces to the device agent.
            attendance_error = "; ".join(
                exc.messages if hasattr(exc, "messages") else [str(exc)]
            )[:255]

    event = FingerprintDeviceEvent.objects.create(
        device=device, event_nonce=nonce, staff=identified_staff,
        action=action, captured_at=captured,
        outcome=outcome, match_score=match_score,
        attendance_reference=attendance_reference, source_key=source_key,
        details={"workstation": getattr(device.workstation, "name", ""),
                 **({"attendance_error": attendance_error} if attendance_error else {})},
    )
    device.last_seen_at = now
    device.save(update_fields=["last_seen_at", "updated_at"])
    log_action(
        actor=actor or identified_staff,
        action="FINGERPRINT_DEVICE_CAPTURE",
        instance=event,
        metadata={
            "device": device.serial_number,
            "outcome": outcome,
            "passed": passed,
            "attendance_reference": attendance_reference,
            **({"attendance_error": attendance_error} if attendance_error else {}),
        },
    )
    if attendance_error:
        raise ValidationError({"attendance": attendance_error})
    return event, outcome, passed, attendance_record
