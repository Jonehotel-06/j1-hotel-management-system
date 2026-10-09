"""Fingerprint verification & temporary workstation credential service.

ISO/IEC 19794-2 & ANSI/NIST-ITL minutiae template matching:
- Validates minutiae feature sets (x, y coordinates, angle/direction, type: ridge ending/bifurcation, quality).
- Alternatively accepts compact standardized feature vectors (48 to 256 dimensions).
- Verifies match score against configurable threshold (default score >= 70.0).
- Issues temporary workstation login credentials upon verified biometric authentication.
- Never stores raw fingerprint images.
"""
from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User, Workstation
from apps.accounts.desktop_policy import receptionist_desktop_for_key, hash_desktop_key
from apps.audit.services import log_action

from ..biometric_models import (
    FingerprintTemplate,
    FingerprintVerificationAttempt,
    TemporaryWorkstationCredential,
)

MIN_MINUTIAE_POINTS = 8
MAX_MINUTIAE_POINTS = 200
DEFAULT_MATCH_THRESHOLD = 70.0  # Min score out of 100
DEFAULT_TEMP_CRED_TTL_MINUTES = 15


class FingerprintOutcome:
    VERIFIED = "VERIFIED"
    NO_MATCH = "NO_MATCH"
    NOT_ENROLLED = "NOT_ENROLLED"
    TEMPLATE_EXPIRED = "TEMPLATE_EXPIRED"
    PROBE_INVALID = "PROBE_INVALID"
    BIOMETRIC_REQUIRED = "BIOMETRIC_REQUIRED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

    PASSING = frozenset({VERIFIED, MANUAL_OVERRIDE})


def fingerprint_verification_required() -> bool:
    return bool(getattr(settings, "STAFF_FINGERPRINT_VERIFICATION_REQUIRED", False)) or bool(
        getattr(settings, "STAFF_BIOMETRIC_VERIFICATION_REQUIRED", False)
    )


def match_score_threshold() -> float:
    return float(getattr(settings, "FINGERPRINT_MATCH_MIN_SCORE", DEFAULT_MATCH_THRESHOLD))


def retention_days() -> int:
    return int(getattr(settings, "FINGERPRINT_TEMPLATE_RETENTION_DAYS", 365))


def biometric_clock_source_key(*, staff: User, action: str, raw_key: str) -> str:
    digest = hashlib.sha256(f"fp:{staff.pk}:{action}:{raw_key}".encode()).hexdigest()
    return f"fp-clock:{digest}"


def validate_minutiae_data(raw: Any) -> list[dict] | list[float]:
    """Validate ISO/IEC 19794-2 minutiae points or normalized feature vector."""
    if not isinstance(raw, (list, tuple)):
        raise ValidationError({"minutiae": "Fingerprint template minutiae must be a list of points or feature vector."})

    if not raw:
        raise ValidationError({"minutiae": "Fingerprint template minutiae cannot be empty."})

    # Case 1: List of minutiae point dictionaries: [{"x": int, "y": int, "angle": float, "type": "ending"|"bifurcation", "quality": int}]
    if isinstance(raw[0], dict):
        if len(raw) < MIN_MINUTIAE_POINTS:
            raise ValidationError({"minutiae": f"At least {MIN_MINUTIAE_POINTS} minutiae points required."})
        if len(raw) > MAX_MINUTIAE_POINTS:
            raise ValidationError({"minutiae": f"Maximum {MAX_MINUTIAE_POINTS} minutiae points allowed."})

        cleaned_points = []
        for idx, pt in enumerate(raw):
            if not isinstance(pt, dict):
                raise ValidationError({"minutiae": f"Minutiae point at index {idx} must be a dictionary."})
            try:
                x = float(pt.get("x", 0))
                y = float(pt.get("y", 0))
                angle = float(pt.get("angle", 0)) % 360
                m_type = str(pt.get("type", "ending")).lower()
                if m_type not in ("ending", "bifurcation", "other"):
                    m_type = "ending"
                quality = int(pt.get("quality", 80))
            except (ValueError, TypeError) as exc:
                raise ValidationError({"minutiae": f"Invalid minutiae data at index {idx}."}) from exc

            cleaned_points.append({
                "x": round(x, 2),
                "y": round(y, 2),
                "angle": round(angle, 2),
                "type": m_type,
                "quality": max(1, min(100, quality)),
            })
        return cleaned_points

    # Case 2: Normalized numeric feature vector (e.g. 64-128 dimensional minutiae descriptor)
    if isinstance(raw[0], (int, float)):
        if len(raw) < 16 or len(raw) > 512:
            raise ValidationError({"minutiae": "Feature vector must contain between 16 and 512 numbers."})
        cleaned_vector = []
        for idx, item in enumerate(raw):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValidationError({"minutiae": f"Vector element {idx} must be numeric."})
            val = float(item)
            if not math.isfinite(val):
                raise ValidationError({"minutiae": "Vector elements must be finite."})
            cleaned_vector.append(round(val, 6))
        return cleaned_vector

    raise ValidationError({"minutiae": "Unrecognized minutiae data format."})


