"""API endpoints for the J-ONE Reader Agent.

Two clearly separated surfaces:

1. Browser endpoints (existing JWT authentication): they only create/read the
   single-use session, provision the agent, and read/approve enrolments. They
   never receive or assert a biometric result.

2. Agent endpoints (HMAC-signed, no JWT): the provisioned native component on
   the reception PC reports the sensor state, fetches the template gallery, and
   submits what the vendor matcher measured. Nothing here is reachable with a
   browser-supplied success flag -- see services/reader_agent_service.py.
"""
from __future__ import annotations

import json

from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

from apps.accounts.capabilities import has_capability
from apps.core.responses import success_response

from .reader_agent_models import ReaderAgent, ReaderAgentTemplate
from .services import reader_agent_service as svc

AGENT_HEADERS = {
    "reference": "X-JONE-Agent-Reference",
    "nonce": "X-JONE-Agent-Nonce",
    "timestamp": "X-JONE-Agent-Timestamp",
    "kind": "X-JONE-Agent-Kind",
    "signature": "X-JONE-Agent-Signature",
}


def _failure(code: str, message: str, http_status: int, errors: dict | None = None) -> Response:
    return Response(
        {"success": False, "code": code, "message": message, "errors": errors or {"detail": [message]}},
        status=http_status,
    )


class ReaderAgentSignatureAuthentication(BaseAuthentication):
    """Authenticate a signed agent request.

    The raw body is read BEFORE any parsing so the exact bytes the agent signed
    are the bytes verified (a re-serialised JSON body would break the digest).
    """

    def authenticate(self, request):
        try:
            raw_body = request.body or b""
        except Exception:  # pragma: no cover - stream already consumed
            raise AuthenticationFailed("The reader agent request body could not be read.")
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AuthenticationFailed("The reader agent request body is not valid JSON.")
        if not isinstance(payload, dict):
            raise AuthenticationFailed("The reader agent request body must be a JSON object.")

        try:
            agent = svc.verify_agent_request(
                reference=request.headers.get(AGENT_HEADERS["reference"], ""),
                nonce=request.headers.get(AGENT_HEADERS["nonce"], ""),
                timestamp=request.headers.get(AGENT_HEADERS["timestamp"], ""),
                kind=request.headers.get(AGENT_HEADERS["kind"], ""),
                body=raw_body,
                signature=request.headers.get(AGENT_HEADERS["signature"], ""),
            )
        except svc.AgentAuthError as exc:
            raise AuthenticationFailed(f"{exc.code}:{exc.message}")

        request.reader_agent = agent
        request.reader_agent_payload = payload
        return (None, agent)

    def authenticate_header(self, request):
        return "X-JONE-Agent-Signature"


def _agent_of(request) -> ReaderAgent:
    agent = getattr(request, "reader_agent", None) or (request.auth if isinstance(request.auth, ReaderAgent) else None)
    if agent is None:  # pragma: no cover - permission layer already refuses this
        raise PermissionDenied("Reader agent authentication is required.")
    return agent


