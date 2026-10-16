"""API endpoints for fingerprint enrollment, biometric clocking, and temporary credentials."""
from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.desktop_policy import (
    front_desk_desktop_ip_allowed, presented_desktop_key, receptionist_desktop_for_key,
)
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.core.pagination import StandardPagination

from .biometric_models import (
    FingerprintDevice,
    FingerprintDeviceEvent,
    FingerprintTemplate,
    FingerprintVerificationAttempt,
    TemporaryWorkstationCredential,
)
from .models import AttendanceRecord
from .services import fingerprint_device, fingerprint_service


def _fp_summary(template: FingerprintTemplate | None) -> dict:
    if template is None:
        return {"status": None}
    return {
        "id": template.pk,
        "status": template.status,
        "finger_position": template.finger_position,
        "template_format": template.template_format,
        "requested_at": template.requested_at,
        "decided_at": template.decided_at,
        "expires_at": template.expires_at,
        "decision_note": template.decision_note,
    }


def _raise_django(exc):
    if isinstance(exc, DjangoValidationError):
        raise ValidationError(exc.message_dict if hasattr(exc, "error_dict") else {"detail": exc.messages})
    raise exc


class FingerprintEnrollmentView(APIView):
    """Self-service: check own fingerprint enrollment status or request enrollment."""
    permission_classes = [HasCapability]; required_capability = "attendance.clock"

    def get(self, request):
        latest = FingerprintTemplate.objects.filter(staff=request.user).first()
        active = FingerprintTemplate.objects.filter(staff=request.user, status=FingerprintTemplate.Status.ACTIVE).first()
        return success_response({
            "enrolled": active is not None,
            "latest": _fp_summary(latest),
            "verification_required": fingerprint_service.fingerprint_verification_required(),
        })

    def post(self, request):
        try:
            template = fingerprint_service.request_fingerprint_enrollment(
                staff=request.user,
                actor=request.user,
                minutiae=request.data.get("minutiae"),
                finger_position=request.data.get("finger_position", "RIGHT_INDEX"),
            )
        except DjangoValidationError as exc:
            _raise_django(exc)
        return success_response(_fp_summary(template), message="Fingerprint enrollment submitted for approval.",
                                status=status.HTTP_201_CREATED)


class FingerprintEnrollmentQueueView(APIView):
    """Supervisors (attendance.manage) list fingerprint enrollments."""
    permission_classes = [HasCapability]; required_capability = "attendance.manage"

    def get(self, request):
        queryset = FingerprintTemplate.objects.select_related("staff").all()
        if value := request.query_params.get("status"):
            queryset = queryset.filter(status=value.upper())
        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = [{**_fp_summary(t), "staff_id": t.staff_id, "staff_email": t.staff.email,
                 "is_self": t.staff_id == request.user.pk} for t in page]
        return paginator.get_paginated_response(data)


class FingerprintEnrollmentActionView(APIView):
    permission_classes = [HasCapability]; required_capability = "attendance.manage"
    ACTIONS = {"approve", "reject", "revoke"}

    def post(self, request, pk: int, action: str):
        if action not in self.ACTIONS:
            raise ValidationError({"action": "Unknown enrollment action."})
        template = get_object_or_404(FingerprintTemplate.objects.select_related("staff"), pk=pk)
        try:
            if action == "approve":
                template = fingerprint_service.approve_fingerprint_enrollment(template=template, approver=request.user)
            else:
                template.status = FingerprintTemplate.Status.REVOKED if action == "revoke" else FingerprintTemplate.Status.REJECTED
                template.clear_minutiae()
                template.decided_by = request.user
                template.decision_note = str(request.data.get("note", ""))[:200]
                template.save()
        except DjangoValidationError as exc:
            _raise_django(exc)
        return success_response(_fp_summary(template), message=f"Fingerprint enrollment {action}d.")


