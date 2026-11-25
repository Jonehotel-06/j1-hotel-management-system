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
            agent=agent, nonce=payload.get("nonce") or request.headers.get(AGENT_HEADERS["nonce"], ""),
        )
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

        try:
            session, token = svc.create_session(
                actor=request.user,
                action=action,
                staff=staff,
                workstation_reference=str(request.data.get("workstation_reference", "") or ""),
                finger_position=str(request.data.get("finger_position", "RIGHT_INDEX") or "RIGHT_INDEX"),
                agent_reference=str(request.data.get("agent_reference", "") or ""),
            )
        except DjangoPermissionDenied:
            raise
        except DjangoValidationError as exc:
            raise ValidationError(_messages(exc))

        agent = session.agent
        base = request.build_absolute_uri("/").rstrip("/")
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
        agents = ReaderAgent.objects.select_related("workstation").all()
        return success_response([svc.agent_status(agent) for agent in agents])


class ReaderAgentProvisionView(APIView):
    """POST /reader-agent/agents/provision/ — create or re-key this workstation's agent."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.accounts.models import Workstation

        reference = str(request.data.get("workstation_reference", "") or "").strip()
        workstation = Workstation.objects.filter(reference=reference).first() if reference else None
        if workstation is None:
            workstation = Workstation.objects.filter(
                department=Workstation.Department.FRONT_DESK, is_active=True
            ).order_by("created_at").first()
        if workstation is None:
            raise ValidationError({"workstation": "No active workstation is configured for this property."})

        try:
            agent, key = svc.provision_agent(
                actor=request.user, workstation=workstation,
                label=str(request.data.get("label", "") or ""),
                device_serial=str(request.data.get("device_serial", "") or ""),
            )
        except DjangoPermissionDenied as exc:
            raise DjangoPermissionDenied(str(exc))

        api_base = request.build_absolute_uri("/").rstrip("/")
        return success_response({
            "reference": agent.reference,
            "signing_key": key,
            "api_base": api_base,
            "workstation_reference": workstation.reference,
            "workstation_name": workstation.name,
            "install_uri": "jone-reader:provision?payload=" + _provision_payload(agent.reference, key, api_base),
            "note": "This key is shown once. Store it with the one-click agent setup on the reception PC.",
        })


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
    """POST /reader-agent/templates/<pk>/<action>/ where action is approve|reject."""

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
            else:
                return _failure("ACTION_UNKNOWN", "Unknown enrolment action.", status.HTTP_400_BAD_REQUEST)
        except DjangoPermissionDenied as exc:
            raise DjangoPermissionDenied(str(exc))
        except DjangoValidationError as exc:
            raise ValidationError(_messages(exc))
        return success_response({"id": template.pk, "status": template.status})