def compute_minutiae_match_score(enrolled: list, probe: list) -> float:
    """Compute matching score (0.0 to 100.0) between enrolled minutiae and probe."""
    if not enrolled or not probe:
        return 0.0

    # If numeric feature vectors: compute cosine similarity
    if isinstance(enrolled[0], (int, float)) and isinstance(probe[0], (int, float)):
        if len(enrolled) != len(probe):
            return 0.0
        dot = sum(a * b for a, b in zip(enrolled, probe))
        norm_a = math.sqrt(sum(a * a for a in enrolled))
        norm_b = math.sqrt(sum(b * b for b in probe))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        similarity = dot / (norm_a * norm_b)
        # Scale [-1, 1] to [0, 100]
        score = max(0.0, min(100.0, (similarity + 1.0) * 50.0))
        return round(score, 2)

    # If ISO minutiae point lists: evaluate point pairing within spatial tolerance
    if isinstance(enrolled[0], dict) and isinstance(probe[0], dict):
        spatial_tolerance_sq = 20.0 ** 2  # 20 pixels tolerance
        angle_tolerance = 25.0  # 25 degrees tolerance

        matches = 0
        used_probe_indices = set()

        for pt_e in enrolled:
            xe, ye, ae = pt_e["x"], pt_e["y"], pt_e["angle"]
            for idx_p, pt_p in enumerate(probe):
                if idx_p in used_probe_indices:
                    continue
                xp, yp, ap = pt_p["x"], pt_p["y"], pt_p["angle"]
                dist_sq = (xe - xp) ** 2 + (ye - yp) ** 2
                if dist_sq <= spatial_tolerance_sq:
                    angle_diff = abs(ae - ap) % 360
                    if angle_diff > 180:
                        angle_diff = 360 - angle_diff
                    if angle_diff <= angle_tolerance:
                        matches += 1
                        used_probe_indices.add(idx_p)
                        break

        # Standard biometrics match formula: 2 * matches / (N_enrolled + N_probe)
        denominator = len(enrolled) + len(probe)
        if denominator == 0:
            return 0.0
        score = (2.0 * matches / denominator) * 100.0
        return round(min(100.0, score), 2)

    return 0.0