class TemporaryWorkstationCredentialIssueView(APIView):
    """Verify staff fingerprint at approved reception terminal and issue temporary login token + numeric code.

    Item F: identifying the staff member here IS their clock-in. The attendance
    record is opened (or the open one reused — never duplicated) after the probe
    has matched, and the code is bound to that record, so clock-out expires it.
    A refused probe leaves no attendance behind.
    """
    permission_classes = []  # Authenticated by presented terminal key + staff email + fingerprint

    def post(self, request):
        # The station question answered exactly once, for the whole module: the
        # desk by key OR by its registered address (so a desk enrolled by
        # address can mint codes too). This endpoint is unauthenticated, so the
        # management off-desk route cannot apply here — an anonymous request has
        # no account to hold that authority, and the resolver refuses it.
        ws_reference, workstation = _resolve_reception_terminal(request)

        from apps.accounts.models import User
        email = (request.data.get("email") or "").strip().lower()
        employee_code = (request.data.get("employee_code") or "").strip()
        probe = request.data.get("fingerprint_probe") or request.data.get("probe")
        user = None
        if email:
            user = User.objects.select_related("staff_profile").filter(email__iexact=email, is_active=True).first()
        elif employee_code:
            user = (
                User.objects.select_related("staff_profile")
                .filter(staff_profile__employee_code__iexact=employee_code, is_active=True)
                .first()
            )
        if not user:
            raise ValidationError({"email": "Staff account not found or inactive."})

        try:
            details = fingerprint_service.issue_temporary_workstation_credential(
                staff=user,
                workstation_reference=workstation.reference,
                probe=probe,
                issue_numeric_code=True,
                return_details=True,
            )
        except DjangoValidationError as exc:
            _raise_django(exc)

        profile = getattr(user, "staff_profile", None)
        attendance_created = bool(details.get("attendance_created"))
        return success_response({
            "temporary_token": details["temporary_token"],
            "access_code": details["access_code"],
            "expires_at": details["expires_at"],
            "workstation": workstation.reference,
            "department": details["department"],
            "staff_id": user.pk,
            "staff_email": user.email,
            "staff_name": user.full_name,
            "employee_code": profile.employee_code if profile else "",
            "attendance_reference": details["attendance_reference"],
            "attendance_created": attendance_created,
        }, message=(
            "Staff member clocked in and a temporary workstation access code generated."
            if attendance_created
            else "Temporary workstation sign-in credential and numeric access code generated."
        ))


#: Why a request that NAMED the desk was still refused: the desk is enrolled,
#: but this device is not on the network the desk is pinned to.
DETAIL_DESK_HOST_MISMATCH = (
    "Attendance can only be recorded from the configured Front Desk desktop IP address. "
    "Ask an administrator to update Settings if the Front Desk desktop has changed."
)
DETAIL_DESK_REQUIRED = (
    "Attendance can only be recorded at an enrolled Receptionist Desktop. "
    "Ask an administrator to enrol this browser (desktop key or registered address)."
)
DETAIL_KEY_REQUIRED = (
    "Reception Attendance Workstation requires an approved Receptionist Desktop key."
)