class _AgentView(APIView):
    authentication_classes = [ReaderAgentSignatureAuthentication]
    permission_classes = [AllowAny]

    def handle_exception(self, exc):
        if isinstance(exc, AuthenticationFailed):
            detail = str(exc.detail)
            code, _, message = detail.partition(":")
            http_status = status.HTTP_401_UNAUTHORIZED
            if code in {"AGENT_TIMESTAMP_STALE", "AGENT_NONCE_REPLAY", "AGENT_UNKNOWN",
                        "AGENT_SIGNATURE_INVALID", "AGENT_SIGNATURE_MISSING"}:
                http_status = status.HTTP_401_UNAUTHORIZED
            if code in {"AGENT_NONCE_INVALID", "AGENT_TIMESTAMP_INVALID"}:
                http_status = status.HTTP_400_BAD_REQUEST
            return _failure(code or "AGENT_UNAUTHENTICATED", message or detail, http_status)
        if isinstance(exc, svc.AgentAuthError):
            return _failure(exc.code, exc.message, exc.http_status)
        if isinstance(exc, (ValidationError, DjangoValidationError)):
            return _failure("VALIDATION_ERROR", _messages(exc), status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


def _messages(exc) -> str:
    if hasattr(exc, "messages") and exc.messages:
        return "; ".join(str(m) for m in exc.messages)
    if hasattr(exc, "detail"):
        return str(exc.detail)
    return str(exc)


# --------------------------------------------------------------------------
# Agent surface (signed)
# --------------------------------------------------------------------------
class AgentHeartbeatView(_AgentView):
    """POST /reader-agent/agent/heartbeat/ — sensor + driver state."""

    def post(self, request):
        agent = _agent_of(request)
        payload = getattr(request, "reader_agent_payload", {}) or {}
        try:
            data = svc.heartbeat(agent=agent, nonce=payload.get("nonce") or request.headers.get(AGENT_HEADERS["nonce"], ""),
                                 state=payload.get("state") or {})
        except ReaderAgent.DoesNotExist:  # pragma: no cover
            return _failure("AGENT_UNKNOWN", "Unknown reader agent.", status.HTTP_401_UNAUTHORIZED)
        return success_response(data)


class AgentGalleryView(_AgentView):
    """POST /reader-agent/agent/gallery/ — ACTIVE templates for local matching."""

    def post(self, request):
        agent = _agent_of(request)
        payload = getattr(request, "reader_agent_payload", {}) or {}
        data = svc.gallery_for_agent(
            agent=agent,
            nonce=payload.get("nonce") or request.headers.get(AGENT_HEADERS["nonce"], ""),
            known_etag=str(payload.get("etag") or ""),
        )
        return success_response(data)


class AgentSessionFailView(_AgentView):
    """POST /reader-agent/agent/session-fail/ — mark a PENDING session FAILED on hardware/capture error."""

    def post(self, request):
        agent = _agent_of(request)
        payload = getattr(request, "reader_agent_payload", {}) or {}
        try:
            data = svc.fail_session_from_agent(
                agent=agent,
                nonce=payload.get("nonce") or request.headers.get(AGENT_HEADERS["nonce"], ""),
                session_token=payload.get("session_token", ""),
                error_code=payload.get("error_code", "AGENT_ERROR"),
                error_message=payload.get("error_message", ""),
                device_state=payload.get("device_state") or {},
            )
        except svc.AgentAuthError as exc:
            return _failure(exc.code, exc.message, exc.http_status)
        return success_response(data)


class AgentIdentifyView(_AgentView):
    """POST /reader-agent/agent/identify/ — measured match + attendance."""

    def post(self, request):
        agent = _agent_of(request)
        payload = getattr(request, "reader_agent_payload", {}) or {}
        try:
            data = svc.process_identify(
                agent=agent,
                nonce=payload.get("nonce") or request.headers.get(AGENT_HEADERS["nonce"], ""),
                session_token=payload.get("session_token", ""),
                candidates=payload.get("candidates") or [],
                captured_at=payload.get("captured_at"),
                device_state=payload.get("device_state") or {},
            )
        except svc.AgentAuthError as exc:
            return _failure(exc.code, exc.message, exc.http_status)
        except (DjangoPermissionDenied, PermissionDenied) as exc:
            return _failure("FORBIDDEN", str(exc), status.HTTP_403_FORBIDDEN)
        return success_response(data)


class AgentEnrollView(_AgentView):
    """POST /reader-agent/agent/enroll/ — signed enrolment capture."""

    def post(self, request):
        agent = _agent_of(request)
        payload = getattr(request, "reader_agent_payload", {}) or {}
        try:
            data = svc.process_enroll(
                agent=agent,
                nonce=payload.get("nonce") or request.headers.get(AGENT_HEADERS["nonce"], ""),
                session_token=payload.get("session_token", ""),
                staff_id=payload.get("staff_id"),
                fmd_b64=payload.get("fmd", ""),
                finger_position=payload.get("finger_position", "RIGHT_INDEX"),
                views=payload.get("views", 0),
            )
        except svc.AgentAuthError as exc:
            return _failure(exc.code, exc.message, exc.http_status)
        except (DjangoPermissionDenied, PermissionDenied) as exc:
            return _failure("FORBIDDEN", str(exc), status.HTTP_403_FORBIDDEN)
        return success_response(data)


# --------------------------------------------------------------------------
# Browser surface (existing JWT authentication)
# --------------------------------------------------------------------------
class ReaderAgentSessionCreateView(APIView):
    """POST /reader-agent/sessions/ — mint the single-use biometric challenge.

    Body: {action, staff_id?, workstation_reference?, finger_position?, agent_reference?}
    Response data: {session_token, launch_uri, expires_at, agent_online, ...}
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_capability(request.user, "attendance.clock") and not has_capability(
            request.user, "attendance.manage"
        ):
            raise DjangoPermissionDenied("This user cannot start a fingerprint action.")

        action = str(request.data.get("action", "CLOCK_IN")).upper()
        staff = request.user
        staff_id = request.data.get("staff_id")
        if staff_id not in (None, "", "null"):
            from apps.accounts.models import User

            staff = User.objects.filter(pk=staff_id).first()
            if staff is None:
                raise ValidationError({"staff_id": "Unknown staff member."})

        workstation_reference = str(request.data.get("workstation_reference", "") or "").strip()
        agent_reference = str(request.data.get("agent_reference", "") or "").strip()
        if not workstation_reference and not agent_reference:
            try:
                from apps.accounts.desktop_policy import resolve_receptionist_desktop

                desk, _method = resolve_receptionist_desktop(request)
                if desk is not None:
                    workstation_reference = desk.reference
            except Exception:
                pass

        try:
            session, token = svc.create_session(
                actor=request.user,
                action=action,
                staff=staff,
                workstation_reference=workstation_reference,
                finger_position=str(request.data.get("finger_position", "RIGHT_INDEX") or "RIGHT_INDEX"),
                agent_reference=agent_reference,
            )
        except DjangoPermissionDenied:
            raise
        except DjangoValidationError as exc:
            raise ValidationError(_messages(exc))

        agent = session.agent
        base = request.build_absolute_uri("/").rstrip("/")
        if action == "ENROLL":
            launch_uri = (
                "jone-reader:enroll?session={token}&api={api}&staff_id={staff_id}&finger_position={pos}".format(
                    token=token,
                    api=base,
                    staff_id=session.staff_id or "",
                    pos=session.finger_position or "RIGHT_INDEX",
                )
            )
        else:
            launch_uri = (
                "jone-reader:capture?session={token}&api={api}&action={action}".format(
                    token=token, api=base, action=action,
                )
            )
        return success_response({
            "session_token": token,
            "action": session.action,
            "staff_id": session.staff_id,
            "expires_at": session.expires_at,
            "launch_uri": launch_uri,
            "agent_reference": agent.reference if agent else "",
            "agent_online": bool(agent and agent.last_seen_at and svc.agent_status(agent)["online"]),
            "match_threshold": svc.match_threshold(),
            "poll_seconds": svc.session_ttl_seconds(),
        }, status=status.HTTP_201_CREATED)


class ReaderAgentSessionDetailView(APIView):
    """GET /reader-agent/sessions/<token>/ — poll one session."""

    permission_classes = [IsAuthenticated]

    def get(self, request, token):
        if not has_capability(request.user, "attendance.clock") and not has_capability(
            request.user, "attendance.manage"
        ):
            raise DjangoPermissionDenied("This user cannot read a fingerprint session.")
        session = svc.session_by_token(token)
        if session is None:
            return _failure("SESSION_UNKNOWN", "Unknown fingerprint request.", status.HTTP_404_NOT_FOUND)
        if session.requested_by_id and session.requested_by_id != request.user.pk and not has_capability(
            request.user, "attendance.manage"
        ):
            raise DjangoPermissionDenied("This fingerprint request belongs to another operator.")
        return success_response(svc.session_payload(session))


class ReaderAgentListView(APIView):
    """GET /reader-agent/agents/ — provisioning + live sensor state."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_capability(request.user, "attendance.clock") and not has_capability(
            request.user, "attendance.manage"
        ):
            raise DjangoPermissionDenied("This user cannot view fingerprint readers.")
        qs = ReaderAgent.objects.select_related("workstation")
        if str(request.query_params.get("include_revoked", "")).lower() not in {"1", "true", "yes"}:
            qs = qs.filter(status=ReaderAgent.Status.ACTIVE)
        return success_response([svc.agent_status(agent) for agent in qs.all()])


