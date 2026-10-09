"""Capability-scoped workforce operations API."""
from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import AttendanceEvent, AttendanceRecord, LeaveRequest, LeaveRequestEvent, ShiftAssignment, ShiftAssignmentEvent, ShiftTemplate, StaffProfile
from apps.core.exceptions import FaceVerificationFailedError
from .services import face_service

from .serializers import (
    AttendanceClockSerializer, AttendanceRecordDetailSerializer, AttendanceRecordListSerializer,
    LeaveCancelSerializer, LeaveRequestCreateSerializer, LeaveRequestDetailSerializer, LeaveRequestListSerializer,
    LeaveReviewSerializer, ShiftAssignmentCreateSerializer, ShiftAssignmentDetailSerializer, ShiftAssignmentListSerializer,
    ShiftCancelSerializer, ShiftTemplateSerializer, StaffProfileCreateSerializer, StaffProfileSerializer,
)
from .services.staff_operations_service import (
    cancel_leave, cancel_shift_assignment, clock_attendance, create_shift_assignment, ensure_staff_profile,
    request_leave, review_leave, scoped_key,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination(); page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True).data)


def _profile_queryset():
    return StaffProfile.objects.select_related("user").order_by("user__last_name", "user__first_name", "pk")


def _shift_queryset(*, detail=False):
    queryset = ShiftAssignment.objects.select_related("staff", "template", "assigned_by", "cancelled_by")
    if detail:
        queryset = queryset.prefetch_related(Prefetch("events", queryset=ShiftAssignmentEvent.objects.select_related("actor").order_by("created_at", "pk")))
    return queryset


def _shift_or_404(reference, *, detail=False):
    shift = _shift_queryset(detail=detail).filter(reference=reference).first()
    if shift is None: raise NotFound("Shift assignment not found.")
    return shift


def _attendance_queryset(*, detail=False):
    queryset = AttendanceRecord.objects.select_related("staff", "shift")
    if detail:
        queryset = queryset.prefetch_related(Prefetch("events", queryset=AttendanceEvent.objects.select_related("actor").order_by("occurred_at", "pk")))
    return queryset


def _attendance_or_404(request, reference, *, detail=False):
    record = _attendance_queryset(detail=detail).filter(reference=reference).first()
    if record is None or (not has_capability(request.user, "attendance.manage") and record.staff_id != request.user.pk):
        raise NotFound("Attendance record not found.")
    return record


def _leave_queryset(*, detail=False):
    queryset = LeaveRequest.objects.select_related("staff", "reviewer", "cancelled_by")
    if detail:
        queryset = queryset.prefetch_related(Prefetch("events", queryset=LeaveRequestEvent.objects.select_related("actor").order_by("created_at", "pk")))
    return queryset


def _leave_or_404(request, reference, *, detail=False):
    leave_request = _leave_queryset(detail=detail).filter(reference=reference).first()
    if leave_request is None:
        raise NotFound("Leave request not found.")
    if leave_request.staff_id != request.user.pk and not has_capability(request.user, "leave.approve"):
        raise NotFound("Leave request not found.")
    return leave_request