def _resolve_reception_terminal(request):
    """Resolve the Reception Attendance Workstation for this request.

    Three ways in, tried in this order:

    1. **The desk itself, by either enrolment route** — the browser presents the
       Receptionist Desktop key, or the request arrives from the address an
       administrator registered for an active FRONT_DESK workstation. Both are
       the routes sign-in accepts, so a desk enrolled by address is a fully
       working attendance station too, and neither asks the desk to type
       anything. A desk outside the network address in hotel Settings is not the
       desk for this purpose and falls through to the rules below.
    2. **Management that may record attendance for others** (the accounts that
       already hold supervised-fallback authority, see
       ``apps.accounts.desktop_policy.may_record_attendance_off_desk``), from
       wherever it is. This is the supervisor's route when the front desk is
       unreachable, and the reason an administrator is never locked out of
       attendance by a desk that has yet to be enrolled or has moved: their
       authority is already server-side, and every action below is audited with
       the account, the reason and the authority. Each use is recorded as
       ``ATTENDANCE_OFF_DESK_TERMINAL``.
    3. **Nobody else.** An operational account reaches this endpoint only from a
       desk it signed in at, because their sign-in is bound to the desk; a
       personal phone has neither a session the policy accepts nor
       ``attendance.manage``. The restriction is enforced server side, never
       from a user agent, screen size, or an address the client asserts.

    Every route still resolves to a REGISTERED active workstation: the endpoint
    never invents a terminal reference, so attendance can never be attributed to
    a workstation that does not exist.
    """
    from apps.accounts.desktop_policy import (
        attendance_off_desk_authority,
        may_record_attendance_off_desk,
        resolve_receptionist_desktop,
    )
    from apps.accounts.models import Workstation
    from apps.audit.services import log_action

    desk, _method = resolve_receptionist_desktop(request)
    if desk is not None and front_desk_desktop_ip_allowed(request):
        return desk.reference, desk

    user = getattr(request, "user", None)
    authenticated = bool(user and getattr(user, "is_authenticated", False))
    exempt = authenticated and may_record_attendance_off_desk(user)

    if desk is not None and not exempt:
        # The desk was named but is pinned elsewhere: say exactly that, rather
        # than "enrol this browser", which would send the desk to the wrong fix.
        raise PermissionDenied(DETAIL_DESK_HOST_MISMATCH)
    if not authenticated:
        raise PermissionDenied(DETAIL_KEY_REQUIRED)
    if not exempt:
        raise PermissionDenied(DETAIL_DESK_REQUIRED)
    authority = attendance_off_desk_authority(user)

    log_action(
        actor=user,
        action="ATTENDANCE_OFF_DESK_TERMINAL",
        request=request,
        metadata={
            "role": user.role,
            "authority": authority,
            "requested_action": str(request.data.get("action") or "").strip().upper()
            if hasattr(request.data, "get")
            else "",
            "receptionist_desktop_used": False,
        },
        summary="Attendance terminal operated away from the Receptionist Desktop by management",
    )

    ws_ref = (
        (request.data.get("workstation_reference") if hasattr(request.data, "get") else "")
        or request.query_params.get("workstation_reference")
        or request.headers.get("X-JONE-Terminal")
        or ""
    ).strip()
    ws = Workstation.objects.filter(reference=ws_ref, is_active=True).first() if ws_ref else None
    if ws is None:
        # Fall back to a front desk desk that is enrolled EITHER way: an issued
        # key or a registered address both mean "this is the reception desk".
        enrolled = Workstation.objects.filter(is_active=True).exclude(
            sign_in_key_hash="", ip_address=""
        ).filter(department=Workstation.Department.FRONT_DESK)
        ws = (
            enrolled.first()
            or Workstation.objects.exclude(sign_in_key_hash="", ip_address="").filter(is_active=True).first()
        )
    if ws is None:
        raise PermissionDenied(
            "No active Receptionist Desktop workstation is registered. Register the front desk "
            "workstation and enrol it (issue its desktop key, or register its address) before "
            "recording attendance."
        )
    return ws.reference, ws