class ReaderAgentRevokeView(APIView):
    """POST /reader-agent/agents/<reference>/revoke/ — retire an obsolete/lost workstation agent."""

    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        agent = ReaderAgent.objects.select_related("workstation").filter(reference=reference).first()
        if agent is None:
            return _failure("AGENT_UNKNOWN", "Unknown reader agent.", status.HTTP_404_NOT_FOUND)
        try:
            agent = svc.revoke_agent(
                actor=request.user,
                agent=agent,
                reason=str(request.data.get("reason", "") or ""),
            )
        except DjangoPermissionDenied as exc:
            raise DjangoPermissionDenied(str(exc))
        return success_response(svc.agent_status(agent))


def _workstation_for_request(request):
    """The workstation this request is acting for, decided by the SERVER.

    Provisioning used to trust whatever ``workstation_reference`` the browser
    posted, and fell back to "the oldest front-desk workstation" — two ways to
    attach a signing key to the wrong terminal. The desk is resolved exactly like
    every other terminal action (installed desk key, approved device token, or
    the workstation's registered address); only an account that may manage
    attendance can name a workstation explicitly, and the reference must exist.
    Returns (workstation, source).
    """
    from apps.accounts.models import Workstation
    from .views_biometrics import _resolve_reception_terminal

    try:
        reference, workstation = _resolve_reception_terminal(request)
        if workstation is not None:
            return workstation, "desk"
    except (ValidationError, DjangoPermissionDenied):
        pass

    if not svc.may_provision(request.user):
        # Let the provisioning service produce the capability refusal, which
        # names the permission and what to do about it.
        return None, "unresolved"

    posted = str(request.data.get("workstation_reference", "") or "").strip()
    if posted:
        workstation = Workstation.objects.filter(reference=posted, is_active=True).first()
        if workstation is None:
            raise ValidationError({"workstation": f"No active workstation has the reference '{posted}'."})
        return workstation, "named"

    workstation = (
        Workstation.objects.filter(department=Workstation.Department.FRONT_DESK, is_active=True)
        .order_by("created_at")
        .first()
    )
    if workstation is None:
        raise ValidationError({"workstation": "No active reception workstation is configured for this property."})
    return workstation, "front_desk"


