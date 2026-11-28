"""Server side of the J-ONE Reader Agent contract.

SECURITY MODEL (read this before changing anything here)
========================================================
The reader agent is the trusted biometric endpoint on the reception PC. It
proves who it is with an HMAC-SHA256 signature over a canonical request, using
a key that only the API and that one provisioned agent hold. An unsigned or
wrongly signed call, a stale call, a repeated nonce, an expired or already used
session, or a candidate that does not correspond to an ACTIVE enrolled
template is refused.

The API still makes the accept/reject decision: the agent reports *measurements*
(vendor dissimilarity score, which staff template it matched) and the API
applies the threshold, the staff-eligibility rules, the workstation rules and
the existing attendance state machine. A browser-supplied "success": true is
never trusted -- browsers have no key, and the only identity accepted here comes
from a signed agent assertion tied to a single-use session.

Attendance is recorded through the EXISTING ``clock_attendance`` state machine
(``services/staff_operations_service.py``), so duplicate clock-ins, capability
checks, access-code revocation and history behave exactly as they already do.
No second attendance system is created here.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any, Iterable

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action

from ..reader_agent_models import (
    ReaderAgent,
    ReaderAgentEvent,
    ReaderAgentSession,
    ReaderAgentTemplate,
)
from .staff_operations_service import clock_attendance

# --------------------------------------------------------------------------
# Tunables (all overridable from settings, none hardcoded into the frontend)
# --------------------------------------------------------------------------
DEFAULT_SESSION_TTL_SECONDS = 120
DEFAULT_SIGNATURE_WINDOW_SECONDS = 300
DEFAULT_CAPTURE_SKEW_SECONDS = 180
# dpfj dissimilarity scores run from 0 (identical) to DPFJ_PROBABILITY_ONE
# (0x7FFFFFFF, "no match"). 2_147_483 is one thousandth of that range, the
# value HID's Developer Guide gives for roughly 0.1% false-accept in the
# verification tables. Calibrate on site with the acceptance test in
# tests/HARDWARE_ACCEPTANCE.md before going live; the API is the decider.
DEFAULT_MATCH_THRESHOLD = 2_147_483
ATTENDANCE_ACTIONS = {
    ReaderAgentSession.Action.CLOCK_IN,
    ReaderAgentSession.Action.CLOCK_OUT,
    ReaderAgentSession.Action.BREAK_START,
    ReaderAgentSession.Action.BREAK_END,
}
#: Actions that end in a numeric workstation code rather than a status change.
#: The finger still has to be verified; only what the server does afterwards
#: differs, and that afterwards is the existing credential/attendance code.
CODE_ACTIONS = {ReaderAgentSession.Action.ISSUE_CODE}


class AgentAuthError(Exception):
    """Agent request refused before any business logic runs."""

    def __init__(self, code: str, message: str, http_status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def session_ttl_seconds() -> int:
    return int(getattr(settings, "READER_AGENT_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS))


def signature_window_seconds() -> int:
    return int(getattr(settings, "READER_AGENT_SIGNATURE_WINDOW_SECONDS", DEFAULT_SIGNATURE_WINDOW_SECONDS))


def capture_skew_seconds() -> int:
    return int(getattr(settings, "READER_AGENT_CAPTURE_SKEW_SECONDS", DEFAULT_CAPTURE_SKEW_SECONDS))


def match_threshold() -> int:
    return int(getattr(settings, "READER_AGENT_MATCH_THRESHOLD", DEFAULT_MATCH_THRESHOLD))


# --------------------------------------------------------------------------
# Signed request verification
# --------------------------------------------------------------------------
def canonical_message(*, reference: str, nonce: str, timestamp: str, kind: str, body: bytes) -> bytes:
    """The exact byte string both sides sign.

    A fixed field order (never a dict iteration order), and the body enters as
    a SHA-256 digest so large payloads never need re-serialising on either
    side. Documented identically in workstation_agent/jone_reader_agent.py.
    """
    body_digest = hashlib.sha256(body or b"").hexdigest()
    return "\n".join(["jone-reader-agent-v1", reference, nonce, timestamp, kind, body_digest]).encode("utf-8")


def sign_request(signing_key_hex: str, message: bytes) -> str:
    return hmac.new(bytes.fromhex(signing_key_hex), message, hashlib.sha256).hexdigest()


def verify_agent_request(
    *,
    reference: str,
    nonce: str,
    timestamp: str,
    kind: str,
    body: bytes,
    signature: str,
) -> ReaderAgent:
    """Authenticate one agent call. Raises AgentAuthError on any doubt."""
    reference = str(reference or "").strip()
    nonce = str(nonce or "").strip()
    timestamp = str(timestamp or "").strip()
    kind = str(kind or "").strip().upper()
    signature = str(signature or "").strip().lower()

    if not (reference and nonce and timestamp and kind and signature):
        raise AgentAuthError("AGENT_SIGNATURE_MISSING", "The reader agent request is not signed.")
    if len(nonce) > 80 or len(nonce) < 8:
        raise AgentAuthError("AGENT_NONCE_INVALID", "The reader agent nonce is malformed.", 400)

    agent = ReaderAgent.objects.select_related("workstation").filter(reference=reference).first()
    if agent is None or not agent.is_active:
        raise AgentAuthError("AGENT_UNKNOWN", "This reader agent is not provisioned or has been revoked.")

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        raise AgentAuthError("AGENT_TIMESTAMP_INVALID", "The reader agent timestamp is malformed.", 400)
    drift = abs(int(timezone.now().timestamp()) - sent_at)
    if drift > signature_window_seconds():
        raise AgentAuthError(
            "AGENT_TIMESTAMP_STALE",
            "The reader agent clock is more than %d seconds away from the server; check the PC clock and try again."
            % signature_window_seconds(),
        )

    expected = sign_request(agent.signing_key, canonical_message(
        reference=reference, nonce=nonce, timestamp=timestamp, kind=kind, body=body,
    ))
    if not hmac.compare_digest(expected, signature):
        raise AgentAuthError("AGENT_SIGNATURE_INVALID", "The reader agent signature is not valid.")

    # Replay: the nonce is consumed before the caller does any work.
    try:
        ReaderAgentEvent.objects.create(
            agent=agent, kind=_event_kind(kind), nonce=nonce, outcome="RECEIVED",
        )
    except IntegrityError:
        raise AgentAuthError("AGENT_NONCE_REPLAY", "This reader agent request was already processed.")
    return agent


def _event_kind(kind: str) -> str:
    kind = str(kind or "").upper()
    if kind in ReaderAgentEvent.Kind.values:
        return kind
    return ReaderAgentEvent.Kind.ERROR


def _close_event(*, agent: ReaderAgent, nonce: str, outcome: str, **fields) -> None:
    """Fill in the outcome of an already-created (nonce-locked) event."""
    detail = fields.pop("detail", None)
    update = {"outcome": outcome, **fields}
    if detail is not None:
        update["detail"] = detail
    # Events are append-only for staff-facing history, but the outcome of the
    # RECEIVED row created during authentication is the same event; update it
    # with a queryset so the model's append-only guard stays intact.
    ReaderAgentEvent.objects.filter(agent=agent, nonce=nonce).update(**update)


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------
PROVISION_CAPABILITY = "attendance.manage"


def _capability_codes(actor: User):
    from apps.accounts.capabilities import capability_codes_for_user

    return capability_codes_for_user(actor)


def may_provision(actor: User) -> bool:
    """Whether this account may provision (or re-key) a reader agent.

    One definition, used by the endpoint, the refusal message and the read-only
    setup-status diagnosis, so the screen and the server can never disagree.
    """
    return PROVISION_CAPABILITY in _capability_codes(actor)


def provisioning_status(actor: User) -> dict[str, Any]:
    """Why can (or can't) THIS account provision the reader on its computer?

    The dashboard used to answer that question with a single sentence, which left
    the operator guessing whether the account was wrong, the role mapping was
    wrong, or the workstation was. Every input to the decision is reported here
    so the refusal can name the exact missing link. Nothing in this payload
    grants authority: ``can_provision`` is computed with the same
    ``has_capability`` call the provisioning endpoint enforces.
    """
    from apps.accounts.capabilities import capability_codes_for_user
    from apps.accounts.departments import department_context_is_valid

    codes = capability_codes_for_user(actor)
    role = str(getattr(actor, "role", "") or "")
    department = ""
    profile = getattr(actor, "staff_profile", None)
    if profile is not None:
        department = str(getattr(profile, "department", "") or "")
    context_ok = department_context_is_valid(actor) if actor is not None else False
    can = PROVISION_CAPABILITY in codes
    missing = "" if can else PROVISION_CAPABILITY
    if can:
        reason = ""
    elif not context_ok:
        reason = (
            "This account has no valid staff-profile department for its role, so the server "
            "resolves no capabilities for it. An administrator must set the account's department "
            "on its staff profile."
        )
    elif not codes:
        reason = (
            "This account's role has no capabilities recorded in the database, so it cannot "
            "manage attendance. An administrator must review the role's capability grants."
        )
    else:
        reason = (
            "This account does not hold the attendance management capability, which is what "
            "provisioning a reader requires."
        )
    return {
        "can_provision": can,
        "missing_capability": missing,
        "reason": reason,
        "role": role,
        "department": department,
        "department_context_valid": context_ok,
        "capability_count": len(codes),
        "holds": sorted(codes),
        "capability_label": "Manage attendance",
        "grant_hint": (
            "Sign in on this computer with an administrator, general manager or attendance "
            "manager account and press Set up again; or ask an administrator to grant this "
            "account the “Manage attendance” permission."
        ) if not can else "",
    }


def provision_agent(
    *, actor: User, workstation, label: str = "", device_serial: str = "", allow_rotate: bool = False
) -> tuple[ReaderAgent, str | None]:
    """Create this workstation's agent, or reuse the one that is already valid.

    Returns ``(agent, raw_key)``. ``raw_key`` is None when a healthy ACTIVE agent
    already existed and no re-key was requested: re-keying on every press would
    silently break the agent that is already running on the reception PC, and the
    browser that presses the button cannot know whether the PC in front of it is
    the one holding the key. The operator re-keys deliberately (``allow_rotate``,
    the "Set up this computer again" control) when a machine is replaced or the
    key is lost.
    """
    if PROVISION_CAPABILITY not in _capability_codes(actor):
        status = provisioning_status(actor)
        raise PermissionDenied(
            "Provisioning a fingerprint reader requires the “Manage attendance” permission "
            f"({PROVISION_CAPABILITY}), which this account does not hold. {status['reason']} "
            f"{status['grant_hint']}".strip()
        )
    if workstation is None:
        raise ValidationError({"workstation": "Choose the workstation this reader is attached to."})
    label = str(label or "").strip() or f"{workstation.name} reader"
    with transaction.atomic():
        agent = ReaderAgent.objects.select_for_update().filter(workstation=workstation).first()
        if agent is None:
            agent = ReaderAgent.objects.create(
                label=label[:120], workstation=workstation,
                device_serial=str(device_serial or "").strip()[:80],
                model_name="DigitalPersona U.are.U 4500/4000B",
                signing_key=secrets.token_hex(32),
                provisioned_by=actor,
            )
            raw_key = agent.signing_key
        elif agent.status == ReaderAgent.Status.ACTIVE and not allow_rotate:
            # Reuse: update the descriptive fields only. The signing key stays.
            agent.label = label[:120]
            if device_serial:
                agent.device_serial = str(device_serial).strip()[:80]
            agent.save(update_fields=["label", "device_serial", "updated_at"])
            raw_key = None
        else:
            agent.label = label[:120]
            if device_serial:
                agent.device_serial = str(device_serial).strip()[:80]
            agent.status = ReaderAgent.Status.ACTIVE
            agent.provisioned_by = actor
            agent.rotate_signing_key()
            agent.save(update_fields=["label", "device_serial", "status", "provisioned_by", "updated_at"])
            raw_key = agent.signing_key
    log_action(actor=actor, action="READER_AGENT_PROVISIONED", instance=agent,
               metadata={"workstation": getattr(workstation, "reference", ""), "reference": agent.reference,
                         "rekeyed": raw_key is not None})
    return agent, raw_key


def rotate_agent_key(*, actor: User, agent: ReaderAgent) -> str:
    if PROVISION_CAPABILITY not in _capability_codes(actor):
        raise PermissionDenied(
            "Rotating a reader agent key requires the “Manage attendance” permission "
            f"({PROVISION_CAPABILITY})."
        )
    key = agent.rotate_signing_key()
    log_action(actor=actor, action="READER_AGENT_KEY_ROTATED", instance=agent, metadata={})
    return key


def revoke_agent(*, actor: User, agent: ReaderAgent, reason: str = "") -> ReaderAgent:
    if PROVISION_CAPABILITY not in _capability_codes(actor):
        raise PermissionDenied(
            "Revoking a reader agent requires the “Manage attendance” permission "
            f"({PROVISION_CAPABILITY})."
        )
    with transaction.atomic():
        locked = ReaderAgent.objects.select_for_update().get(pk=agent.pk)
        locked.status = ReaderAgent.Status.REVOKED
        if reason:
            locked.last_error = str(reason)[:255]
        locked.save(update_fields=["status", "last_error", "updated_at"])
    log_action(
        actor=actor,
        action="READER_AGENT_REVOKED",
        instance=locked,
        metadata={"reference": locked.reference, "reason": str(reason or "")[:200]},
    )
    return locked


# --------------------------------------------------------------------------
# Sessions (browser side, JWT authenticated)
# --------------------------------------------------------------------------
def create_session(
    *,
    actor: User,
    action: str,
    staff: User | None = None,
    workstation_reference: str = "",
    finger_position: str = "RIGHT_INDEX",
    agent_reference: str = "",
) -> tuple[ReaderAgentSession, str]:
    """Mint a single-use challenge. Returns (session, raw_token)."""
    action = str(action or "").upper()
    if action not in ReaderAgentSession.Action.values:
        raise ValidationError({"action": "Unknown fingerprint action."})

    if action == ReaderAgentSession.Action.ENROLL and staff is None:
        raise ValidationError({"staff_id": "Choose the staff member whose fingerprint is being enrolled."})
    if action == ReaderAgentSession.Action.ENROLL and not (
        has_capability(actor, "attendance.manage") or has_capability(actor, "staff.manage")
    ):
        raise PermissionDenied("Enrolling a fingerprint needs attendance or staff management rights.")
    if staff is not None and staff.pk != actor.pk and not has_capability(actor, "attendance.manage"):
        raise PermissionDenied("You can only use your own fingerprint unless you manage attendance.")

    agent = None
    if agent_reference:
        agent = ReaderAgent.objects.select_related("workstation").filter(
            reference=str(agent_reference).strip(), status=ReaderAgent.Status.ACTIVE
        ).first()
    if agent is None:
        qs = ReaderAgent.objects.select_related("workstation").filter(status=ReaderAgent.Status.ACTIVE)
        if workstation_reference:
            qs = qs.filter(workstation__reference=workstation_reference)
        agent = qs.order_by(F("last_seen_at").desc(nulls_last=True), "-created_at").first()

    raw_token = secrets.token_urlsafe(32)
    session = ReaderAgentSession.objects.create(
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        action=action,
        staff=staff,
        requested_by=actor,
        agent=agent,
        workstation_reference=str(workstation_reference or (agent.workstation.reference if agent and agent.workstation else ""))[:48],
        finger_position=str(finger_position or "RIGHT_INDEX")[:20].upper(),
        expires_at=timezone.now() + timedelta(seconds=session_ttl_seconds()),
    )
    return session, raw_token


def session_by_token(raw_token: str) -> ReaderAgentSession | None:
    token = str(raw_token or "").strip()
    if not token:
        return None
    return ReaderAgentSession.objects.select_related("staff", "agent").filter(
        token_hash=hashlib.sha256(token.encode()).hexdigest()
    ).first()


def session_payload(session: ReaderAgentSession) -> dict[str, Any]:
    expired = session.status == ReaderAgentSession.Status.PENDING and session.expires_at <= timezone.now()
    return {
        "status": "EXPIRED" if expired else session.status,
        "action": session.action,
        "staff_id": session.staff_id,
        "staff_name": session.staff.full_name if session.staff_id else None,
        "result": session.result or {},
        "error_code": session.error_code,
        "error_message": session.error_message,
        "expires_at": session.expires_at,
    }


def _consume_session(*, session: ReaderAgentSession, agent: ReaderAgent, action: str) -> ReaderAgentSession:
    """Lock + validate the session inside the caller's transaction."""
    locked = ReaderAgentSession.objects.select_for_update().select_related("staff").get(pk=session.pk)
    if locked.status != ReaderAgentSession.Status.PENDING:
        raise AgentAuthError("SESSION_ALREADY_USED", "This fingerprint request has already been answered.", 409)
    if locked.expires_at <= timezone.now():
        locked.status = ReaderAgentSession.Status.EXPIRED
        locked.save(update_fields=["status", "updated_at"])
        raise AgentAuthError("SESSION_EXPIRED", "This fingerprint request expired. Start the action again.", 409)
    if locked.action != action:
        raise AgentAuthError("SESSION_ACTION_MISMATCH", "This fingerprint request is for a different action.", 409)
    if locked.agent_id and locked.agent_id != agent.pk:
        raise AgentAuthError("SESSION_AGENT_MISMATCH", "This fingerprint request belongs to another reader agent.", 409)
    if locked.workstation_reference and agent.workstation_id and \
            locked.workstation_reference != agent.workstation.reference:
        raise AgentAuthError("SESSION_WORKSTATION_MISMATCH", "This fingerprint request belongs to another workstation.", 409)
    return locked


# --------------------------------------------------------------------------
# Agent side (device signed)
# --------------------------------------------------------------------------
def _active_gallery_rows() -> tuple[str, list[dict[str, Any]]]:
    templates = ReaderAgentTemplate.objects.select_related("staff").filter(
        status=ReaderAgentTemplate.Status.ACTIVE, staff__is_active=True
    ).exclude(fmd="")
    rows = []
    for template in templates:
        staff = template.staff
        if template.expires_at and template.expires_at <= timezone.now():
            continue
        rows.append({
            "staff_id": staff.pk,
            "staff_name": staff.full_name,
            "template_id": template.pk,
            "finger_position": template.finger_position,
            "fmd": template.fmd,
        })
    etag = hashlib.sha256(
        "|".join(f"{t['template_id']}:{t['staff_id']}:{t['fmd'][:32]}" for t in sorted(rows, key=lambda r: r["template_id"])).encode()
    ).hexdigest()[:32]
    return etag, rows


def current_gallery_etag() -> str:
    etag, _rows = _active_gallery_rows()
    return etag


def gallery_for_agent(*, agent: ReaderAgent, nonce: str, known_etag: str = "") -> dict[str, Any]:
    etag, rows = _active_gallery_rows()
    if known_etag and str(known_etag).strip() == etag:
        _close_event(agent=agent, nonce=nonce, outcome="SERVED_CACHED", detail={"etag": etag, "templates": len(rows)})
        return {"etag": etag, "unchanged": True}
    _close_event(agent=agent, nonce=nonce, outcome="SERVED", detail={"templates": len(rows)})
    return {"etag": etag, "unchanged": False, "templates": rows}


def heartbeat(*, agent: ReaderAgent, nonce: str, state: dict[str, Any]) -> dict[str, Any]:
    """Record the agent's own view of the sensor (ready / driver / SDK)."""
    state = state if isinstance(state, dict) else {}
    status = str(state.get("device_status", "unknown"))[:40]
    driver_mode = str(state.get("driver_mode", "")).upper()
    if driver_mode in ReaderAgent.DriverMode.values:
        agent.driver_mode = driver_mode
    agent.device_serial = str(state.get("device_serial", "") or agent.device_serial)[:80]
    agent.sdk_version = str(state.get("sdk_version", "") or agent.sdk_version)[:40]
    agent.firmware = str(state.get("firmware", "") or agent.firmware)[:40]
    agent.last_seen_at = timezone.now()
    agent.last_heartbeat = {
        "device_status": status,
        "detail": str(state.get("detail", ""))[:300],
        "sdk_available": bool(state.get("sdk_available", False)),
        "driver_mode": agent.driver_mode,
        "agent_version": str(state.get("agent_version", ""))[:40],
        "python": str(state.get("python", ""))[:40],
        "hostname": str(state.get("hostname", ""))[:80],
    }
    agent.last_error = str(state.get("detail", ""))[:255] if status not in {"ready", "connected"} else ""
    agent.save(update_fields=[
        "driver_mode", "device_serial", "sdk_version", "firmware",
        "last_seen_at", "last_heartbeat", "last_error", "updated_at",
    ])
    _close_event(agent=agent, nonce=nonce, outcome=status.upper()[:40], detail=agent.last_heartbeat)
    return {
        "agent": agent.reference,
        "workstation": agent.workstation.reference if agent.workstation_id else "",
        "match_threshold": match_threshold(),
        "gallery_etag": current_gallery_etag(),
    }


def fail_session_from_agent(
    *,
    agent: ReaderAgent,
    nonce: str,
    session_token: str,
    error_code: str,
    error_message: str,
    device_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Allow a signed workstation agent to mark a PENDING session FAILED immediately."""
    session = session_by_token(session_token)
    if session is None:
        raise AgentAuthError("SESSION_UNKNOWN", "Unknown fingerprint request.", 404)

    code = str(error_code or "AGENT_ERROR").strip().upper()[:40] or "AGENT_ERROR"
    message = str(error_message or "The fingerprint reader could not complete this request.").strip()[:300]
    now = timezone.now()

    with transaction.atomic():
        locked = ReaderAgentSession.objects.select_for_update().get(pk=session.pk)
        if locked.agent_id and locked.agent_id != agent.pk:
            raise AgentAuthError("SESSION_AGENT_MISMATCH", "This fingerprint request belongs to another reader agent.", 409)
        if locked.workstation_reference and agent.workstation_id and locked.workstation_reference != agent.workstation.reference:
            raise AgentAuthError("SESSION_WORKSTATION_MISMATCH", "This fingerprint request belongs to another workstation.", 409)
        if locked.status == ReaderAgentSession.Status.PENDING:
            if locked.expires_at <= now:
                locked.status = ReaderAgentSession.Status.EXPIRED
                locked.save(update_fields=["status", "updated_at"])
            else:
                locked.consumed_at = now
                _fail_session(locked, code, message)
                locked.save(update_fields=["consumed_at", "updated_at"])
        final_status = locked.status

    if isinstance(device_state, dict) and device_state:
        dev_status = str(device_state.get("device_status", "") or "").strip()[:40]
        driver_mode = str(device_state.get("driver_mode", "") or "").upper()
        if driver_mode in ReaderAgent.DriverMode.values:
            agent.driver_mode = driver_mode
        agent.last_seen_at = now
        if dev_status and dev_status not in {"ready", "connected"}:
            agent.last_error = str(device_state.get("detail") or message)[:255]
        agent.save(update_fields=["driver_mode", "last_seen_at", "last_error", "updated_at"])

    _close_event(agent=agent, nonce=nonce, outcome=code, session=session, detail={"error": message})
    return {"session_token": session_token, "status": final_status, "error_code": code}


def _assert_staff_eligible(staff: User) -> None:
    """The same eligibility the existing workstation flow enforces."""
    if staff is None or not staff.is_active:
        raise ValidationError("This staff account is not active.")
    profile = getattr(staff, "staff_profile", None)
    if profile is not None and getattr(profile, "employment_status", "ACTIVE") != "ACTIVE":
        raise ValidationError("This staff member's employment status does not allow attendance right now.")
    if not has_capability(staff, "attendance.clock"):
        raise PermissionDenied(
            "This staff member's role does not hold the 'attendance.clock' capability, so attendance cannot be recorded."
        )


def _pick_candidate(*, agent: ReaderAgent, session: ReaderAgentSession, candidates: Iterable[dict]) -> tuple[User, ReaderAgentTemplate, float] | None:
    """Choose the best candidate the API will accept, or None.

    Every candidate must point at an ACTIVE template of an eligible staff
    member that the agent was actually served; the best score under the
    threshold wins. A session bound to one staff member only accepts that
    member (credential substitution is refused, not just scored lower).
    """
    threshold = match_threshold()
    best: tuple[User, ReaderAgentTemplate, float] | None = None
    for candidate in candidates or []:
        try:
            staff_id = int(candidate.get("staff_id"))
            template_id = int(candidate.get("template_id"))
            score = float(candidate.get("score"))
        except (TypeError, ValueError, AttributeError):
            continue
        template = ReaderAgentTemplate.objects.select_related("staff").filter(
            pk=template_id, staff_id=staff_id, status=ReaderAgentTemplate.Status.ACTIVE
        ).first()
        if template is None or not template.fmd:
            continue
        staff = template.staff
        if not staff.is_active:
            continue
        if template.expires_at and template.expires_at <= timezone.now():
            continue
        if session.staff_id and session.staff_id != staff.pk:
            continue
        if score > threshold:
            continue
        _assert_staff_eligible(staff)
        if best is None or score < best[2]:
            best = (staff, template, score)
    return best


def process_identify(
    *,
    agent: ReaderAgent,
    nonce: str,
    session_token: str,
    candidates: Iterable[dict],
    captured_at: Any = None,
    device_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a signed identification result and, if attendance, record it.

    Refusals are stored on the session (so the browser stops waiting and sees
    the real reason) *before* the exception leaves this function: a raise inside
    the atomic block would roll the failure record back and leave the operator
    looking at "waiting for fingerprint" until the session expired.
    """
    session = session_by_token(session_token)
    if session is None:
        raise AgentAuthError("SESSION_UNKNOWN", "Unknown fingerprint request.", 404)

    refusal: tuple[str, str, int] | None = None
    result: dict[str, Any] = {}
    with transaction.atomic():
        session = _consume_session(session=session, agent=agent, action=session.action)
        now = timezone.now()
        if captured_at:
            try:
                captured = timezone.datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
                if timezone.is_naive(captured):
                    captured = timezone.make_aware(captured)
            except (TypeError, ValueError):
                raise AgentAuthError("CAPTURE_TIMESTAMP_INVALID", "The capture timestamp is malformed.", 400)
            if abs((now - captured).total_seconds()) > capture_skew_seconds():
                raise AgentAuthError("CAPTURE_TIMESTAMP_STALE", "The capture is too old to be accepted.", 409)

        if session.action == ReaderAgentSession.Action.ENROLL:
            raise AgentAuthError("SESSION_ACTION_MISMATCH", "This request is an enrolment, not a verification.", 409)

        match = None
        try:
            match = _pick_candidate(agent=agent, session=session, candidates=candidates)
        except (ValidationError, PermissionDenied) as exc:
            refusal = ("STAFF_NOT_ELIGIBLE", _first_message(exc), 403)

        if refusal is None and match is None:
            refusal = ("NO_MATCH", "No enrolled staff member matched this fingerprint.", 422)

        if refusal is None:
            staff, template, score = match
            result = {
                "staff_id": staff.pk,
                "staff_name": staff.full_name,
                "employee_code": getattr(getattr(staff, "staff_profile", None), "employee_code", "") or "",
                "match_score": score,
                "template_id": template.pk,
                "finger_position": template.finger_position,
                "captured_at": now.isoformat(),
            }
            attendance_reference = ""
            if session.action in ATTENDANCE_ACTIONS:
                try:
                    record, created = clock_attendance(
                        staff=staff,
                        action=session.action,
                        actor=staff,
                        idempotency_key=f"reader-agent:{agent.reference}:{session.token_hash[:24]}",
                    )
                    attendance_reference = record.reference
                    result["attendance_reference"] = attendance_reference
                    result["attendance_created"] = created
                except (ValidationError, PermissionDenied) as exc:
                    refusal = ("ATTENDANCE_REFUSED", _first_message(exc), 409)

            if refusal is None and session.action in CODE_ACTIONS:
                # The finger is verified exactly as for a clock action; the only
                # difference is what the server does next. This is what removed
                # the console's fabricated "probe": the old terminal posted
                # synthetic minutiae to reception-terminal/ purely to satisfy its
                # validator, so the code it issued was never tied to a real read.
                from . import fingerprint_service

                try:
                    details = fingerprint_service.issue_workstation_code_for_verified_staff(
                        staff=staff,
                        workstation_reference=session.workstation_reference or "",
                        verified_template=template,
                        match_score=score,
                        actor=staff,
                        session_reference=session.token_hash[:16],
                    )
                    result["access_code"] = details.get("access_code", "")
                    result["code_expires_at"] = details.get("expires_at")
                    result["code_valid_until"] = details.get("code_valid_until", "CLOCK_OUT")
                    result["attendance_reference"] = details.get("attendance_reference", "")
                    result["attendance_created"] = details.get("attendance_created")
                except (ValidationError, PermissionDenied) as exc:
                    refusal = ("CODE_ISSUE_REFUSED", _first_message(exc), 409)

            if refusal is None:
                session.status = ReaderAgentSession.Status.DONE
                session.consumed_at = now
                session.result = result
                session.save(update_fields=["status", "consumed_at", "result", "updated_at"])
                _close_event(agent=agent, nonce=nonce, outcome="VERIFIED", staff=staff, match_score=score,
                             session=session, detail={"attendance_reference": attendance_reference})
                _mirror_device_event(agent=agent, nonce=nonce, staff=staff, action=session.action,
                                     score=score, attendance_reference=attendance_reference, captured_at=now)
                agent.last_seen_at = now
                agent.save(update_fields=["last_seen_at", "updated_at"])
                log_action(actor=staff, action="READER_AGENT_VERIFIED", instance=session,
                           metadata={"agent": agent.reference, "action": session.action,
                                     "staff_id": staff.pk, "score": score,
                                     "attendance_reference": attendance_reference})
            else:
                _fail_session(session, refusal[0], refusal[1], result=result)
                _close_event(agent=agent, nonce=nonce, outcome=refusal[0], staff=staff, match_score=score,
                             session=session, detail={"error": refusal[1]})
        else:
            _fail_session(session, refusal[0], refusal[1])
            _close_event(agent=agent, nonce=nonce, outcome=refusal[0], session=session,
                         detail={"error": refusal[1], "candidates": list(candidates or [])[:5]})

    if refusal is not None:
        raise AgentAuthError(refusal[0], refusal[1], refusal[2])
    return {**result, "status": ReaderAgentSession.Status.DONE, "action": session.action}


def process_enroll(
    *,
    agent: ReaderAgent,
    nonce: str,
    session_token: str,
    staff_id: int,
    fmd_b64: str,
    finger_position: str = "RIGHT_INDEX",
    views: int = 0,
) -> dict[str, Any]:
    """Accept a signed enrolment capture and queue it for approval."""
    session = session_by_token(session_token)
    if session is None:
        raise AgentAuthError("SESSION_UNKNOWN", "Unknown fingerprint request.", 404)

    fmd_b64 = str(fmd_b64 or "").strip()
    if not fmd_b64 or len(fmd_b64) > 20_000:
        raise AgentAuthError("FMD_INVALID", "The enrolment template is missing or too large.", 400)
    try:
        import base64

        size = len(base64.b64decode(fmd_b64, validate=True))
    except Exception:
        raise AgentAuthError("FMD_INVALID", "The enrolment template is not valid base64.", 400)
    if size < 100:
        raise AgentAuthError("FMD_INVALID", "The enrolment template is too small to be a fingerprint template.", 400)

    refusal: tuple[str, str, int] | None = None
    result: dict[str, Any] = {}
    with transaction.atomic():
        session = _consume_session(session=session, agent=agent, action=ReaderAgentSession.Action.ENROLL)
        staff = User.objects.filter(pk=staff_id).first()
        if staff is None:
            refusal = ("STAFF_UNKNOWN", "Unknown staff member.", 404)
        elif session.staff_id and session.staff_id != staff.pk:
            refusal = ("STAFF_MISMATCH", "This enrolment request is for another staff member.", 409)
        else:
            try:
                _assert_staff_eligible(staff)
            except (ValidationError, PermissionDenied) as exc:
                refusal = ("STAFF_NOT_ELIGIBLE", _first_message(exc), 403)

        if refusal is None:
            active = ReaderAgentTemplate.objects.select_for_update().filter(
                staff=staff, status__in=[ReaderAgentTemplate.Status.ACTIVE, ReaderAgentTemplate.Status.PENDING]
            ).first()
            if active is not None:
                refusal = (
                    "ALREADY_ENROLLED",
                    "This staff member already has an enrolled fingerprint awaiting approval or in use. "
                    "Revoke it first if the finger has changed.",
                    409,
                )

        if refusal is not None:
            _fail_session(session, refusal[0], refusal[1])
            _close_event(agent=agent, nonce=nonce, outcome=refusal[0], staff=staff, session=session,
                         detail={"error": refusal[1]})
        else:
            template = ReaderAgentTemplate.objects.create(
                staff=staff,
                finger_position=str(finger_position or "RIGHT_INDEX")[:20].upper(),
                fmd=fmd_b64,
                fmd_size=size,
                views=int(views or 0),
                enrolled_by_agent=agent,
                requested_by=session.requested_by,
            )
            result = {"template_id": template.pk, "staff_id": staff.pk, "staff_name": staff.full_name,
                      "status": template.status}
            session.status = ReaderAgentSession.Status.DONE
            session.consumed_at = timezone.now()
            session.result = result
            session.save(update_fields=["status", "consumed_at", "result", "updated_at"])
            _close_event(agent=agent, nonce=nonce, outcome="ENROLLED", staff=staff, session=session,
                         detail={"template_id": template.pk})
            log_action(actor=session.requested_by, action="READER_AGENT_ENROLLMENT_CAPTURED",
                       instance=template, metadata={"agent": agent.reference, "staff_id": staff.pk})

    if refusal is not None:
        raise AgentAuthError(refusal[0], refusal[1], refusal[2])
    return result


def approve_template(*, template: ReaderAgentTemplate, approver: User) -> ReaderAgentTemplate:
    """Approve an agent enrolment, keeping the existing maker/checker rule."""
    from apps.core.approvals import require_independent_approver

    if not has_capability(approver, "attendance.manage"):
        raise PermissionDenied("Approving a fingerprint enrolment requires attendance management rights.")
    override = require_independent_approver(
        requester=template.staff, actor=approver, workflow="fingerprint enrollment",
    )
    with transaction.atomic():
        locked = ReaderAgentTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status != ReaderAgentTemplate.Status.PENDING:
            raise ValidationError({"status": "Only pending enrolments can be approved."})
        for previous in ReaderAgentTemplate.objects.select_for_update().filter(
            staff_id=locked.staff_id, status=ReaderAgentTemplate.Status.ACTIVE
        ):
            previous.status = ReaderAgentTemplate.Status.REVOKED
            previous.decided_by = approver
            previous.decided_at = timezone.now()
            previous.decision_note = "replaced by a newly approved enrolment"
            previous.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
        locked.status = ReaderAgentTemplate.Status.ACTIVE
        locked.decided_by = approver
        locked.decided_at = timezone.now()
        locked.expires_at = locked.decided_at + timedelta(
            days=int(getattr(settings, "FINGERPRINT_TEMPLATE_RETENTION_DAYS", 365))
        )
        if override:
            from apps.core.approvals import override_note

            locked.decision_note = override_note(locked.decision_note, override)[:200]
        locked.save()
    log_action(actor=approver, action="READER_AGENT_ENROLLMENT_APPROVED", instance=locked,
               metadata={"staff_id": locked.staff_id})
    return locked


def reject_template(*, template: ReaderAgentTemplate, approver: User) -> ReaderAgentTemplate:
    if not has_capability(approver, "attendance.manage"):
        raise PermissionDenied("Rejecting a fingerprint enrolment requires attendance management rights.")
    with transaction.atomic():
        locked = ReaderAgentTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status != ReaderAgentTemplate.Status.PENDING:
            raise ValidationError({"status": "Only pending enrolments can be rejected."})
        locked.status = ReaderAgentTemplate.Status.REJECTED
        locked.fmd = ""
        locked.decided_by = approver
        locked.decided_at = timezone.now()
        locked.decision_note = "rejected by approver"
        locked.save(update_fields=["status", "fmd", "decided_by", "decided_at", "decision_note", "updated_at"])
    log_action(actor=approver, action="READER_AGENT_ENROLLMENT_REJECTED", instance=locked,
               metadata={"staff_id": locked.staff_id})
    return locked


def revoke_template(*, template: ReaderAgentTemplate, actor: User, reason: str = "") -> ReaderAgentTemplate:
    if not (has_capability(actor, "attendance.manage") or has_capability(actor, "staff.manage")):
        raise PermissionDenied("Revoking a fingerprint requires attendance or staff management rights.")
    from . import fingerprint_service

    with transaction.atomic():
        locked = ReaderAgentTemplate.objects.select_for_update().select_related("staff").get(pk=template.pk)
        if locked.status not in (ReaderAgentTemplate.Status.ACTIVE, ReaderAgentTemplate.Status.PENDING):
            raise ValidationError({"status": "Only active or pending fingerprint enrolments can be revoked."})
        locked.status = ReaderAgentTemplate.Status.REVOKED
        locked.fmd = ""
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.decision_note = str(reason or "Revoked by management")[:200]
        locked.save(update_fields=["status", "fmd", "decided_by", "decided_at", "decision_note", "updated_at"])
        fingerprint_service.revoke_workstation_credentials_on_clock_out(
            staff=locked.staff, reason="BIOMETRIC_REVOKED",
        )
    log_action(actor=actor, action="READER_AGENT_ENROLLMENT_REVOKED", instance=locked,
               metadata={"staff_id": locked.staff_id, "reason": locked.decision_note})
    return locked


def _fail_session(session: ReaderAgentSession, code: str, message: str, result: dict | None = None) -> None:
    session.status = ReaderAgentSession.Status.FAILED
    session.error_code = code[:40]
    session.error_message = str(message)[:300]
    if result:
        session.result = result
    session.save(update_fields=["status", "error_code", "error_message", "result", "updated_at"])


def _first_message(exc: Exception) -> str:
    if hasattr(exc, "messages") and exc.messages:
        return "; ".join(str(m) for m in exc.messages)
    return str(exc)


def _mirror_device_event(*, agent: ReaderAgent, nonce: str, staff: User, action: str, score: float,
                         attendance_reference: str, captured_at) -> None:
    """Also write the existing append-only device log when the reader is registered.

    Purely additive audit: a missing/registrable device row must never break a
    verification that the API already accepted, so this is best-effort.
    """
    if not agent.device_serial:
        return
    try:
        from ..biometric_models import FingerprintDevice, FingerprintDeviceEvent

        device = FingerprintDevice.objects.filter(serial_number=agent.device_serial).first()
        if device is None:
            return
        source_key = f"reader-agent:{agent.reference}:{nonce}"
        if FingerprintDeviceEvent.objects.filter(source_key=source_key).exists():
            return
        FingerprintDeviceEvent.objects.create(
            device=device,
            event_nonce=f"rag-{nonce}"[:80],
            staff=staff,
            action=str(action)[:30],
            captured_at=captured_at,
            outcome="VERIFIED",
            match_score=score,
            attendance_reference=attendance_reference,
            source_key=source_key,
            details={"agent": agent.reference, "matcher": "dpfj_identify"},
        )
    except Exception:  # pragma: no cover - audit mirror must never break attendance
        pass


def agent_status(agent: ReaderAgent) -> dict[str, Any]:
    fresh_seconds = int(getattr(settings, "READER_AGENT_HEARTBEAT_STALE_SECONDS", 90))
    fresh = bool(agent.last_seen_at and (timezone.now() - agent.last_seen_at).total_seconds() <= fresh_seconds)
    return {
        "reference": agent.reference,
        "label": agent.label,
        "status": agent.status,
        "workstation_id": agent.workstation_id,
        "workstation_reference": agent.workstation.reference if agent.workstation_id else "",
        "workstation_name": agent.workstation.name if agent.workstation_id else "",
        "device_serial": agent.device_serial,
        "model_name": agent.model_name,
        "driver_mode": agent.driver_mode,
        "sdk_version": agent.sdk_version,
        "last_seen_at": agent.last_seen_at,
        "online": fresh,
        "device_status": (agent.last_heartbeat or {}).get("device_status", "unknown"),
        "detail": (agent.last_heartbeat or {}).get("detail", ""),
        "last_error": agent.last_error,
    }