class ReceptionAttendanceTerminalView(APIView):
    """Dedicated Reception Attendance Workstation API for fingerprint verification, clocking, and numeric code issuance."""
    permission_classes = []

    def get(self, request):
        ws_reference, ws = _resolve_reception_terminal(request)
        from django.utils import timezone
        from apps.accounts.models import User

        now = timezone.now()
        today = timezone.localdate()
        active_records = list(
            AttendanceRecord.objects.select_related("staff", "staff__staff_profile")
            .filter(clock_out_at__isnull=True)
            .order_by("-clock_in_at")[:100]
        )
        completed_today = AttendanceRecord.objects.filter(
            business_date=today, clock_out_at__isnull=False
        ).count()
        active_creds = {
            c.staff_id: c
            for c in TemporaryWorkstationCredential.objects.filter(
                session_active=True, revoked_at__isnull=True
            ).order_by("created_at")
        }
        enrolled_count = FingerprintTemplate.objects.filter(
            status=FingerprintTemplate.Status.ACTIVE, staff__is_active=True
        ).count()
        pending_count = FingerprintTemplate.objects.filter(
            status=FingerprintTemplate.Status.PENDING, staff__is_active=True
        ).count()
        devices = [
            {
                "id": d.pk,
                "serial_number": d.serial_number,
                "label": d.label,
                "vendor": d.vendor,
                "model_name": d.model_name,
                "status": d.status,
                "workstation_reference": d.workstation.reference if d.workstation_id else "",
                "last_seen_at": d.last_seen_at,
                "last_error": d.last_error,
            }
            for d in FingerprintDevice.objects.select_related("workstation").all()[:25]
        ]
        recent_attempts = [
            {
                "id": a.pk,
                "staff_id": a.staff_id,
                "staff_name": a.staff.full_name if a.staff else "",
                "action": a.action,
                "outcome": a.outcome,
                "match_score": a.match_score,
                "workstation_reference": a.workstation_reference,
                "created_at": a.created_at,
            }
            for a in FingerprintVerificationAttempt.objects.select_related("staff").order_by("-created_at")[:20]
        ]

        on_duty_staff = []
        for rec in active_records:
            profile = getattr(rec.staff, "staff_profile", None)
            cred = active_creds.get(rec.staff_id)
            on_duty_staff.append({
                "attendance_reference": rec.reference,
                "staff_id": rec.staff_id,
                "staff_name": rec.staff.full_name,
                "staff_email": rec.staff.email,
                "employee_code": profile.employee_code if profile else f"EMP-{rec.staff_id:04d}",
                "role": rec.staff.role,
                "department": fingerprint_service.staff_department_code(rec.staff),
                "status": rec.status,
                "clock_in_at": rec.clock_in_at,
                "has_active_code": cred is not None,
                "code_workstation": cred.workstation_reference if cred else "",
                "code_expires_at": cred.expires_at if cred else None,
            })

        return success_response({
            "workstation_reference": ws_reference,
            "workstation_name": ws.name if ws else "Reception Attendance Workstation",
            "server_time": now,
            "summary": {
                "enrolled_staff_count": enrolled_count,
                "pending_enrollments_count": pending_count,
                "currently_clocked_in_count": sum(1 for r in active_records if r.status == AttendanceRecord.Status.ON_DUTY),
                "currently_on_break_count": sum(1 for r in active_records if r.status == AttendanceRecord.Status.ON_BREAK),
                "clocked_out_today_count": completed_today,
                "active_access_codes_count": len(active_creds),
            },
            "active_attendance": on_duty_staff,
            "devices": devices,
            "recent_attempts": recent_attempts,
        })

    def post(self, request):
        ws_reference, _ws = _resolve_reception_terminal(request)
        action = (request.data.get("action") or "CLOCK_IN").strip().upper()
        probe = request.data.get("fingerprint_probe") or request.data.get("probe") or request.data.get("minutiae")
        manual_override_reason = str(request.data.get("manual_override_reason") or "")
        failure_classification = str(
            request.data.get("failure_classification") or request.data.get("classification") or ""
        )
        failure_note = str(request.data.get("note") or "")
        email = (request.data.get("email") or "").strip()
        employee_code = (request.data.get("employee_code") or "").strip()
        staff_id = request.data.get("staff_id")
        target_ws_ref = (request.data.get("workstation_reference") or ws_reference).strip()

        try:
            result = fingerprint_service.reception_attendance_workstation_action(
                action=action,
                probe=probe,
                workstation_reference=target_ws_ref,
                email=email,
                employee_code=employee_code,
                staff_id=int(staff_id) if staff_id else None,
                actor=request.user if getattr(request.user, "is_authenticated", False) else None,
                manual_override_reason=manual_override_reason,
                classification=failure_classification,
                note=failure_note,
            )
        except DjangoValidationError as exc:
            _raise_django(exc)

        return success_response(result, message=f"Reception attendance workstation {action.lower()} completed.")