class ReaderAgentSetupStatusView(APIView):
    """GET /reader-agent/setup-status/ — can THIS account set up THIS computer?

    Read-only diagnosis for the reader panel. It exists because the live screen
    could only say "Only attendance management can provision a fingerprint reader
    agent": the operator could not tell whether the account lacked the
    capability, or the role→capability mapping was broken. Nothing here grants
    anything — ``can_provision`` is the same check the endpoint enforces.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        status = svc.provisioning_status(request.user)
        workstation, source = (None, "unresolved")
        workstation_error = ""
        try:
            workstation, source = _workstation_for_request(request)
        except (ValidationError, DjangoPermissionDenied) as exc:
            workstation_error = str(getattr(exc, "detail", exc))
        agent_payload = None
        if workstation is not None:
            agent = ReaderAgent.objects.filter(workstation=workstation).first()
            if agent is not None:
                agent_payload = {
                    "reference": agent.reference,
                    "label": agent.label,
                    "status": agent.status,
                    "device_serial": agent.device_serial,
                    "last_seen_at": agent.last_seen_at,
                }
        return success_response({
            **status,
            "workstation_reference": getattr(workstation, "reference", ""),
            "workstation_name": getattr(workstation, "name", ""),
            "workstation_source": source,
            "workstation_error": workstation_error,
            "agent": agent_payload,
            "setup_ready": bool(status["can_provision"] and workstation is not None),
        })


class ReaderAgentProvisionView(APIView):
    """POST /reader-agent/agents/provision/ — create or deliberately re-key this workstation's agent."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        workstation, source = _workstation_for_request(request)
        rotate = bool(request.data.get("rotate", False))
        try:
            agent, key = svc.provision_agent(
                actor=request.user,
                workstation=workstation,
                label=str(request.data.get("label", "") or ""),
                device_serial=str(request.data.get("device_serial", "") or ""),
                allow_rotate=rotate,
            )
        except DjangoPermissionDenied as exc:
            raise DjangoPermissionDenied(str(exc))

        api_base = request.build_absolute_uri("/").rstrip("/")
        payload = {
            "reference": agent.reference,
            "workstation_reference": agent.workstation.reference if agent.workstation_id else "",
            "workstation_name": agent.workstation.name if agent.workstation_id else "",
            "workstation_source": source,
            "already_provisioned": key is None,
            "rotated": bool(rotate and key is not None),
        }
        if key is not None:
            payload.update({
                "signing_key": key,
                "api_base": api_base,
                "install_uri": "jone-reader:provision?payload=" + _provision_payload(agent.reference, key, api_base),
                "note": "This key is shown once. It is handed straight to the reader agent on this computer.",
            })
        else:
            payload["note"] = (
                "This computer already has a valid reader agent, so its key was left untouched. "
                "Use the re-key control only if this machine or its key was replaced."
            )
        return success_response(payload)


