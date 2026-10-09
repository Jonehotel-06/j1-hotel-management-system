"""API endpoints for fingerprint enrollment, biometric clocking, and temporary credentials."""
from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.desktop_policy import receptionist_desktop_for_key, presented_desktop_key
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.core.pagination import StandardPagination

from .biometric_models import FingerprintTemplate
from .services import fingerprint_service


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
    """Verify staff fingerprint at approved reception terminal and issue temporary login token."""
    permission_classes = []  # Authenticated by presented terminal key + staff email + fingerprint

    def post(self, request):
        terminal_key = presented_desktop_key(request)
        workstation = receptionist_desktop_for_key(terminal_key)
        if not workstation:
            raise PermissionDenied("Temporary credentials may only be minted from an approved Receptionist Desktop.")

        from apps.accounts.models import User
        email = (request.data.get("email") or "").strip().lower()
        probe = request.data.get("fingerprint_probe") or request.data.get("probe")
        user = User.objects.filter(email=email, is_active=True).first()
        if not user:
            raise ValidationError({"email": "Staff account not found or inactive."})

        try:
            raw_token, expires_at = fingerprint_service.issue_temporary_workstation_credential(
                staff=user,
                workstation_reference=workstation.reference,
                probe=probe,
            )
        except DjangoValidationError as exc:
            _raise_django(exc)

        return success_response({
            "temporary_token": raw_token,
            "expires_at": expires_at,
            "workstation": workstation.reference,
            "staff_email": user.email,
        }, message="Temporary workstation sign-in credential generated.")