class StaffFingerprintStatusEnrollView(APIView):
    """Supervisor/Admin view to inspect or manage a specific staff member's fingerprint enrollment & status."""
    permission_classes = [HasCapability]
    required_capability = "staff.view"

    def get(self, request, staff_id: int):
        from apps.accounts.models import User

        staff = get_object_or_404(User.objects.select_related("staff_profile"), pk=staff_id)
        return success_response(fingerprint_service.get_staff_biometric_status(staff))

    def post(self, request, staff_id: int):
        if not (has_capability(request.user, "attendance.manage") or has_capability(request.user, "staff.manage")):
            raise PermissionDenied("Enrolling or managing another staff member's fingerprint requires attendance or staff management permission.")
        from apps.accounts.models import User

        staff = get_object_or_404(User.objects.select_related("staff_profile"), pk=staff_id)
        action = (request.data.get("action") or "enroll").strip().lower()
        try:
            if action in ("enroll", "re_enroll", "replace"):
                template = fingerprint_service.request_fingerprint_enrollment(
                    staff=staff,
                    actor=request.user,
                    minutiae=request.data.get("minutiae") or request.data.get("fingerprint_probe"),
                    finger_position=request.data.get("finger_position", "RIGHT_INDEX"),
                )
                auto_approve = bool(request.data.get("auto_approve", False))
                if auto_approve and request.user.pk != staff.pk and has_capability(request.user, "attendance.manage"):
                    template = fingerprint_service.approve_fingerprint_enrollment(
                        template=template, approver=request.user
                    )
                return success_response(
                    fingerprint_service.get_staff_biometric_status(staff),
                    message="Fingerprint enrollment recorded.",
                    status=status.HTTP_201_CREATED,
                )
            elif action == "revoke":
                from django.utils import timezone

                now = timezone.now()
                for tpl in FingerprintTemplate.objects.filter(
                    staff=staff, status__in=[FingerprintTemplate.Status.ACTIVE, FingerprintTemplate.Status.PENDING]
                ):
                    tpl.status = FingerprintTemplate.Status.REVOKED
                    tpl.clear_minutiae()
                    tpl.decided_by = request.user
                    tpl.decided_at = now
                    tpl.decision_note = str(request.data.get("note") or "Revoked by management")[:200]
                    tpl.save()
                fingerprint_service.revoke_workstation_credentials_on_clock_out(
                    staff=staff, reason="BIOMETRIC_REVOKED"
                )
                return success_response(
                    fingerprint_service.get_staff_biometric_status(staff),
                    message="Fingerprint template revoked.",
                )
            else:
                raise ValidationError({"action": "Unsupported biometric action."})
        except DjangoValidationError as exc:
            _raise_django(exc)


class FingerprintDeviceListCreateView(APIView):
    """List and register physical/mocked fingerprint reader devices bound to workstations."""
    permission_classes = [HasCapability]
    required_capability = "attendance.manage"

    def get(self, request):
        devices = FingerprintDevice.objects.select_related("workstation", "registered_by").all()
        data = [
            {
                "id": d.pk,
                "serial_number": d.serial_number,
                "label": d.label,
                "workstation_id": d.workstation_id,
                "workstation_reference": d.workstation.reference if d.workstation else "",
                "workstation_name": d.workstation.name if d.workstation else "",
                "vendor": d.vendor,
                "model_name": d.model_name,
                "status": d.status,
                "last_seen_at": d.last_seen_at,
                "last_error": d.last_error,
                "notes": d.notes,
                "created_at": d.created_at,
            }
            for d in devices
        ]
        return success_response(data)

    def post(self, request):
        from apps.accounts.models import Workstation

        ws_id = request.data.get("workstation_id")
        ws_ref = (request.data.get("workstation_reference") or "").strip()
        ws = None
        if ws_id:
            ws = Workstation.objects.filter(pk=ws_id).first()
        elif ws_ref:
            ws = Workstation.objects.filter(reference=ws_ref).first()
        if ws is None:
            raise ValidationError({"workstation": "A valid registered workstation is required."})
        try:
            device = fingerprint_device.register_device(
                serial_number=request.data.get("serial_number", ""),
                label=request.data.get("label", ""),
                workstation=ws,
                actor=request.user,
                vendor=request.data.get("vendor", ""),
                model_name=request.data.get("model_name", ""),
                notes=request.data.get("notes", ""),
            )
        except DjangoValidationError as exc:
            _raise_django(exc)
        return success_response(
            {
                "id": device.pk,
                "serial_number": device.serial_number,
                "label": device.label,
                "workstation_id": device.workstation_id,
                "workstation_reference": device.workstation.reference,
                "vendor": device.vendor,
                "model_name": device.model_name,
                "status": device.status,
            },
            message="Fingerprint device registered.",
            status=status.HTTP_201_CREATED,
        )