def _provision_payload(reference: str, key: str, api_base: str) -> str:
    import base64

    blob = json.dumps({"reference": reference, "key": key, "api": api_base}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(blob).decode().rstrip("=")


class ReaderAgentTemplatesView(APIView):
    """GET /reader-agent/templates/?staff_id= — enrolment status/queue."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ReaderAgentTemplate.objects.select_related("staff", "decided_by")
        staff_id = request.query_params.get("staff_id")
        own_only = not has_capability(request.user, "attendance.manage")
        if own_only:
            qs = qs.filter(staff=request.user)
        elif staff_id:
            qs = qs.filter(staff_id=staff_id)
        rows = [{
            "id": t.pk,
            "staff_id": t.staff_id,
            "staff_name": t.staff.full_name,
            "status": t.status,
            "finger_position": t.finger_position,
            "views": t.views,
            "template_format": t.template_format,
            "created_at": t.created_at,
            "decided_at": t.decided_at,
            "expires_at": t.expires_at,
            "decided_by": getattr(t.decided_by, "full_name", "") if t.decided_by_id else "",
            "decision_note": t.decision_note,
        } for t in qs[:200]]
        return success_response(rows)


class ReaderAgentTemplateActionView(APIView):
    """POST /reader-agent/templates/<pk>/<action>/ where action is approve|reject|revoke."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk, action):
        template = ReaderAgentTemplate.objects.select_related("staff").filter(pk=pk).first()
        if template is None:
            return _failure("TEMPLATE_UNKNOWN", "Unknown fingerprint enrolment.", status.HTTP_404_NOT_FOUND)
        try:
            if action == "approve":
                template = svc.approve_template(template=template, approver=request.user)
            elif action == "reject":
                template = svc.reject_template(template=template, approver=request.user)
            elif action == "revoke":
                template = svc.revoke_template(
                    template=template,
                    actor=request.user,
                    reason=str(request.data.get("note") or request.data.get("reason") or ""),
                )
            else:
                return _failure("ACTION_UNKNOWN", "Unknown enrolment action.", status.HTTP_400_BAD_REQUEST)
        except DjangoPermissionDenied as exc:
            raise DjangoPermissionDenied(str(exc))
        except DjangoValidationError as exc:
            raise ValidationError(_messages(exc))
        return success_response({"id": template.pk, "status": template.status})
