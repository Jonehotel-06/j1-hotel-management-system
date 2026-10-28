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
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.approvals import override_note, require_independent_approver
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

# Supervisor attendance fallback (audit N6). A recorded biometric failure is
# what makes a *manager* fallback legitimate; administrators may authorise one
# without a prior attempt because they carry the privileged exception. Ordinary
# employees can never issue a fallback code, for anyone.
DEFAULT_MANUAL_FALLBACK_WINDOW_MINUTES = 30
MIN_MANUAL_REASON_LENGTH = 10



class FingerprintOutcome:
    VERIFIED = "VERIFIED"
    NO_MATCH = "NO_MATCH"
    NOT_ENROLLED = "NOT_ENROLLED"
    TEMPLATE_EXPIRED = "TEMPLATE_EXPIRED"
    PROBE_INVALID = "PROBE_INVALID"
    BIOMETRIC_REQUIRED = "BIOMETRIC_REQUIRED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

    PASSING = frozenset({VERIFIED, MANUAL_OVERRIDE})
    # Capture / hardware failures. These are TECHNICAL: they say nothing about
    # whether the finger matched, which is why they – and only they – can make a
    # manager attendance fallback legitimate.
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    DEVICE_FAULT = "DEVICE_FAULT"
    CAPTURE_TIMEOUT = "CAPTURE_TIMEOUT"
    UNSUPPORTED_DEVICE = "UNSUPPORTED_DEVICE"
    INTEGRATION_ERROR = "INTEGRATION_ERROR"


# The classifications an approved workstation may report when the reader could
# not produce a probe at all. Nothing here claims a fingerprint was captured.
FALLBACK_FAILURE_CLASSES = frozenset({
    FingerprintOutcome.DEVICE_UNAVAILABLE,
    FingerprintOutcome.DEVICE_FAULT,
    FingerprintOutcome.CAPTURE_TIMEOUT,
    FingerprintOutcome.UNSUPPORTED_DEVICE,
    FingerprintOutcome.INTEGRATION_ERROR,
})


# What a manager fallback may be based on. A plain mismatch (NO_MATCH) is
# deliberately absent – a wrong finger is not a hardware failure – and so is
# NOT_ENROLLED/TEMPLATE_EXPIRED, which are closed by enrolling, not by handing
# out a fallback code. An invalid/incomplete capture and every reported capture
# or integration failure are included (audit N7).
QUALIFYING_FAILURE_OUTCOMES = FALLBACK_FAILURE_CLASSES | frozenset({FingerprintOutcome.PROBE_INVALID})


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
    can_manage_enrollment = has_capability(actor, "attendance.manage") or has_capability(actor, "staff.manage")
    if actor.pk != staff.pk and not can_manage_enrollment:
        log_action(actor=actor, action="FINGERPRINT_ENROLLMENT_DENIED", instance=None,
                   metadata={"staff_id": staff.pk, "reason": "cross_account"})
        raise PermissionDenied("Fingerprint enrollment can only be requested for your own account or by authorised HR/attendance management.")

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
    # Centralised maker-checker policy. Ordinary supervisors still need a second
    # person; an administrator enrolling their own fingerprint is permitted with
    # recorded override evidence (see apps.core.approvals).
    try:
        override = require_independent_approver(
            requester=template.staff, actor=approver, workflow="fingerprint enrollment",
        )
    except PermissionDenied:
        log_action(actor=approver, action="FINGERPRINT_ENROLLMENT_DENIED", instance=template,
                   metadata={"staff_id": template.staff_id, "reason": "self_approval"})
        raise

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
        if override:
            locked.decision_note = override_note(locked.decision_note, override)
            locked.save(update_fields=["decision_note"])
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


def _hash_numeric_code(raw_code: str) -> str:
    return hashlib.sha256(f"jone_ws_numcode_v1:{str(raw_code or '').strip()}".encode("utf-8")).hexdigest()


def _generate_numeric_access_code() -> str:
    """Generate a cryptographically random 6-digit numeric-only workstation code."""
    for _ in range(30):
        candidate = f"{secrets.randbelow(900000) + 100000:06d}"
        code_hash = _hash_numeric_code(candidate)
        if not TemporaryWorkstationCredential.objects.filter(
            numeric_code_hash=code_hash, session_active=True
        ).exists():
            return candidate
    return f"{secrets.randbelow(900000) + 100000:06d}"