def request_fingerprint_enrollment(*, staff: User, actor: User, minutiae, finger_position="RIGHT_INDEX") -> FingerprintTemplate:
    if actor.pk != staff.pk:
        log_action(actor=actor, action="FINGERPRINT_ENROLLMENT_DENIED", instance=None,
                   metadata={"staff_id": staff.pk, "reason": "cross_account"})
        raise PermissionDenied("Fingerprint enrollment can only be requested for your own account.")

    data = validate_minutiae_data(minutiae)
    with transaction.atomic():
        # Lock staff rows
        list(FingerprintTemplate.objects.select_for_update().filter(staff=staff).values_list("pk", flat=True))
        for old in FingerprintTemplate.objects.filter(staff=staff, status=FingerprintTemplate.Status.PENDING):
            old.status = FingerprintTemplate.Status.REJECTED
            old.clear_minutiae()
            old.decision_note = "superseded by a newer request"
            old.decided_at = timezone.now()
            old.save()

        template = FingerprintTemplate.objects.create(
            staff=staff,
            status=FingerprintTemplate.Status.PENDING,
            finger_position=finger_position,
            minutiae_data=data,
            template_format="ISO_19794_2_MINUTIAE" if isinstance(data[0], dict) else "STANDARDIZED_FEATURE_VECTOR",
        )
    log_action(actor=actor, action="FINGERPRINT_ENROLLMENT_REQUESTED", instance=template, metadata={"staff_id": staff.pk})
    return template


def approve_fingerprint_enrollment(*, template: FingerprintTemplate, approver: User) -> FingerprintTemplate:
    if not has_capability(approver, "attendance.manage"):
        raise PermissionDenied("Approving fingerprint enrollment requires attendance management rights.")
    if approver.pk == template.staff_id:
        log_action(actor=approver, action="FINGERPRINT_ENROLLMENT_DENIED", instance=template,
                   metadata={"staff_id": template.staff_id, "reason": "self_approval"})
        raise PermissionDenied("Fingerprint enrollment must be approved by a different supervisor.")

    with transaction.atomic():
        locked = FingerprintTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status != FingerprintTemplate.Status.PENDING:
            raise ValidationError({"status": "Only pending fingerprint enrollments can be approved."})

        for prev in FingerprintTemplate.objects.select_for_update().filter(staff_id=locked.staff_id, status=FingerprintTemplate.Status.ACTIVE):
            prev.status = FingerprintTemplate.Status.REVOKED
            prev.clear_minutiae()
            prev.decided_by = approver
            prev.decided_at = timezone.now()
            prev.decision_note = "replaced by newly approved fingerprint enrollment"
            prev.save()

        locked.status = FingerprintTemplate.Status.ACTIVE
        locked.decided_at = timezone.now()
        locked.decided_by = approver
        locked.expires_at = locked.decided_at + timedelta(days=retention_days())
        locked.save()

    log_action(actor=approver, action="FINGERPRINT_ENROLLMENT_APPROVED", instance=locked, metadata={"staff_id": locked.staff_id})
    return locked


def verify_fingerprint_for_clock(*, staff: User, actor: User, action: str, source_key: str, probe, manual_reason: str = "") -> tuple[str, bool]:
    """Verify staff fingerprint minutiae probe. Returns (outcome, passed)."""
    prior = FingerprintVerificationAttempt.objects.filter(source_key=source_key).first()
    if prior:
        return prior.outcome, prior.outcome in FingerprintOutcome.PASSING

    if manual_reason:
        if actor.pk != staff.pk or not has_capability(actor, "attendance.manage"):
            raise PermissionDenied("Manual attendance fallback is limited to supervisors clocking their own shift.")
        reason = str(manual_reason).strip()
        if len(reason) < 10:
            raise ValidationError({"manual_override_reason": "Give a reason of at least 10 characters."})
        FingerprintVerificationAttempt.objects.create(
            staff=staff, action=action, outcome=FingerprintOutcome.MANUAL_OVERRIDE, source_key=source_key
        )
        log_action(actor=actor, action="ATTENDANCE_MANUAL_OVERRIDE", instance=None,
                   metadata={"staff_id": staff.pk, "action": action, "reason": reason[:200]})
        return FingerprintOutcome.MANUAL_OVERRIDE, True

    if probe is None:
        return FingerprintOutcome.BIOMETRIC_REQUIRED, False

    try:
        probe_data = validate_minutiae_data(probe)
    except ValidationError:
        FingerprintVerificationAttempt.objects.create(
            staff=staff, action=action, outcome=FingerprintOutcome.PROBE_INVALID, source_key=source_key
        )
        return FingerprintOutcome.PROBE_INVALID, False

    active_template = FingerprintTemplate.objects.filter(staff=staff, status=FingerprintTemplate.Status.ACTIVE).first()
    if not active_template:
        FingerprintVerificationAttempt.objects.create(
            staff=staff, action=action, outcome=FingerprintOutcome.NOT_ENROLLED, source_key=source_key
        )
        return FingerprintOutcome.NOT_ENROLLED, False

    if active_template.expires_at and active_template.expires_at <= timezone.now():
        active_template.status = FingerprintTemplate.Status.EXPIRED
        active_template.clear_minutiae()
        active_template.save()
        FingerprintVerificationAttempt.objects.create(
            staff=staff, action=action, outcome=FingerprintOutcome.TEMPLATE_EXPIRED, source_key=source_key
        )
        return FingerprintOutcome.TEMPLATE_EXPIRED, False

    score = compute_minutiae_match_score(active_template.minutiae_data, probe_data)
    passed = score >= match_score_threshold()
    outcome = FingerprintOutcome.VERIFIED if passed else FingerprintOutcome.NO_MATCH

    FingerprintVerificationAttempt.objects.create(
        staff=staff, action=action, outcome=outcome, match_score=score, source_key=source_key
    )
    return outcome, passed