@extend_schema(tags=["Admin · Staff operations"], summary="List or create staff operational profiles")
class StaffProfileListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "staff.profile.manage"

    def get(self, request):
        queryset = _profile_queryset()
        if status_value := request.query_params.get("employment_status"):
            queryset = queryset.filter(employment_status=status_value)
        if department := request.query_params.get("department"):
            queryset = queryset.filter(department__iexact=department)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(Q(employee_code__icontains=search) | Q(user__email__icontains=search) | Q(user__first_name__icontains=search) | Q(user__last_name__icontains=search))
        return _page(self, request, queryset, StaffProfileSerializer)

    def post(self, request):
        serializer = StaffProfileCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        staff = User.objects.filter(pk=data["user_id"]).first()
        if staff is None: raise NotFound("Staff user not found.")
        profile, created = ensure_staff_profile(staff=staff)
        profile_fields = ("department", "job_title", "employment_start", "emergency_contact_name", "emergency_contact_phone", "notes")
        if created or any(field in data for field in profile_fields):
            for field in profile_fields:
                if field in data: setattr(profile, field, data[field])
            profile.save()
        if created: log_action(actor=request.user, action="STAFF_PROFILE_CREATED", instance=profile, request=request, metadata={"employee_code": profile.employee_code})
        return success_response(StaffProfileSerializer(_profile_queryset().get(pk=profile.pk)).data,
                                message="Staff profile created." if created else "Existing staff profile returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class StaffProfileDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "staff.profile.manage"

    def patch(self, request, pk):
        profile = _profile_queryset().filter(pk=pk).first()
        if profile is None: raise NotFound("Staff profile not found.")
        serializer = StaffProfileSerializer(profile, data=request.data, partial=True); serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        log_action(actor=request.user, action="STAFF_PROFILE_UPDATED", instance=profile, request=request)
        return success_response(StaffProfileSerializer(_profile_queryset().get(pk=profile.pk)).data, message="Staff profile updated.")


@extend_schema(tags=["Admin · Staff operations"], summary="List or create reusable shift templates")
class ShiftTemplateListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "shift.manage"

    def get(self, request):
        queryset = ShiftTemplate.objects.all().order_by("department", "start_time", "name")
        if request.query_params.get("active") == "true": queryset = queryset.filter(is_active=True)
        if department := request.query_params.get("department"): queryset = queryset.filter(department__iexact=department)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(Q(code__icontains=search) | Q(name__icontains=search) | Q(department__icontains=search))
        return _page(self, request, queryset, ShiftTemplateSerializer)

    def post(self, request):
        serializer = ShiftTemplateSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        template = serializer.save()
        log_action(actor=request.user, action="SHIFT_TEMPLATE_CREATED", instance=template, request=request)
        return success_response(ShiftTemplateSerializer(template).data, message="Shift template created.", status=status.HTTP_201_CREATED)


class ShiftTemplateDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "shift.manage"

    def patch(self, request, pk):
        template = ShiftTemplate.objects.filter(pk=pk).first()
        if template is None: raise NotFound("Shift template not found.")
        serializer = ShiftTemplateSerializer(template, data=request.data, partial=True); serializer.is_valid(raise_exception=True)
        template = serializer.save()
        log_action(actor=request.user, action="SHIFT_TEMPLATE_UPDATED", instance=template, request=request)
        return success_response(ShiftTemplateSerializer(template).data, message="Shift template updated.")


@extend_schema(tags=["Admin · Staff operations"], summary="List or schedule non-overlapping staff shifts")
class ShiftAssignmentListCreateView(APIView):
    permission_classes = [HasCapability]; required_capability = "shift.manage"

    def get(self, request):
        queryset = _shift_queryset()
        for field in ("status", "department", "staff_id"):
            if value := request.query_params.get(field): queryset = queryset.filter(**{field: value})
        if start := request.query_params.get("starts_after"): queryset = queryset.filter(planned_start_at__gte=start)
        if end := request.query_params.get("starts_before"): queryset = queryset.filter(planned_start_at__lt=end)
        return _page(self, request, queryset.order_by("planned_start_at", "pk"), ShiftAssignmentListSerializer)

    def post(self, request):
        serializer = ShiftAssignmentCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        staff = User.objects.filter(pk=data["staff_id"]).first()
        if staff is None: raise NotFound("Staff user not found.")
        template = None
        if data.get("template_id") is not None:
            template = ShiftTemplate.objects.filter(pk=data["template_id"]).first()
            if template is None: raise NotFound("Shift template not found.")
        key = scoped_key(prefix="shift", scope=f"staff:{staff.pk}", raw_key=data["idempotency_key"])
        shift, created = create_shift_assignment(
            staff=staff, planned_start_at=data["planned_start_at"], planned_end_at=data["planned_end_at"], actor=request.user,
            template=template, department=data.get("department", ""), location=data.get("location", ""), notes=data.get("notes", ""), idempotency_key=key,
        )
        if created: log_action(actor=request.user, action="SHIFT_ASSIGNED", instance=shift, request=request, metadata={"reference": shift.reference, "staff_id": staff.pk})
        return success_response(ShiftAssignmentDetailSerializer(_shift_or_404(shift.reference, detail=True)).data,
                                message="Shift assigned." if created else "Existing shift assignment returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ShiftAssignmentDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "shift.manage"
    def get(self, request, reference):
        return success_response(ShiftAssignmentDetailSerializer(_shift_or_404(reference, detail=True)).data)


class ShiftAssignmentCancelView(APIView):
    permission_classes = [HasCapability]; required_capability = "shift.manage"
    def post(self, request, reference):
        serializer = ShiftCancelSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        shift = cancel_shift_assignment(shift=_shift_or_404(reference), actor=request.user, reason=serializer.validated_data.get("reason", ""))
        log_action(actor=request.user, action="SHIFT_CANCELLED", instance=shift, request=request, metadata={"reference": shift.reference})
        return success_response(ShiftAssignmentDetailSerializer(_shift_or_404(shift.reference, detail=True)).data, message="Shift cancelled.")


@extend_schema(tags=["Admin · Staff operations"], summary="List attendance records; workers see only their own records")
class AttendanceRecordListView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("attendance.clock", "attendance.manage"); require_any_capability = True

    def get(self, request):
        queryset = _attendance_queryset()
        if not has_capability(request.user, "attendance.manage"):
            queryset = queryset.filter(staff=request.user)
        elif staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if status_value := request.query_params.get("status"): queryset = queryset.filter(status=status_value)
        if business_date := request.query_params.get("business_date"): queryset = queryset.filter(business_date=business_date)
        return _page(self, request, queryset, AttendanceRecordListSerializer)


class AttendanceRecordDetailView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("attendance.clock", "attendance.manage"); require_any_capability = True
    def get(self, request, reference):
        return success_response(AttendanceRecordDetailSerializer(_attendance_or_404(request, reference, detail=True)).data)


@extend_schema(tags=["Admin · Staff operations"], summary="Record the caller's source-keyed server-timestamped attendance action")
class AttendanceClockView(APIView):
    permission_classes = [HasCapability]; required_capability = "attendance.clock"
    def post(self, request):
        serializer = AttendanceClockSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        probe = request.data.get("face_probe")
        manual_reason = str(request.data.get("manual_override_reason") or "")
        if face_service.face_verification_required() or probe is not None or manual_reason:
            source_key = face_service.clock_source_key(staff=request.user, action=data["action"], raw_key=data["idempotency_key"])
            outcome, passed = face_service.verify_for_clock(staff=request.user, actor=request.user, action=data["action"],
                                                            source_key=source_key, probe=probe, manual_reason=manual_reason)
            if not passed:
                log_action(actor=request.user, action="ATTENDANCE_FACE_REFUSED", request=request,
                           metadata={"action": data["action"], "outcome": outcome})
                raise FaceVerificationFailedError(detail=f"Facial verification did not pass ({outcome}); attendance was not recorded.")
        record, created = clock_attendance(staff=request.user, action=data["action"], actor=request.user,
                                           idempotency_key=data["idempotency_key"], shift_reference=data.get("shift_reference", ""))
        log_action(actor=request.user, action=f"ATTENDANCE_{data['action']}", instance=record, request=request, metadata={"reference": record.reference, "created": created})
        record = _attendance_or_404(request, record.reference, detail=True)
        return success_response(AttendanceRecordDetailSerializer(record).data,
                                message="Attendance action recorded." if created else "Existing attendance action returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=["Admin · Staff operations"], summary="List or request leave; ordinary workers see only their own requests")
class LeaveRequestListCreateView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("leave.request", "leave.approve"); require_any_capability = True

    def get(self, request):
        queryset = _leave_queryset()
        if not has_capability(request.user, "leave.approve"):
            queryset = queryset.filter(staff=request.user)
        elif staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if status_value := request.query_params.get("status"): queryset = queryset.filter(status=status_value)
        return _page(self, request, queryset, LeaveRequestListSerializer)

    def post(self, request):
        if not has_capability(request.user, "leave.request"):
            raise PermissionDenied("This user cannot request leave.")
        serializer = LeaveRequestCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        key = scoped_key(prefix="leave", scope=f"staff:{request.user.pk}", raw_key=data["idempotency_key"])
        leave_request, created = request_leave(staff=request.user, leave_type=data["type"], start_date=data["start_date"], end_date=data["end_date"],
                                                reason=data.get("reason", ""), idempotency_key=key, actor=request.user)
        if created: log_action(actor=request.user, action="LEAVE_REQUESTED", instance=leave_request, request=request, metadata={"reference": leave_request.reference})
        return success_response(LeaveRequestDetailSerializer(_leave_or_404(request, leave_request.reference, detail=True)).data,
                                message="Leave request submitted." if created else "Existing leave request returned.",
                                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class LeaveRequestDetailView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("leave.request", "leave.approve"); require_any_capability = True
    def get(self, request, reference):
        return success_response(LeaveRequestDetailSerializer(_leave_or_404(request, reference, detail=True)).data)


class LeaveReviewView(APIView):
    permission_classes = [HasCapability]; required_capability = "leave.approve"
    def post(self, request, reference):
        serializer = LeaveReviewSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        leave_request = review_leave(leave_request=_leave_or_404(request, reference), reviewer=request.user,
                                      approved=serializer.validated_data["approved"], review_note=serializer.validated_data.get("review_note", ""))
        log_action(actor=request.user, action="LEAVE_REVIEWED", instance=leave_request, request=request,
                   metadata={"reference": leave_request.reference, "status": leave_request.status})
        return success_response(LeaveRequestDetailSerializer(_leave_or_404(request, leave_request.reference, detail=True)).data,
                                message="Leave request approved." if leave_request.status == LeaveRequest.Status.APPROVED else "Leave request rejected.")


class LeaveCancelView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("leave.request", "leave.approve"); require_any_capability = True
    def post(self, request, reference):
        serializer = LeaveCancelSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        leave_request = cancel_leave(leave_request=_leave_or_404(request, reference), actor=request.user, note=serializer.validated_data.get("note", ""))
        log_action(actor=request.user, action="LEAVE_CANCELLED", instance=leave_request, request=request, metadata={"reference": leave_request.reference})
        return success_response(LeaveRequestDetailSerializer(_leave_or_404(request, leave_request.reference, detail=True)).data, message="Leave request cancelled.")