def staff_department_code(staff: User) -> str:
    """Resolve canonical department code for a staff member."""
    profile = getattr(staff, "staff_profile", None)
    if profile is not None:
        if getattr(profile, "department_ref_id", None) and getattr(profile, "department_ref", None):
            return profile.department_ref.code
        raw_dept = (profile.department or "").strip().upper()
        dept_aliases = {
            "MANAGEMENT": "MANAGEMENT",
            "GENERAL MANAGER": "MANAGEMENT",
            "GENERAL_MANAGER": "MANAGEMENT",
            "ACCOUNTS": "ACCOUNTS",
            "ACCOUNTANT": "ACCOUNTS",
            "ACCOUNTS & FINANCE": "ACCOUNTS",
            "FINANCE": "ACCOUNTS",
            "ADMINISTRATION & PROCUREMENT": "ADMIN_PROCUREMENT",
            "ADMIN MANAGER/PROCUREMENT": "ADMIN_PROCUREMENT",
            "ADMIN_MANAGER_PROCUREMENT": "ADMIN_PROCUREMENT",
            "ADMIN_PROCUREMENT": "ADMIN_PROCUREMENT",
            "PROCUREMENT": "ADMIN_PROCUREMENT",
            "ADMINISTRATION": "ADMIN_PROCUREMENT",
            "HR": "ADMIN_PROCUREMENT",
            "HUMAN RESOURCES": "ADMIN_PROCUREMENT",
            "HUMAN_RESOURCES": "ADMIN_PROCUREMENT",
            "FRONT DESK": "FRONT_DESK",
            "FRONT DESK / RECEPTION": "FRONT_DESK",
            "FRONT_DESK": "FRONT_DESK",
            "RECEPTION": "FRONT_DESK",
            "RECEPTIONIST": "FRONT_DESK",
            "KITCHEN": "KITCHEN",
            "CHEFS": "KITCHEN",
            "CHEF": "KITCHEN",
            "KITCHEN ASSISTANT": "KITCHEN_ASSISTANT",
            "KITCHEN_ASSISTANT": "KITCHEN_ASSISTANT",
            # Historical Cashier & Bar department labels resolve to Waiters/Waitresses
            # for stored-profile sign-in checks; no standalone Cashier or Bar portal exists.
            "CASHIER": "RESTAURANT_BAR",
            "CASHIER_TILL": "RESTAURANT_BAR",
            "CASHIER TILL": "RESTAURANT_BAR",
            "CASHIER / TILL": "RESTAURANT_BAR",
            "RESTAURANT": "RESTAURANT_BAR",
            "WAITERS/WAITRESSES": "RESTAURANT_BAR",
            "WAITERS_WAITRESSES": "RESTAURANT_BAR",
            "BAR": "RESTAURANT_BAR",
            "RESTAURANT & BAR": "RESTAURANT_BAR",
            "RESTAURANT & BAR SERVICE": "RESTAURANT_BAR",
            "RESTAURANT_BAR": "RESTAURANT_BAR",
            "HOUSEKEEPING": "HOUSEKEEPING",
            "HOUSEKEEPERS": "HOUSEKEEPING",
            "MAINTENANCE": "HOUSEKEEPING",
            "PORTERING": "PORTERING",
            "PORTER": "PORTERING",
            "PORTERS": "PORTERING",
            "STORES": "STORES_INVENTORY",
            "INVENTORY": "STORES_INVENTORY",
            "STORE KEEPER": "STORES_INVENTORY",
            "STORE_KEEPER": "STORES_INVENTORY",
            "STOREKEEPER": "STORES_INVENTORY",
            "STORES & INVENTORY": "STORES_INVENTORY",
            "STORES_INVENTORY": "STORES_INVENTORY",
            "SECURITY": "SECURITY",
        }
        if raw_dept in dept_aliases:
            return dept_aliases[raw_dept]
        if raw_dept:
            return raw_dept

    role = getattr(staff, "role", "") or ""
    role_map = {
        "ADMIN": "ADMIN_PROCUREMENT",
        "GENERAL_MANAGER": "MANAGEMENT",
        "MANAGER": "MANAGEMENT",
        "HR_MANAGER": "ADMIN_PROCUREMENT",
        "HR_OFFICER": "ADMIN_PROCUREMENT",
        "ACCOUNTS_MANAGER": "ACCOUNTS",
        "ACCOUNTANT": "ACCOUNTS",
        # CASHIER is retired: a historical account resolves to Waiters/Waitresses.
        "CASHIER": "RESTAURANT_BAR",
        "PROCUREMENT_OFFICER": "ADMIN_PROCUREMENT",
        "FRONT_DESK_SUPERVISOR": "FRONT_DESK",
        "RECEPTIONIST": "FRONT_DESK",
        "KITCHEN_MANAGER": "KITCHEN",
        "CHEF": "KITCHEN",
        "KITCHEN_ASSISTANT": "KITCHEN_ASSISTANT",
        "RESTAURANT_MANAGER": "RESTAURANT_BAR",
        "WAITER": "RESTAURANT_BAR",
        "BAR_MANAGER": "RESTAURANT_BAR",
        "BARTENDER": "RESTAURANT_BAR",
        "HOUSEKEEPING_MANAGER": "HOUSEKEEPING",
        "HOUSEKEEPING": "HOUSEKEEPING",
        "HOUSEKEEPER": "HOUSEKEEPING",
        "MAINTENANCE": "HOUSEKEEPING",
        "MAINTENANCE_TECHNICIAN": "HOUSEKEEPING",
        "PORTER": "PORTERING",
        "SECURITY": "SECURITY",
        "STOREKEEPER": "STORES_INVENTORY",
        "INVENTORY_CLERK": "STORES_INVENTORY",
    }
    if role in role_map:
        return role_map[role]
    from apps.accounts.capabilities import is_housekeeping_worker, is_maintenance_worker

    if is_housekeeping_worker(staff) or is_maintenance_worker(staff):
        return "HOUSEKEEPING"
    return "SHARED"


def is_staff_authorized_for_department(staff: User, target_department: str) -> bool:
    """Check whether a staff member is allowed to access a given department / subdomain."""
    target = (target_department or "").strip().upper()
    if not target or target == "SHARED":
        return True
    if getattr(staff, "role", "") == User.Role.ADMIN:
        return True
    subdomain_to_dept = {
        "MANAGER": {"MANAGEMENT"},
        "MANAGEMENT": {"MANAGEMENT"},
        "GENERAL_MANAGER": {"MANAGEMENT"},
        "GENERAL-MANAGER": {"MANAGEMENT"},
        "ACCOUNTANT": {"ACCOUNTS"},
        "ACCOUNTS": {"ACCOUNTS"},
        "ADMIN": {"ADMIN_PROCUREMENT"},
        "PROCUREMENT": {"ADMIN_PROCUREMENT"},
        "ADMIN_PROCUREMENT": {"ADMIN_PROCUREMENT"},
        "ADMIN-MANAGER-PROCUREMENT": {"ADMIN_PROCUREMENT"},
        "FRONTDESK": {"FRONT_DESK"},
        "FRONT_DESK": {"FRONT_DESK"},
        "RECEPTION": {"FRONT_DESK"},
        "RECEPTIONIST": {"FRONT_DESK"},
        "KITCHEN": {"KITCHEN"},
        "CHEFS": {"KITCHEN"},
        "KITCHEN_ASSISTANT": {"KITCHEN_ASSISTANT"},
        "KITCHEN-ASSISTANT": {"KITCHEN_ASSISTANT"},
        "RESTAURANT": {"RESTAURANT_BAR"},
        "BAR": {"RESTAURANT_BAR"},
        "RESTAURANT_BAR": {"RESTAURANT_BAR"},
        "WAITERS": {"RESTAURANT_BAR"},
        "WAITERS_WAITRESSES": {"RESTAURANT_BAR"},
        "WAITERS-WAITRESSES": {"RESTAURANT_BAR"},
        "HOUSEKEEPING": {"HOUSEKEEPING"},
        "HOUSEKEEPERS": {"HOUSEKEEPING"},
        "MAINTENANCE": {"HOUSEKEEPING"},
        "PORTER": {"PORTERING"},
        "PORTERS": {"PORTERING"},
        "PORTERING": {"PORTERING"},
        "STORE": {"STORES_INVENTORY"},
        "STORES": {"STORES_INVENTORY"},
        "STORE_KEEPER": {"STORES_INVENTORY"},
        "STORE-KEEPER": {"STORES_INVENTORY"},
        "INVENTORY": {"STORES_INVENTORY"},
        "STORES_INVENTORY": {"STORES_INVENTORY"},
        "SECURITY": {"SECURITY"},
    }
    allowed_depts = subdomain_to_dept.get(target, {target})
    staff_dept = staff_department_code(staff)
    if staff_dept in allowed_depts:
        return True
    # General Manager / Manager can access management dashboard and operational oversight,
    # except strictly Administrator-only "ADMIN" subdomain unless they hold ADMIN role.
    if getattr(staff, "role", "") in (User.Role.GENERAL_MANAGER, User.Role.MANAGER) and target in (
        "MANAGER", "MANAGEMENT", "GENERAL_MANAGER", "GENERAL-MANAGER", "PROCUREMENT", "ADMIN_PROCUREMENT",
    ):
        return True
    return False