def issue_temporary_workstation_credential(*, staff: User, workstation_reference: str, probe) -> tuple[str, str]:
    """Verify biometric fingerprint at workstation terminal and mint temporary sign-in credential.
    
    Returns (raw_temp_token, expires_at_iso).
    """
    try:
        probe_data = validate_minutiae_data(probe)
    except ValidationError as exc:
        raise ValidationError({"probe": "Invalid fingerprint minutiae probe."}) from exc

    active_template = FingerprintTemplate.objects.filter(staff=staff, status=FingerprintTemplate.Status.ACTIVE).first()
    if not active_template:
        raise ValidationError({"biometrics": "Staff member has no active fingerprint enrollment."})

    score = compute_minutiae_match_score(active_template.minutiae_data, probe_data)
    if score < match_score_threshold():
        log_action(actor=staff, action="BIOMETRIC_LOGIN_REFUSED", instance=None,
                   metadata={"staff_id": staff.pk, "score": score, "workstation": workstation_reference})
        raise PermissionDenied(f"Biometric fingerprint match failed (score {score:.1f}).")

    raw_token = f"jone-temp-{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    ttl = int(getattr(settings, "TEMPORARY_WORKSTATION_CREDENTIAL_TTL_MINUTES", DEFAULT_TEMP_CRED_TTL_MINUTES))
    expires_at = timezone.now() + timedelta(minutes=ttl)

    with transaction.atomic():
        # Invalidate old unused temporary credentials for this user
        TemporaryWorkstationCredential.objects.filter(staff=staff, is_used=False).update(is_used=True)
        cred = TemporaryWorkstationCredential.objects.create(
            staff=staff,
            workstation_reference=workstation_reference,
            credential_token_hash=token_hash,
            expires_at=expires_at,
        )

    log_action(actor=staff, action="TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED", instance=cred,
               metadata={"staff_id": staff.pk, "workstation": workstation_reference})
    return raw_token, expires_at.isoformat()


def consume_temporary_workstation_credential(raw_token: str) -> User | None:
    """Consume a temporary workstation sign-in token."""
    if not raw_token or not raw_token.startswith("jone-temp-"):
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with transaction.atomic():
        cred = TemporaryWorkstationCredential.objects.select_for_update().filter(
            credential_token_hash=token_hash,
            is_used=False,
            expires_at__gt=timezone.now(),
        ).first()
        if not cred:
            return None
        cred.is_used = True
        cred.used_at = timezone.now()
        cred.save(update_fields=["is_used", "used_at", "updated_at"])
        return cred.staff
