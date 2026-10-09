"""Facial enrollment and review endpoints for staff attendance.

Responses never include descriptors, probes or any other biometric value.
"""
from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.core.pagination import StandardPagination

from .face_models import FaceTemplate
from .services import face_service


def _template_summary(template: FaceTemplate | None) -> dict:
    if template is None:
        return {"status": None}
    return {
        "id": template.pk,
        "status": template.status,
        "requested_at": template.requested_at,
        "decided_at": template.decided_at,
        "expires_at": template.expires_at,
        "decision_note": template.decision_note,
    }


def _raise_django(exc):
    if isinstance(exc, DjangoValidationError):
        raise ValidationError(exc.message_dict if hasattr(exc, "error_dict") else {"detail": exc.messages})
    raise exc


class FaceEnrollmentView(APIView):
    """Self-service: check own enrollment status or request enrollment with a descriptor."""
    permission_classes = [HasCapability]; required_capability = "attendance.clock"

    def get(self, request):
        latest = FaceTemplate.objects.filter(staff=request.user).first()
        active = FaceTemplate.objects.filter(staff=request.user, status=FaceTemplate.Status.ACTIVE).first()
        return success_response({"enrolled": active is not None, "latest": _template_summary(latest),
                                 "verification_required": face_service.face_verification_required()})

    def post(self, request):
        try:
            template = face_service.request_enrollment(staff=request.user, actor=request.user,
                                                       descriptor=request.data.get("descriptor"))
        except DjangoValidationError as exc:
            _raise_django(exc)
        return success_response(_template_summary(template), message="Enrollment submitted for approval.",
                                status=status.HTTP_201_CREATED)


class FaceEnrollmentQueueView(APIView):
    """Reviewers (attendance.manage) list enrollments without descriptors."""
    permission_classes = [HasCapability]; required_capability = "attendance.manage"

    def get(self, request):
        queryset = FaceTemplate.objects.select_related("staff").all()
        if value := request.query_params.get("status"):
            queryset = queryset.filter(status=value.upper())
        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = [{**_template_summary(t), "staff_id": t.staff_id, "staff_email": t.staff.email,
                 "is_self": t.staff_id == request.user.pk} for t in page]
        return paginator.get_paginated_response(data)


class FaceEnrollmentActionView(APIView):
    permission_classes = [HasCapability]; required_capability = "attendance.manage"
    ACTIONS = {"approve", "reject", "revoke"}

    def post(self, request, pk: int, action: str):
        if action not in self.ACTIONS:
            raise ValidationError({"action": "Unknown enrollment action."})
        template = get_object_or_404(FaceTemplate.objects.select_related("staff"), pk=pk)
        if not has_capability(request.user, "attendance.manage"):
            raise PermissionDenied("Managing facial enrollment requires attendance management rights.")
        try:
            if action == "approve":
                template = face_service.approve_enrollment(template=template, approver=request.user)
            else:
                template = face_service.reject_or_revoke(template=template, actor=request.user,
                                                         revoke=action == "revoke",
                                                         note=str(request.data.get("note", ""))[:200])
        except DjangoValidationError as exc:
            _raise_django(exc)
        return success_response(_template_summary(template), message=f"Enrollment {action}d.")