@transaction.atomic
def revoke_workstation_credentials_on_clock_out(*, staff: User, reason: str = "CLOCKED_OUT") -> int:
    """Immediately and transactionally expire/invalidate all workstation codes for a staff member."""
    from django.db.models import Q

    now = timezone.now()
    qs = TemporaryWorkstationCredential.objects.select_for_update().filter(
        staff=staff
    ).filter(Q(session_active=True) | Q(is_used=False) | Q(expires_at__gt=now))
    count = qs.update(
        session_active=False,
        is_used=True,
        revoked_at=now,
        revoked_reason=reason,
        expires_at=now,
    )
    if count:
        log_action(
            actor=staff,
            action="WORKSTATION_CREDENTIALS_REVOKED_ON_CLOCK_OUT",
            instance=None,
            metadata={"staff_id": staff.pk, "revoked_count": count, "reason": reason},
        )
    return count


def get_staff_biometric_status(staff: User) -> dict:
    """Return comprehensive biometric & attendance summary for Staff Profile and Reception Terminal."""
    from ..models import AttendanceRecord

    now = timezone.now()
    templates = list(
        FingerprintTemplate.objects.filter(staff=staff)
        .select_related("decided_by")
        .order_by("-requested_at", "-id")
    )
    active_tmpl = next((t for t in templates if t.status == FingerprintTemplate.Status.ACTIVE), None)
    pending_tmpl = next((t for t in templates if t.status == FingerprintTemplate.Status.PENDING), None)
    revoked_tmpl = next((t for t in templates if t.status == FingerprintTemplate.Status.REVOKED), None)

    if active_tmpl:
        enrollment_status = "ENROLLED"
    elif pending_tmpl:
        enrollment_status = "PENDING_APPROVAL"
    elif revoked_tmpl:
        enrollment_status = "REVOKED"
    else:
        enrollment_status = "NOT_ENROLLED"

    attempts = list(
        FingerprintVerificationAttempt.objects.filter(staff=staff).order_by("-created_at", "-id")[:25]
    )
    last_verified = next(
        (a for a in attempts if a.outcome in FingerprintOutcome.PASSING),
        None,
    )
    failed_count = FingerprintVerificationAttempt.objects.filter(
        staff=staff
    ).exclude(outcome__in=FingerprintOutcome.PASSING).count()

    if attempts:
        verification_status = (
            "VERIFIED" if attempts[0].outcome in FingerprintOutcome.PASSING else "VERIFICATION_FAILED"
        )
    else:
        verification_status = "NEVER_VERIFIED"

    open_att = (
        AttendanceRecord.objects.filter(staff=staff, clock_out_at__isnull=True)
        .order_by("-clock_in_at")
        .first()
    )
    active_cred = (
        TemporaryWorkstationCredential.objects.filter(
            staff=staff,
            session_active=True,
            revoked_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )

    def _tmpl_info(t: FingerprintTemplate | None):
        if t is None:
            return None
        return {
            "id": t.pk,
            "status": t.status,
            "finger_position": t.finger_position,
            "template_format": t.template_format,
            "requested_at": t.requested_at.isoformat() if t.requested_at else None,
            "decided_at": t.decided_at.isoformat() if t.decided_at else None,
            "decided_by_id": t.decided_by_id,
            "decided_by_name": t.decided_by.full_name if t.decided_by else "",
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "decision_note": t.decision_note,
        }

    return {
        "enrollment_status": enrollment_status,
        "verification_status": verification_status,
        "enrolled": active_tmpl is not None,
        "active_template": _tmpl_info(active_tmpl),
        "pending_template": _tmpl_info(pending_tmpl),
        "latest_template": _tmpl_info(templates[0] if templates else None),
        "templates_count": len(templates),
        "last_verified_at": last_verified.created_at.isoformat() if last_verified else None,
        "last_match_score": last_verified.match_score if last_verified else None,
        "last_verification_workstation": last_verified.workstation_reference if last_verified else "",
        "failed_attempts_count": failed_count,
        "recent_attempts": [
            {
                "id": a.pk,
                "action": a.action,
                "outcome": a.outcome,
                "match_score": a.match_score,
                "workstation_reference": a.workstation_reference,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in attempts[:10]
        ],
        "attendance_status": open_att.status if open_att else "NOT_CLOCKED_IN",
        "active_attendance_reference": open_att.reference if open_att else None,
        "clock_in_at": open_att.clock_in_at.isoformat() if open_att and open_att.clock_in_at else None,
        "has_active_access_code": bool(active_cred and (not open_att or open_att.clock_out_at is None)),
        "active_workstation_reference": active_cred.workstation_reference if active_cred else "",
        "active_department": active_cred.department if active_cred else staff_department_code(staff),
        "active_code_expires_at": active_cred.expires_at.isoformat() if active_cred else None,
    }


def mint_temporary_workstation_credential(
    *,
    staff: User,
    workstation_reference: str,
    attendance_record=None,
    issue_numeric_code: bool = True,
    issued_by: User | None = None,
    mode: str = "BIOMETRIC",
) -> dict:
    """Mint a temporary workstation credential (one-time token + session code).

    The two parts have deliberately different lifetimes:

    * ``temporary_token`` is a ONE-TIME ``jone-temp-...`` ticket with a short
      ``expires_at`` TTL — the token path consumes it on first use;
    * ``access_code`` (the 6-digit numeric code) is the staff member's sign-in
      credential for the whole ATTENDANCE SESSION it is bound to. It stays valid
      for repeated application sign-ins until an official clock-out revokes it,
      so ``expires_at`` must never be read as the code's lifetime
      (``code_valid_until`` states that explicitly).

    The ONE implementation of credential creation: the biometric terminal path
    and the supervised manual-fallback path both mint through here, so expiry,
    supersede/revoke semantics, code hashing and auditing can never drift apart.
    ``issued_by`` is the account the audit trail names; the fallback path names
    the authorising supervisor, the biometric path names the staff member.
    """
    if not staff.is_active:
        raise PermissionDenied("Inactive staff accounts cannot receive workstation credentials.")
    profile = getattr(staff, "staff_profile", None)
    if profile and profile.employment_status in ("SUSPENDED", "INACTIVE", "ON_LEAVE"):
        raise PermissionDenied(
            f"Staff member is currently {profile.employment_status.lower()} and cannot receive workstation credentials."
        )

    raw_token = f"jone-temp-{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    numeric_code = _generate_numeric_access_code() if issue_numeric_code else ""
    numeric_hash = _hash_numeric_code(numeric_code) if numeric_code else ""
    dept_code = staff_department_code(staff)
    ttl = int(getattr(settings, "TEMPORARY_WORKSTATION_CREDENTIAL_TTL_MINUTES", DEFAULT_TEMP_CRED_TTL_MINUTES))
    now = timezone.now()
    expires_at = now + timedelta(minutes=ttl)

    if attendance_record is None:
        from ..models import AttendanceRecord

        attendance_record = (
            AttendanceRecord.objects.filter(staff=staff, clock_out_at__isnull=True)
            .order_by("-clock_in_at")
            .first()
        )
    if attendance_record is None:
        raise ValidationError({
            "attendance": "Clock in at the Receptionist Desktop before a workstation access code can be issued."
        })

    with transaction.atomic():
        # Invalidate old unused/active temporary credentials for this user
        TemporaryWorkstationCredential.objects.filter(
            staff=staff, session_active=True
        ).update(
            is_used=True,
            session_active=False,
            revoked_at=now,
            revoked_reason="SUPERSEDED",
        )
        cred = TemporaryWorkstationCredential.objects.create(
            staff=staff,
            workstation_reference=workstation_reference,
            credential_token_hash=token_hash,
            numeric_code_hash=numeric_hash,
            department=dept_code,
            attendance_record=attendance_record,
            expires_at=expires_at,
            session_active=True,
        )

    log_action(actor=issued_by or staff, action="TEMPORARY_WORKSTATION_CREDENTIAL_ISSUED", instance=cred,
               metadata={"staff_id": staff.pk, "workstation": workstation_reference, "department": dept_code,
                         "mode": mode})
    return {
        "credential_id": cred.pk,
        "temporary_token": raw_token,
        "access_code": numeric_code,
        # Token TTL. The numeric code is NOT bounded by this value; it is bound
        # to the open attendance record above and dies at clock-out.
        "expires_at": expires_at.isoformat(),
        "code_valid_until": "CLOCK_OUT",
        "department": dept_code,
        "workstation": workstation_reference,
        "attendance_reference": attendance_record.reference if attendance_record else None,
    }


def issue_temporary_workstation_credential(
    *,
    staff: User,
    workstation_reference: str,
    probe,
    attendance_record=None,
    issue_numeric_code: bool = True,
    return_details: bool = False,
):
    """Verify biometric fingerprint at workstation terminal and mint temporary sign-in credential.

    Identifying the staff member here IS their clock-in: when the caller has not
    supplied an attendance record, the open one is reused or opened — after the
    finger has matched, never before — so a refused probe can never leave
    attendance behind and a code can only ever exist against an open record
    (which is what makes clock-out expire it).

    Returns (raw_temp_token, expires_at_iso) by default, or a dict with `access_code` when `return_details=True`.
    """
    if not staff.is_active:
        raise PermissionDenied("Inactive staff accounts cannot receive workstation credentials.")
    profile = getattr(staff, "staff_profile", None)
    if profile and profile.employment_status in ("SUSPENDED", "INACTIVE", "ON_LEAVE"):
        raise PermissionDenied(f"Staff member is currently {profile.employment_status.lower()} and cannot receive workstation credentials.")

    try:
        probe_data = validate_minutiae_data(probe)
    except ValidationError as exc:
        raise ValidationError({"probe": "Invalid fingerprint minutiae probe."}) from exc

    active_template = FingerprintTemplate.objects.filter(staff=staff, status=FingerprintTemplate.Status.ACTIVE).first()
    if not active_template:
        raise ValidationError({"biometrics": "Staff member has no active fingerprint enrollment."})

    score = compute_minutiae_match_score(active_template.minutiae_data, probe_data)
    source_key = f"fp-ws-issue:{staff.pk}:{workstation_reference}:{secrets.token_hex(6)}"
    if score < match_score_threshold():
        FingerprintVerificationAttempt.objects.create(
            staff=staff,
            action="ISSUE_CODE",
            outcome=FingerprintOutcome.NO_MATCH,
            match_score=score,
            source_key=source_key,
            workstation_reference=workstation_reference,
        )
        log_action(actor=staff, action="BIOMETRIC_LOGIN_REFUSED", instance=None,
                   metadata={"staff_id": staff.pk, "score": score, "workstation": workstation_reference})
        raise PermissionDenied(f"Biometric fingerprint match failed (score {score:.1f}).")

    FingerprintVerificationAttempt.objects.create(
        staff=staff,
        action="ISSUE_CODE",
        outcome=FingerprintOutcome.VERIFIED,
        match_score=score,
        source_key=source_key,
        workstation_reference=workstation_reference,
    )

    # Item F: a matched finger at the workstation is the staff member's
    # clock-in. The record is opened here — after the match, never before — and
    # an existing open record is reused rather than duplicated. The minted code
    # stays bound to that record, so clock-out still expires it.
    attendance_created = None
    if attendance_record is None:
        from .staff_operations_service import ensure_open_attendance_record

        attendance_record, attendance_created = ensure_open_attendance_record(
            staff=staff, idempotency_key=f"ws-code:{source_key}"
        )

    details = mint_temporary_workstation_credential(
        staff=staff,
        workstation_reference=workstation_reference,
        attendance_record=attendance_record,
        issue_numeric_code=issue_numeric_code,
        issued_by=staff,
        mode="BIOMETRIC",
    )
    if attendance_created is not None:
        details["attendance_created"] = attendance_created
    details["match_score"] = score
    if return_details:
        return details
    return details["temporary_token"], details["expires_at"]


def record_failed_attempt(
    *,
    staff: User,
    workstation_reference: str = "",
    classification: str,
    note: str = "",
    actor: User | None = None,
) -> FingerprintVerificationAttempt:
    """Record that a fingerprint attempt was made and the capture itself failed.

    This is the ONLY way a hardware/capture failure enters the evidence trail:
    an approved workstation (desktop key) or a management session reports the
    classification from the fixed allowlist. Nothing here asserts that a
    fingerprint was captured or matched — the outcome says the opposite — and the
    report is audited, so it can never be used to fabricate a verification.
    """
    outcome = str(classification or "").strip().upper()
    if outcome not in FALLBACK_FAILURE_CLASSES:
        raise ValidationError({
            "failure_classification": "Report DEVICE_UNAVAILABLE, DEVICE_FAULT, CAPTURE_TIMEOUT, "
                                      "UNSUPPORTED_DEVICE or INTEGRATION_ERROR."
        })
    attempt = FingerprintVerificationAttempt.objects.create(
        staff=staff,
        action="ATTEMPT_FAILED",
        outcome=outcome,
        source_key=f"fp-attempt-failed:{staff.pk}:{secrets.token_hex(6)}",
        workstation_reference=workstation_reference or "",
    )
    log_action(
        actor=actor or staff,
        action="ATTENDANCE_FINGERPRINT_FAILED",
        instance=attempt,
        metadata={
            "staff_id": staff.pk,
            "outcome": outcome,
            "workstation": workstation_reference,
            "note": str(note or "")[:200],
        },
    )
    return attempt


# Terminal and fingerprint responses display ``EMP-{pk:04d}`` for staff
# without an operational profile (see e.g. the reception terminal's
# on-duty list). That code exists nowhere in the database — real
# employee codes are ``STF-...`` on the profile — so an operator typing
# the code shown on screen must still reach the same person.
_EMP_DISPLAY_CODE_RE = re.compile(r"^EMP-?0*(\d+)$", re.IGNORECASE)
_IDENTIFIER_DASH_TRANSLATION = str.maketrans({
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
})


def _identifier_staff_queryset():
    return User.objects.select_related("staff_profile", "staff_profile__department_ref")


def _normalise_identifier(value) -> str:
    """Canonicalise an operator-entered identifier without changing its meaning.

    Staff details are routinely copied from tables, documents and mobile
    browsers. Those copies can contain non-breaking spaces, zero-width
    formatting characters or typographic dashes even though the account
    email/employee code itself is plain ASCII. NFKC normalisation plus the
    removal of Unicode format characters makes those copies resolve to the
    same identifier; it never performs a fuzzy or partial match.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text.translate(_IDENTIFIER_DASH_TRANSLATION).strip()


def _positive_identifier_int(value) -> int | None:
    text = _normalise_identifier(value)
    if not text.isdigit():
        return None
    candidate = int(text)
    # Keep arbitrary operator input inside the signed BIGINT key range; a
    # longer all-digit string is not a plausible account/profile id and must
    # be a clean no-match rather than a database overflow error.
    return candidate if 0 < candidate <= 9_223_372_036_854_775_807 else None


def _resolve_identifier_value(value, *, primary: str) -> User | None:
    """Resolve ONE presented identifier value to a staff account.

    ``primary`` is the request field it arrived in ("email" or
    "employee_code") and only decides which interpretation is tried
    first. Every advertised interpretation is then tried, because the
    workforce/terminals pages bucket a free-text identifier by the
    presence of "@" — a numeric staff id typed there arrives as
    ``employee_code="12"`` and could never match a real (``STF-...``)
    employee code, even though ids are an advertised identifier.

    Email and employee-code comparisons also have a trimmed/lower-cased
    database fallback. Current serializers normalise new records, but
    legacy/imported rows can carry outer whitespace; without this fallback
    the exact visible email/code would be rejected for an invisible reason.

    An identifier is a NAME, never a credential: resolving it grants
    nothing by itself (biometric actions still require a matching
    fingerprint; a supervised fallback still requires a signed-in
    supervisor, a reason and, for managers, recorded failure evidence).
    """
    text = _normalise_identifier(value)
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None
    qs = _identifier_staff_queryset()

    def by_email():
        if "@" not in compact:
            return None
        found = qs.filter(email__iexact=compact).first()
        if found is not None:
            return found
        return (
            qs.annotate(_terminal_email=Lower(Trim(F("email"))))
            .filter(_terminal_email=compact.lower())
            .first()
        )

    def by_employee_code():
        if "@" in compact:
            return None
        found = qs.filter(staff_profile__employee_code__iexact=compact).first()
        if found is not None:
            return found
        return (
            qs.annotate(
                _terminal_employee_code=Lower(Trim(F("staff_profile__employee_code")))
            )
            .filter(_terminal_employee_code=compact.lower())
            .first()
        )

    def by_staff_id():
        candidate = None
        bare_numeric = compact.isdigit()
        if bare_numeric:
            candidate = _positive_identifier_int(compact)
        else:
            match = _EMP_DISPLAY_CODE_RE.match(compact)
            if match:
                candidate = _positive_identifier_int(match.group(1))
        if not candidate:
            return None
        found = qs.filter(pk=candidate).first()
        if found is not None or not bare_numeric:
            return found
        # ``staff_id`` in the API is, and remains, the User/account id. Only
        # when no such account exists may a bare number be the distinct
        # StaffProfile row id exposed by the workforce profiles API. The
        # explicit ``staff_profile_id`` field below is the unambiguous route
        # when both numbers happen to exist for different people.
        return qs.filter(staff_profile__pk=candidate).first()

    attempts = (
        (by_employee_code, by_staff_id, by_email)
        if primary == "employee_code"
        else (by_email, by_employee_code, by_staff_id)
    )
    for attempt in attempts:
        resolved = attempt()
        if resolved is not None:
            return resolved
    return None


def presented_staff_identifier(
    *,
    staff_id=None,
    staff_profile_id=None,
    identifier: str = "",
    email: str = "",
    employee_code: str = "",
) -> str:
    """The identifier an operator actually presented (for error messages)."""
    for value in (identifier, email, employee_code, staff_id, staff_profile_id):
        text = _normalise_identifier(value)
        if text:
            return text
    return ""


def lookup_staff_by_identifier(
    *,
    staff: User | None = None,
    staff_id=None,
    staff_profile_id=None,
    identifier: str = "",
    email: str = "",
    employee_code: str = "",
) -> User | None:
    """Resolve a staff account from an explicit terminal identifier.

    The shared lookup for every terminal action: one implementation, so the
    biometric path and the supervised fallback path can never disagree about
    which account an identifier names. Accepts the account email, the
    ``STF-...`` employee code, the numeric account/staff id, an explicit
    StaffProfile id, and the displayed ``EMP-{id}`` code — in whichever
    request field the client sent them.
    """
    if staff is not None:
        return staff

    profile_id = _positive_identifier_int(staff_profile_id)
    if profile_id:
        resolved = _identifier_staff_queryset().filter(staff_profile__pk=profile_id).first()
        if resolved is not None:
            return resolved

    account_id = _positive_identifier_int(staff_id)
    if account_id:
        resolved = _identifier_staff_queryset().filter(pk=account_id).first()
        if resolved is not None:
            return resolved

    return (
        _resolve_identifier_value(identifier, primary="identifier")
        or _resolve_identifier_value(email, primary="email")
        or _resolve_identifier_value(employee_code, primary="employee_code")
    )


def _manual_fallback_terminal_action(
    *,
    actor: User | None,
    workstation_reference: str,
    reason: str,
    staff: User | None = None,
    staff_id=None,
    staff_profile_id=None,
    identifier: str = "",
    email: str = "",
    employee_code: str = "",
) -> dict:
    """Record the attendance of a supervised fallback and mint its 6-digit code.

    Order matters: the supervisor's authority, the reason and the recorded
    failure evidence are all verified BEFORE anything is written, so a refused
    fallback can never leave an attendance record behind. Clock-in is attributed
    to the staff member (their attendance record), while the authorising
    supervisor is named in the audit trail and in the credential metadata.
    """
    from .attendance_fallback_service import issue_manual_fallback_code

    resolved_staff = lookup_staff_by_identifier(
        staff=staff,
        staff_id=staff_id,
        staff_profile_id=staff_profile_id,
        identifier=identifier,
        email=email,
        employee_code=employee_code,
    )
    if resolved_staff is None:
        presented = presented_staff_identifier(
            staff_id=staff_id,
            staff_profile_id=staff_profile_id,
            identifier=identifier,
            email=email,
            employee_code=employee_code,
        )
        if not presented:
            raise ValidationError(
                {"staff": "Identify the staff member (email, employee code or id) for a supervised fallback."}
            )
        # The operator here is a signed-in supervisor who can already see
        # the staff directory, so naming what did not match (their own
        # input) is safe and turns a dead end into a correctable typo.
        raise ValidationError(
            {"staff": (
                f"No staff account matches '{presented[:80]}'. Select the staff member from "
                "the staff search, or use their account email, STF employee code, or numeric staff id."
            )}
        )
    return issue_manual_fallback_code(
        staff=resolved_staff,
        actor=actor,
        reason=reason,
        workstation_reference=workstation_reference,
        clock_in=True,
    )


def reception_attendance_workstation_action(
    *,
    action: str,
    probe,
    workstation_reference: str,
    staff: User | None = None,
    email: str = "",
    employee_code: str = "",
    staff_id: int | None = None,
    staff_profile_id: int | None = None,
    identifier: str = "",
    actor: User | None = None,
    manual_override_reason: str = "",
    classification: str = "",
    note: str = "",
) -> dict:
    """Execute Reception Attendance Workstation verification, clock-in/out, break, and numeric code issuance."""
    from ..models import AttendanceRecord
    from .staff_operations_service import clock_attendance

    action_norm = (action or "CLOCK_IN").strip().upper()
    action_aliases = {
        "BREAK_START": "BREAK_STARTED",
        "BREAK_END": "BREAK_ENDED",
    }
    action_norm = action_aliases.get(action_norm, action_norm)
    valid_actions = {
        "VERIFY_STATUS", "CLOCK_IN", "CLOCK_OUT", "BREAK_STARTED", "BREAK_ENDED", "ISSUE_CODE",
        "MANUAL_FALLBACK", "RECORD_FAILURE",
    }
    if action_norm not in valid_actions:
        raise ValidationError({
            "action": "Action must be VERIFY_STATUS, CLOCK_IN, BREAK_START, BREAK_END, CLOCK_OUT, "
                      "ISSUE_CODE, MANUAL_FALLBACK, or RECORD_FAILURE."
        })

    # A supervised fallback never carries a probe: it is the recorded failure of
    # the probe path. It is handled before probe validation for that reason.
    if action_norm == "RECORD_FAILURE":
        failed_staff = lookup_staff_by_identifier(
            staff=staff,
            staff_id=staff_id,
            staff_profile_id=staff_profile_id,
            identifier=identifier,
            email=email,
            employee_code=employee_code,
        )
        if failed_staff is None:
            presented = presented_staff_identifier(
                staff_id=staff_id,
                staff_profile_id=staff_profile_id,
                identifier=identifier,
                email=email,
                employee_code=employee_code,
            )
            if not presented:
                raise ValidationError(
                    {"staff": "Identify the staff member (email, employee code or id) whose capture failed."}
                )
            raise ValidationError(
                {"staff": (
                    f"No staff account matches '{presented[:80]}'. Select the staff member from "
                    "the staff search, or use their account email, STF employee code, or numeric staff id."
                )}
            )
        attempt = record_failed_attempt(
            staff=failed_staff,
            workstation_reference=workstation_reference,
            classification=classification,
            note=note,
            actor=actor,
        )
        return {
            "action": "RECORD_FAILURE",
            "status": "ATTEMPT_FAILED",
            "outcome": attempt.outcome,
            "staff_id": failed_staff.pk,
            "staff_email": failed_staff.email,
            "staff_name": failed_staff.full_name,
            "employee_code": getattr(getattr(failed_staff, "staff_profile", None), "employee_code", ""),
            "workstation": workstation_reference,
        }

    if action_norm == "MANUAL_FALLBACK":
        return _manual_fallback_terminal_action(
            actor=actor,
            workstation_reference=workstation_reference,
            reason=manual_override_reason,
            staff=staff,
            staff_id=staff_id,
            staff_profile_id=staff_profile_id,
            identifier=identifier,
            email=email,
            employee_code=employee_code,
        )

    probe_data = validate_minutiae_data(probe)

    # Resolve staff by explicit identifier or 1:N biometric template matching
    resolved_staff = lookup_staff_by_identifier(
        staff=staff,
        staff_id=staff_id,
        staff_profile_id=staff_profile_id,
        identifier=identifier,
        email=email,
        employee_code=employee_code,
    )
    if resolved_staff is None:
        best_score = 0.0
        best_template = None
        for tmpl in FingerprintTemplate.objects.select_related(
            "staff", "staff__staff_profile", "staff__staff_profile__department_ref"
        ).filter(status=FingerprintTemplate.Status.ACTIVE, staff__is_active=True):
            score = compute_minutiae_match_score(tmpl.minutiae_data, probe_data)
            if score >= match_score_threshold() and score > best_score:
                best_score = score
                best_template = tmpl
        if best_template is not None:
            resolved_staff = best_template.staff

    if resolved_staff is None:
        raise ValidationError({"staff": "Could not identify an active enrolled staff member from the supplied credentials/fingerprint."})

    if not resolved_staff.is_active:
        raise PermissionDenied("This staff account is inactive and cannot clock in or receive workstation access codes.")

    profile = getattr(resolved_staff, "staff_profile", None)
    if profile and profile.employment_status in ("SUSPENDED", "INACTIVE", "ON_LEAVE"):
        raise PermissionDenied(
            f"Staff member is currently {profile.employment_status.lower().replace('_', ' ')} and is not eligible for attendance or workstation codes."
        )

    source_key = f"fp-terminal:{resolved_staff.pk}:{action_norm}:{secrets.token_hex(6)}"
    outcome, passed = verify_fingerprint_for_clock(
        staff=resolved_staff,
        actor=resolved_staff,
        action=action_norm,
        source_key=source_key,
        probe=probe_data,
    )
    if not passed:
        if outcome == FingerprintOutcome.NOT_ENROLLED:
            has_pending = FingerprintTemplate.objects.filter(
                staff=resolved_staff, status=FingerprintTemplate.Status.PENDING
            ).exists()
            if has_pending:
                raise PermissionDenied("Fingerprint enrollment is pending supervisor approval.")
            raise PermissionDenied("Staff member does not have an active approved fingerprint enrollment.")
        raise PermissionDenied(f"Biometric fingerprint verification failed ({outcome}).")

    attempt = FingerprintVerificationAttempt.objects.filter(source_key=source_key).first()
    if attempt and workstation_reference and not attempt.workstation_reference:
        attempt.workstation_reference = workstation_reference
        attempt.save(update_fields=["workstation_reference"])
    match_score = getattr(attempt, "match_score", None)

    dept_code = staff_department_code(resolved_staff)
    emp_code = profile.employee_code if profile else f"EMP-{resolved_staff.pk:04d}"

    if action_norm == "VERIFY_STATUS":
        bio_status = get_staff_biometric_status(resolved_staff)
        return {
            **bio_status,
            "action": "VERIFY_STATUS",
            "outcome": outcome,
            "match_score": match_score,
            "staff_id": resolved_staff.pk,
            "staff_email": resolved_staff.email,
            "staff_name": resolved_staff.full_name,
            "employee_code": emp_code,
            "role": resolved_staff.role,
            "department": dept_code,
            "workstation": workstation_reference,
        }

    if action_norm == "CLOCK_IN":
        # Item F: this action IS the clock-in. The shared helper reuses an open
        # record and only opens one when the staff member has none, inside one
        # locked transaction, so a repeated terminal clock-in can never create a
        # second record or a duplicate event.
        from .staff_operations_service import ensure_open_attendance_record

        open_att, created = ensure_open_attendance_record(
            staff=resolved_staff, idempotency_key=source_key
        )

        cred_details = issue_temporary_workstation_credential(
            staff=resolved_staff,
            workstation_reference=workstation_reference,
            probe=probe_data,
            attendance_record=open_att,
            issue_numeric_code=True,
            return_details=True,
        )
        return {
            "action": "CLOCK_IN",
            "status": "CLOCKED_IN",
            "created": created,
            "outcome": outcome,
            "match_score": match_score,
            "staff_id": resolved_staff.pk,
            "staff_email": resolved_staff.email,
            "staff_name": resolved_staff.full_name,
            "employee_code": emp_code,
            "role": resolved_staff.role,
            "department": dept_code,
            "workstation": workstation_reference,
            "attendance_reference": open_att.reference,
            "attendance_status": open_att.status,
            "clock_in_at": open_att.clock_in_at.isoformat() if open_att.clock_in_at else None,
            "temporary_token": cred_details["temporary_token"],
            "access_code": cred_details["access_code"],
            "expires_at": cred_details["expires_at"],
            "code_valid_until": cred_details["code_valid_until"],
        }

    if action_norm == "ISSUE_CODE":
        # Item F: issuing the code identifies the staff member, so it IS their
        # clock-in — with no open record the code opens one (after the probe
        # above matched, never before) and an existing open record is reused,
        # never duplicated. The code stays bound to that record. A staff member
        # on an active break is refused instead: the code would sign them in to
        # a workstation mid-break, and that policy is unchanged.
        from .staff_operations_service import ensure_open_attendance_record

        open_att, created = ensure_open_attendance_record(
            staff=resolved_staff, idempotency_key=source_key
        )
        if open_att.status != AttendanceRecord.Status.ON_DUTY:
            raise ValidationError({"attendance": "Staff member must be on active duty (not on break) to issue a workstation access code."})
        cred_details = issue_temporary_workstation_credential(
            staff=resolved_staff,
            workstation_reference=workstation_reference,
            probe=probe_data,
            attendance_record=open_att,
            issue_numeric_code=True,
            return_details=True,
        )
        return {
            "action": "ISSUE_CODE",
            "status": "CODE_ISSUED",
            "created": created,
            "outcome": outcome,
            "match_score": match_score,
            "staff_id": resolved_staff.pk,
            "staff_email": resolved_staff.email,
            "staff_name": resolved_staff.full_name,
            "employee_code": emp_code,
            "role": resolved_staff.role,
            "department": dept_code,
            "workstation": workstation_reference,
            "attendance_reference": open_att.reference,
            "attendance_status": open_att.status,
            "temporary_token": cred_details["temporary_token"],
            "access_code": cred_details["access_code"],
            "expires_at": cred_details["expires_at"],
            "code_valid_until": cred_details["code_valid_until"],
        }

    if action_norm in ("BREAK_STARTED", "BREAK_ENDED"):
        record, _ = clock_attendance(
            staff=resolved_staff,
            action=action_norm,
            actor=resolved_staff,
            idempotency_key=source_key,
        )
        return {
            "action": action_norm,
            "status": record.status,
            "outcome": outcome,
            "match_score": match_score,
            "staff_id": resolved_staff.pk,
            "staff_email": resolved_staff.email,
            "staff_name": resolved_staff.full_name,
            "employee_code": emp_code,
            "role": resolved_staff.role,
            "department": dept_code,
            "workstation": workstation_reference,
            "attendance_reference": record.reference,
            "attendance_status": record.status,
        }

    # CLOCK_OUT
    with transaction.atomic():
        open_att = (
            AttendanceRecord.objects.select_for_update()
            .filter(staff=resolved_staff, clock_out_at__isnull=True)
            .order_by("-clock_in_at")
            .first()
        )
        if open_att is None:
            raise ValidationError({"attendance": "Staff member does not have an active clock-in session to clock out."})
        if open_att.status == AttendanceRecord.Status.ON_BREAK:
            clock_attendance(
                staff=resolved_staff,
                action="BREAK_ENDED",
                actor=resolved_staff,
                idempotency_key=f"{source_key}:break-end",
            )
        revoked_count = revoke_workstation_credentials_on_clock_out(
            staff=resolved_staff, reason="CLOCKED_OUT"
        )
        record, _ = clock_attendance(
            staff=resolved_staff,
            action="CLOCK_OUT",
            actor=resolved_staff,
            idempotency_key=source_key,
        )

    return {
        "action": "CLOCK_OUT",
        "status": "CLOCKED_OUT",
        "outcome": outcome,
        "match_score": match_score,
        "staff_id": resolved_staff.pk,
        "staff_email": resolved_staff.email,
        "staff_name": resolved_staff.full_name,
        "employee_code": emp_code,
        "role": resolved_staff.role,
        "department": dept_code,
        "workstation": workstation_reference,
        "attendance_reference": record.reference,
        "attendance_status": record.status,
        "clock_out_at": record.clock_out_at.isoformat() if record.clock_out_at else None,
        "worked_minutes": record.worked_minutes,
        "revoked_credentials_count": revoked_count,
    }


def consume_temporary_workstation_credential(raw_token: str) -> User | None:
    """Consume a temporary workstation sign-in token."""
    cred = consume_temporary_workstation_credential_record(raw_token=raw_token)
    return cred.staff if cred else None


def consume_temporary_workstation_credential_record(*, raw_token: str) -> TemporaryWorkstationCredential | None:
    """Consume a single-use `jone-temp-...` token and return its credential record."""
    if not raw_token or not raw_token.startswith("jone-temp-"):
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        cred = (
            TemporaryWorkstationCredential.objects.select_for_update()
            .select_related("staff", "attendance_record")
            .filter(
                credential_token_hash=token_hash,
                is_used=False,
                session_active=True,
                revoked_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if not cred:
            return None
        if cred.attendance_record and cred.attendance_record.clock_out_at is not None:
            cred.session_active = False
            cred.revoked_at = now
            cred.revoked_reason = "CLOCKED_OUT"
            cred.save(update_fields=["session_active", "revoked_at", "revoked_reason", "updated_at"])
            return None
        cred.is_used = True
        cred.used_at = now
        cred.save(update_fields=["is_used", "used_at", "updated_at"])
        return cred


def authenticate_numeric_workstation_code(
    *,
    access_code: str,
    email: str = "",
    employee_code: str = "",
    department: str = "",
    workstation_reference: str = "",
) -> TemporaryWorkstationCredential:
    """Validate a 6-digit code against its staff member's ACTIVE attendance session.

    The numeric code is the staff member's sign-in credential for the length of
    one attendance session — it is not a one-time ticket that an application
    sign-out consumes, and it is not tied to the application JWT's lifetime.
    Application authentication (sign in / sign out / token expiry) and
    attendance (clock in / clock out) are separate concerns:

    * signing out blacklists the refresh token and nothing else;
    * the code keeps working for as many sign-ins as the staff member needs
      while their attendance record is open (browser refresh, a new device, a
      token that expired overnight);
    * an official clock-out (or an administrative revocation — suspension,
      deactivation, biometric revocation, supersede-on-reissue) ends it, and
      the code row stays in place as historical evidence.

    A credential with NO attendance binding (a row written before every code was
    bound at mint time) keeps the original short TTL and single-use rules, so
    this lifecycle change can never extend the life of an old, unbound code.
    """
    from ..models import AttendanceRecord

    code_str = str(access_code or "").strip()
    if not code_str.isdigit() or len(code_str) != 6:
        raise PermissionDenied("Workstation access code must be a 6-digit numeric code.")

    code_hash = _hash_numeric_code(code_str)
    now = timezone.now()

    with transaction.atomic():
        candidates = (
            TemporaryWorkstationCredential.objects.select_for_update()
            .select_related("staff", "staff__staff_profile", "attendance_record")
            .filter(numeric_code_hash=code_hash)
        )
        # A revoked/superseded row may legitimately share a hash with a newer
        # live one (code generation only guarantees uniqueness among ACTIVE
        # codes). Always prefer the live credential; fall back to the newest row
        # so the refusal names the real state instead of a stale duplicate.
        cred = (
            candidates.filter(session_active=True, revoked_at__isnull=True)
            .order_by("-created_at")
            .first()
            or candidates.order_by("-created_at").first()
        )
        if not cred:
            raise PermissionDenied("Invalid workstation access code.")

        staff = cred.staff
        if not staff or not staff.is_active:
            raise PermissionDenied("Staff account is inactive.")

        profile = getattr(staff, "staff_profile", None)
        if profile and profile.employment_status in ("SUSPENDED", "INACTIVE", "ON_LEAVE"):
            raise PermissionDenied(f"Staff member is currently {profile.employment_status.lower()}.")

        # Refusals that MUST be audited are collected here and raised after this
        # atomic block: an audit row written inside it would be rolled back by
        # the very refusal it records, so security-relevant events (code sharing,
        # cross-department attempts) would never reach the audit trail.
        refusal = None

        # Prevent code sharing: if caller specified email or employee_code, it MUST belong to this employee
        if (email or "").strip() and staff.email.lower() != email.strip().lower():
            refusal = (
                "Workstation access code belongs to a different employee.",
                {"presented_email": email.strip().lower(), "owner_id": staff.pk},
            )
        elif (employee_code or "").strip() and (profile.employee_code if profile else "").strip().lower() != employee_code.strip().lower():
            refusal = (
                "Workstation access code belongs to a different employee.",
                {"presented_employee_code": employee_code.strip(), "owner_id": staff.pk},
            )

        # An administratively revoked credential is dead immediately, whatever
        # the attendance record says (clock-out, suspension, supersede, ...).
        if refusal is None and (not cred.session_active or cred.revoked_at is not None):
            raise PermissionDenied("Workstation access code has expired upon clock-out or been revoked.")

        open_att = (
            AttendanceRecord.objects.filter(staff=staff, clock_out_at__isnull=True)
            .order_by("-clock_in_at")
            .first()
        )
        bound_record = cred.attendance_record
        revoke_reason = ""
        if open_att is None:
            # No attendance session is open: a code can never outlive its
            # session, even if a revocation step was missed.
            revoke_reason = "CLOCKED_OUT"
        elif bound_record is not None and (
            bound_record.clock_out_at is not None or bound_record.pk != open_att.pk
        ):
            # This code belongs to a completed/older session, not to the one
            # currently open (or none at all).
            revoke_reason = "CLOCKED_OUT"

        if not revoke_reason and bound_record is None:
            # Legacy unbound credential: the original ONE-TIME ticket rules
            # still apply, because there is no session to justify a longer life.
            if cred.expires_at and cred.expires_at <= now:
                raise PermissionDenied("Workstation access code has expired. Ask the front desk for a new code.")
            if cred.is_used:
                raise PermissionDenied(
                    "Workstation access code has already been used. Ask the front desk to re-issue one."
                )

        if not revoke_reason:
            if (workstation_reference or "").strip() and cred.workstation_reference != workstation_reference.strip():
                raise PermissionDenied("Workstation access code is not valid for this workstation.")

            if refusal is None and (department or "").strip() and not is_staff_authorized_for_department(staff, department):
                refusal = (
                    "Workstation access code is not authorized for this department dashboard.",
                    {"target_department": department, "staff_department": cred.department},
                )

            if refusal is None and not cred.is_used:
                # First use is still recorded for the audit trail; unlike a
                # ticket, it does not consume the credential. Repeat sign-ins
                # are the documented behaviour for the rest of the session.
                cred.is_used = True
                cred.used_at = now
                cred.save(update_fields=["is_used", "used_at", "updated_at"])

    if revoke_reason:
        # The revocation is written OUTSIDE the atomic block: raising inside it
        # would roll back the very revocation the refusal records.
        TemporaryWorkstationCredential.objects.filter(pk=cred.pk).update(
            session_active=False,
            revoked_at=now,
            revoked_reason=revoke_reason,
            expires_at=now,
        )
        raise PermissionDenied("Workstation access code expired because the employee has clocked out.")

    if refusal is not None:
        message, metadata = refusal
        action = (
            "CROSS_DEPARTMENT_CODE_ACCESS_BLOCKED"
            if "department dashboard" in message
            else "WORKSTATION_CODE_SHARING_BLOCKED"
        )
        log_action(actor=cred.staff, action=action, instance=cred, metadata=metadata)
        raise PermissionDenied(message)
    return cred
