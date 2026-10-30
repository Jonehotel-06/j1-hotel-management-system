"""Capability-scoped workforce operations API."""
from django.db.models import Prefetch, Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.audit.services import log_action
from apps.core.approval_audit import ApprovalCategory, approval_decision_metadata
from apps.core.pagination import StandardPagination
from apps.core.permissions import HasCapability
from apps.core.responses import success_response

from .models import (
    AttendanceEvent, AttendanceRecord, Department, LeaveRequest, LeaveRequestEvent,
    PayrollLine, PayrollPeriod, PayrollSalaryPayment, ShiftAssignment, ShiftAssignmentEvent,
    ShiftTemplate, StaffCompensation, StaffDisciplinaryRecord, StaffDocument, StaffPerformanceReview, StaffProfile,
)
from apps.core.exceptions import FaceVerificationFailedError
from .services import face_service, fingerprint_service

from .serializers import (
    AttendanceClockSerializer, AttendanceRecordDetailSerializer, AttendanceRecordListSerializer,
    DepartmentSerializer, LeaveCancelSerializer, LeaveRequestCreateSerializer, LeaveRequestDetailSerializer,
    LeaveRequestListSerializer, LeaveReviewSerializer, ShiftAssignmentCreateSerializer,
    ShiftAssignmentDetailSerializer, ShiftAssignmentListSerializer, ShiftCancelSerializer,
    ShiftTemplateSerializer, StaffDisciplinaryRecordSerializer, StaffDocumentSerializer,
    StaffPerformanceReviewSerializer, StaffProfileCreateSerializer, StaffProfileSerializer,
)
from .services.staff_operations_service import (
    cancel_leave, cancel_shift_assignment, clock_attendance, create_shift_assignment, ensure_staff_profile,
    request_leave, review_leave, scoped_key,
)


def _page(view, request, queryset, serializer):
    paginator = StandardPagination(); page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer(page, many=True, context={"request": request}).data)


def _profile_queryset():
    """Staff profile list/detail queryset.

    The list serializer reports each member's fingerprint enrolment state and
    their open attendance status. Asking the database once per row turned a
    40-member page into 82 queries (measured), so both collections are
    prefetched for the whole page in two fixed queries. The serializer keeps
    its per-row fallback for callers that load a profile without this
    queryset.
    """
    from .biometric_models import FingerprintTemplate

    return (
        StaffProfile.objects.select_related(
            "user", "department_ref", "reporting_manager", "default_shift_template", "assigned_workstation"
        )
        .prefetch_related(
            Prefetch(
                "user__fingerprint_templates",
                queryset=FingerprintTemplate.objects.only("staff_id", "status"),
                to_attr="_jone_fingerprint_templates",
            ),
            Prefetch(
                "user__attendance_records",
                queryset=AttendanceRecord.objects.filter(clock_out_at__isnull=True)
                .only("staff_id", "status", "clock_in_at")
                .order_by("-clock_in_at"),
                to_attr="_jone_open_attendance",
            ),
        )
        .order_by("user__last_name", "user__first_name", "pk")
    )


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
        from .biometric_models import FingerprintTemplate

        queryset = _profile_queryset()
        if status_value := request.query_params.get("employment_status"):
            queryset = queryset.filter(employment_status=status_value.upper())
        if emp_type := request.query_params.get("employment_type"):
            queryset = queryset.filter(employment_type=emp_type.upper())
        if department := request.query_params.get("department"):
            queryset = queryset.filter(
                Q(department__iexact=department)
                | Q(department_ref__code__iexact=department)
                | Q(department_ref__name__iexact=department)
                | Q(department_ref__slug__iexact=department)
            )
        if job_title := request.query_params.get("job_title"):
            queryset = queryset.filter(job_title__icontains=job_title)
        if role_val := request.query_params.get("role"):
            queryset = queryset.filter(user__role=role_val.upper())
        if att_status := request.query_params.get("attendance_status"):
            att_upper = att_status.upper()
            open_staff_ids = set(
                AttendanceRecord.objects.filter(clock_out_at__isnull=True).values_list("staff_id", flat=True)
            )
            if att_upper in ("ON_DUTY", "ON_BREAK"):
                matching_ids = AttendanceRecord.objects.filter(
                    clock_out_at__isnull=True, status=att_upper
                ).values_list("staff_id", flat=True)
                queryset = queryset.filter(user_id__in=matching_ids)
            elif att_upper == "NOT_CLOCKED_IN":
                queryset = queryset.exclude(user_id__in=open_staff_ids)
        if fp_status := request.query_params.get("fingerprint_status"):
            fp_upper = fp_status.upper()
            active_fp_ids = set(
                FingerprintTemplate.objects.filter(status=FingerprintTemplate.Status.ACTIVE).values_list("staff_id", flat=True)
            )
            pending_fp_ids = set(
                FingerprintTemplate.objects.filter(status=FingerprintTemplate.Status.PENDING).values_list("staff_id", flat=True)
            )
            revoked_fp_ids = set(
                FingerprintTemplate.objects.filter(status=FingerprintTemplate.Status.REVOKED).values_list("staff_id", flat=True)
            )
            if fp_upper == "ENROLLED":
                queryset = queryset.filter(user_id__in=active_fp_ids)
            elif fp_upper in ("PENDING_APPROVAL", "PENDING"):
                queryset = queryset.filter(user_id__in=pending_fp_ids - active_fp_ids)
            elif fp_upper == "REVOKED":
                queryset = queryset.filter(user_id__in=revoked_fp_ids - active_fp_ids - pending_fp_ids)
            elif fp_upper == "NOT_ENROLLED":
                queryset = queryset.exclude(user_id__in=active_fp_ids | pending_fp_ids | revoked_fp_ids)
        search = request.query_params.get("search") or request.query_params.get("q")
        if search:
            queryset = queryset.filter(
                Q(employee_code__icontains=search)
                | Q(user__email__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__middle_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(user__phone__icontains=search)
                | Q(job_title__icontains=search)
                | Q(department__icontains=search)
            )
        ordering = request.query_params.get("ordering")
        allowed_orderings = {
            "employee_code": ("employee_code",),
            "-employee_code": ("-employee_code",),
            "name": ("user__last_name", "user__first_name"),
            "-name": ("-user__last_name", "-user__first_name"),
            "department": ("department", "user__last_name"),
            "-department": ("-department", "user__last_name"),
            "employment_start": ("employment_start", "pk"),
            "-employment_start": ("-employment_start", "-pk"),
            "created_at": ("created_at", "pk"),
            "-created_at": ("-created_at", "-pk"),
        }
        if ordering in allowed_orderings:
            queryset = queryset.order_by(*allowed_orderings[ordering])
        return _page(self, request, queryset, StaffProfileSerializer)

    def post(self, request):
        import secrets
        from decimal import Decimal
        from django.db import transaction
        from django.utils import timezone
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from apps.accounts.models import Workstation
        from apps.core.emails import send_email_safe
        from apps.core.utils import hotel_today

        serializer = StaffProfileCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            created_user = False
            raw_pw = ""
            if data.get("user_id"):
                staff = User.objects.filter(pk=data["user_id"]).first()
                if staff is None:
                    raise NotFound("Staff user not found.")
            else:
                email = User.objects.normalize_email(data["email"])
                if User.objects.filter(email__iexact=email).exists():
                    raise DRFValidationError({"email": ["An account with this email already exists."]})
                role_val = (data.get("role") or User.Role.RECEPTIONIST).upper()
                if role_val not in User.Role.values or role_val == User.Role.GUEST:
                    raise DRFValidationError({"role": ["Select a valid staff role."]})
                from apps.accounts.departments import GLOBAL_ROLES
                if role_val in GLOBAL_ROLES and request.user.role != User.Role.ADMIN:
                    raise PermissionDenied("Only an Administrator can assign a global oversight role.")
                raw_pw = (data.get("password") or "").strip() or f"Jone#{secrets.token_hex(4)}!9"
                staff = User.objects.create_user(
                    email=email,
                    password=raw_pw,
                    first_name=data["first_name"],
                    middle_name=data.get("middle_name", ""),
                    last_name=data["last_name"],
                    phone=data.get("phone", ""),
                    role=role_val,
                    is_active=True,
                    is_staff=(role_val == User.Role.ADMIN),
                    must_change_password=bool(data.get("must_change_password", True)),
                )
                created_user = True

            profile, created = ensure_staff_profile(staff=staff)

            # Resolve foreign keys if supplied
            if data.get("department_ref"):
                dept_obj = Department.objects.filter(pk=data["department_ref"]).first()
                if dept_obj:
                    profile.department_ref = dept_obj
                    if not data.get("department"):
                        profile.department = dept_obj.name
            elif data.get("department"):
                dept_obj = Department.objects.filter(
                    Q(code__iexact=data["department"]) | Q(name__iexact=data["department"])
                ).first()
                if dept_obj:
                    profile.department_ref = dept_obj

            if "reporting_manager" in data:
                profile.reporting_manager = (
                    User.objects.filter(pk=data["reporting_manager"]).first() if data["reporting_manager"] else None
                )
            if "default_shift_template" in data:
                profile.default_shift_template = (
                    ShiftTemplate.objects.filter(pk=data["default_shift_template"]).first()
                    if data["default_shift_template"]
                    else None
                )
            if "assigned_workstation" in data:
                profile.assigned_workstation = (
                    Workstation.objects.filter(pk=data["assigned_workstation"]).first()
                    if data["assigned_workstation"]
                    else None
                )

            scalar_fields = (
                "department", "job_title", "employment_type", "employment_status", "employment_start",
                "probation_status", "probation_end_date", "work_location", "job_description",
                "date_of_birth", "gender", "residential_address",
                "emergency_contact_name", "emergency_contact_relationship",
                "emergency_contact_phone", "emergency_contact_address", "notes",
            )
            for field in scalar_fields:
                if field in data and data[field] is not None:
                    setattr(profile, field, data[field])

            if created_user and data.get("send_credentials_email") and raw_pw:
                sent = send_email_safe(
                    subject="J-One Hotel & Lodge — Welcome & Staff Portal Credentials",
                    message=(
                        f"Hello {staff.full_name},\n\n"
                        f"Your J-One Hotel & Lodge staff account ({staff.email}) has been registered.\n"
                        f"Employee Code: {profile.employee_code}\n"
                        f"Temporary Password: {raw_pw}\n\n"
                        f"Please change your password on first login."
                    ),
                    recipient_list=[staff.email],
                )
                profile.credential_delivery_status = "SENT" if sent else "PENDING_DELIVERY"
                if sent:
                    profile.credential_delivered_at = timezone.now()
            elif created_user:
                profile.credential_delivery_status = "PENDING_DELIVERY"

            profile.save()

            # Optional initial compensation setup if basic_salary was supplied and caller has payroll.manage
            if data.get("basic_salary") is not None and has_capability(request.user, "payroll.manage"):
                eff_date = data.get("employment_start") or hotel_today()
                StaffCompensation.objects.update_or_create(
                    staff=staff,
                    effective_from=eff_date,
                    defaults={
                        "currency": "NGN",
                        "basic_salary": data["basic_salary"],
                        "housing_allowance": data.get("housing_allowance") or Decimal("0.00"),
                        "transport_allowance": data.get("transport_allowance") or Decimal("0.00"),
                        "overtime_rate": data.get("overtime_rate") or Decimal("0.00"),
                        "created_by": request.user,
                    },
                )

        if created or created_user:
            log_action(
                actor=request.user,
                action="STAFF_PROFILE_CREATED",
                instance=profile,
                request=request,
                metadata={"employee_code": profile.employee_code, "user_id": staff.pk},
            )
        return success_response(
            StaffProfileSerializer(_profile_queryset().get(pk=profile.pk), context={"request": request}).data,
            message="Staff profile created." if (created or created_user) else "Existing staff profile returned.",
            status=status.HTTP_201_CREATED if (created or created_user) else status.HTTP_200_OK,
        )


class StaffProfileDetailView(APIView):
    permission_classes = [HasCapability]; required_capability = "staff.profile.manage"

    def get(self, request, pk):
        profile = _profile_queryset().filter(pk=pk).first()
        if profile is None:
            raise NotFound("Staff profile not found.")
        return success_response(StaffProfileSerializer(profile, context={"request": request}).data)

    def patch(self, request, pk):
        from django.db import transaction
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from apps.accounts.departments import (
            RetiredDepartmentalRole, assert_assignable_role, department_for_role,
            validate_role_department,
        )

        profile = _profile_queryset().filter(pk=pk).first()
        if profile is None:
            raise NotFound("Staff profile not found.")
        user = profile.user
        payload = request.data.copy()
        new_role = user.role
        role_changed = False
        if "role" in request.data:
            if request.user.role != User.Role.ADMIN:
                raise PermissionDenied("Only an Administrator can change a staff member's role.")
            try:
                new_role = assert_assignable_role(request.data.get("role"))
                if new_role not in User.Role.values or new_role == User.Role.GUEST:
                    raise ValueError("Select a valid staff role.")
                role_changed = new_role != user.role
                submitted_department = request.data.get("department", "")
                validate_role_department(new_role, submitted_department)
            except (RetiredDepartmentalRole, ValueError) as exc:
                raise DRFValidationError({"role": str(exc)}) from exc
            payload.pop("role", None)  # User.role is not a StaffProfile field.
            if role_changed and "department" not in payload:
                payload["department"] = department_for_role(new_role)
                if not payload["department"]:
                    payload["department"] = ""
                # A ref to the previous department would conflict with the new role.
                payload["department_ref"] = None
        elif "department" in payload or "department_ref" in payload:
            # Profile department changes cannot transfer an account by themselves.
            ref_value = payload.get("department_ref", "")
            if isinstance(ref_value, int):
                ref = Department.objects.filter(pk=ref_value).first()
                if ref is None:
                    raise DRFValidationError({"department_ref": "Choose an existing configured department."})
                ref_value = ref.code
            try:
                validate_role_department(new_role, payload.get("department", profile.department), department_ref=ref_value or "")
            except ValueError as exc:
                raise DRFValidationError({"department": str(exc)}) from exc

        serializer = StaffProfileSerializer(
            profile, data=payload, partial=True,
            context={"request": request, "assignment_role": new_role},
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            profile = serializer.save()
            user_updated = False
            for ufield in ("first_name", "middle_name", "last_name", "phone"):
                if ufield in request.data:
                    setattr(user, ufield, request.data.get(ufield) or "")
                    user_updated = True
            if role_changed:
                user.role = new_role
                user_updated = True
            if user_updated:
                user.save()
            if profile.department and not profile.department_ref_id:
                dept_obj = Department.objects.filter(
                    Q(code__iexact=profile.department) | Q(name__iexact=profile.department)
                ).first()
                if dept_obj:
                    profile.department_ref = dept_obj
                    profile.save(update_fields=["department_ref", "updated_at"])
        log_action(actor=request.user, action="STAFF_PROFILE_UPDATED", instance=profile, request=request)
        return success_response(
            StaffProfileSerializer(_profile_queryset().get(pk=profile.pk), context={"request": request}).data,
            message="Staff profile updated.",
        )


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
        # Item F: the personal clock controls ask for the caller's OPEN record
        # explicitly (open=true). The Clock in control is then disabled from the
        # server's own state — including a record opened at the terminal by a
        # biometric clock-in or an issued code, or one opened on an earlier day
        # that was never clocked out. Newest first, so one record is unambiguous.
        if str(request.query_params.get("open", "")).strip().lower() in ("1", "true", "yes"):
            queryset = queryset.filter(clock_out_at__isnull=True).order_by("-clock_in_at")
        return _page(self, request, queryset, AttendanceRecordListSerializer)


class AttendanceRecordDetailView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("attendance.clock", "attendance.manage"); require_any_capability = True
    def get(self, request, reference):
        return success_response(AttendanceRecordDetailSerializer(_attendance_or_404(request, reference, detail=True)).data)


def _assert_reception_workstation(request):
    """Attendance for operational staff is recorded AT the Receptionist Desktop.

    The desktop is identified on the server by one of its two enrolment routes
    — the key its browser was enrolled with, or its administrator-registered
    address (apps.accounts.desktop_policy) — and never by a client's own claim
    about user agent or screen size. A personal phone holding a normal staff
    session therefore still cannot clock itself, or anyone else, in or out.
    Management roles keep their existing device freedom, exactly as they do for
    sign-in.
    """
    from apps.accounts.desktop_policy import (
        front_desk_desktop_ip_allowed,
        receptionist_desktop_required,
        resolve_receptionist_desktop,
    )

    if not receptionist_desktop_required(request.user):
        return None
    desktop, _method = resolve_receptionist_desktop(request)
    if desktop is not None:
        if not front_desk_desktop_ip_allowed(request):
            raise PermissionDenied(
                "Attendance is recorded only from the configured Front Desk desktop IP address. "
                "Ask an administrator to update Settings if the Front Desk desktop has changed."
            )
        return None
    raise PermissionDenied(
        "Attendance is recorded at the Receptionist Desktop. Ask the front desk to clock you "
        "in and give you your 6-digit workstation access code."
    )


@extend_schema(tags=["Admin · Staff operations"], summary="Record the caller's source-keyed server-timestamped attendance action")
class AttendanceClockView(APIView):
    permission_classes = [HasCapability]; required_capability = "attendance.clock"
    def post(self, request):
        _assert_reception_workstation(request)
        serializer = AttendanceClockSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = serializer.validated_data
        probe = request.data.get("face_probe")
        fingerprint_probe = request.data.get("fingerprint_probe")
        manual_reason = str(request.data.get("manual_override_reason") or "")
        
        # 1. Fingerprint verification check
        if fingerprint_service.fingerprint_verification_required() or fingerprint_probe is not None:
            fp_key = fingerprint_service.biometric_clock_source_key(staff=request.user, action=data["action"], raw_key=data["idempotency_key"])
            fp_outcome, fp_passed = fingerprint_service.verify_fingerprint_for_clock(
                staff=request.user, actor=request.user, action=data["action"],
                source_key=fp_key, probe=fingerprint_probe, manual_reason=manual_reason
            )
            if not fp_passed:
                log_action(actor=request.user, action="ATTENDANCE_FINGERPRINT_REFUSED", request=request,
                           metadata={"action": data["action"], "outcome": fp_outcome})
                raise FaceVerificationFailedError(detail=f"Fingerprint biometric verification did not pass ({fp_outcome}); attendance was not recorded.")

        # 2. Facial verification check (if configured/probed)
        elif face_service.face_verification_required() or probe is not None or manual_reason:
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
        before = _leave_or_404(request, reference)
        leave_request = review_leave(leave_request=before, reviewer=request.user,
                                      approved=serializer.validated_data["approved"], review_note=serializer.validated_data.get("review_note", ""))
        metadata = {"reference": leave_request.reference, "status": leave_request.status}
        metadata.update(approval_decision_metadata(
            category=ApprovalCategory.LEAVE, reference=leave_request.reference,
            initiated_by=before.staff, decided_by=request.user,
            previous_status=before.status, new_status=leave_request.status,
            note=leave_request.review_note,
        ))
        log_action(actor=request.user, action="LEAVE_REVIEWED", instance=leave_request, request=request, metadata=metadata)
        return success_response(LeaveRequestDetailSerializer(_leave_or_404(request, leave_request.reference, detail=True)).data,
                                message="Leave request approved." if leave_request.status == LeaveRequest.Status.APPROVED else "Leave request rejected.")


class LeaveCancelView(APIView):
    permission_classes = [HasCapability]; required_capabilities = ("leave.request", "leave.approve"); require_any_capability = True
    def post(self, request, reference):
        serializer = LeaveCancelSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        leave_request = cancel_leave(leave_request=_leave_or_404(request, reference), actor=request.user, note=serializer.validated_data.get("note", ""))
        log_action(actor=request.user, action="LEAVE_CANCELLED", instance=leave_request, request=request, metadata={"reference": leave_request.reference})
        return success_response(LeaveRequestDetailSerializer(_leave_or_404(request, leave_request.reference, detail=True)).data, message="Leave request cancelled.")


class DepartmentListCreateView(APIView):
    """List configurable hotel departments or register a new department."""
    permission_classes = [HasCapability]
    required_capabilities = (
        "staff.view", "staff.profile.manage", "shift.manage", "attendance.manage", "attendance.clock",
    )
    require_any_capability = True

    def get(self, request):
        # Historical standalone rows (Cashier, Bar, Maintenance, HR) remain
        # archived for FK history, never appearing in the active department list.
        queryset = Department.objects.select_related("head").exclude(
            Q(code__iexact="CASHIER") | Q(code__iexact="CASHIER_TILL")
            | Q(name__iexact="Cashier") | Q(name__iexact="Cashier / till")
            | Q(slug__iexact="cashier") | Q(slug__iexact="cashier-till")
            | Q(code__iexact="BAR") | Q(code__iexact="BAR_SERVICE")
            | Q(code__iexact="MAINTENANCE")
            | Q(code__iexact="HR") | Q(code__iexact="HUMAN_RESOURCES")
        )
        if request.query_params.get("include_archived") != "true":
            queryset = queryset.filter(is_active=True)
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(code__icontains=search) | Q(name__icontains=search) | Q(slug__icontains=search)
            )
        return success_response(DepartmentSerializer(queryset, many=True, context={"request": request}).data)

    def post(self, request):
        if not has_capability(request.user, "staff.profile.manage") and request.user.role != User.Role.ADMIN:
            raise PermissionDenied("Creating or configuring departments requires staff profile management authority.")
        serializer = DepartmentSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        dept = serializer.save()
        log_action(actor=request.user, action="DEPARTMENT_CREATED", instance=dept, request=request)
        return success_response(
            DepartmentSerializer(dept, context={"request": request}).data,
            message="Department created.",
            status=status.HTTP_201_CREATED,
        )


class DepartmentDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "staff.profile.manage"

    def get(self, request, pk: int):
        dept = Department.objects.select_related("head").filter(pk=pk).first()
        if dept is None:
            raise NotFound("Department not found.")
        return success_response(DepartmentSerializer(dept, context={"request": request}).data)

    def patch(self, request, pk: int):
        dept = Department.objects.select_related("head").filter(pk=pk).first()
        if dept is None:
            raise NotFound("Department not found.")
        serializer = DepartmentSerializer(dept, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        dept = serializer.save()
        log_action(actor=request.user, action="DEPARTMENT_UPDATED", instance=dept, request=request)
        return success_response(
            DepartmentSerializer(dept, context={"request": request}).data,
            message="Department updated.",
        )


class DepartmentDashboardSummaryView(APIView):
    """Return live, database-backed operational KPIs and queues for a specific department dashboard."""
    permission_classes = [HasCapability]
    required_capabilities = (
        "attendance.clock", "attendance.manage", "staff.view", "staff.profile.manage",
        "booking.read", "pos.order.manage", "kitchen.queue.view", "housekeeping.task.manage",
        "guest_request.manage", "procurement.manage",
        "reports.financial.view", "payment.read", "inventory.manage",
    )
    require_any_capability = True

    def get(self, request, code_or_slug=None):
        from decimal import Decimal
        from django.db.models import Sum
        from django.utils import timezone
        from .services.fingerprint_service import is_staff_authorized_for_department, staff_department_code

        target_dept = (
            code_or_slug
            or request.query_params.get("department")
            or request.headers.get("X-JONE-Department")
            or staff_department_code(request.user)
        ).strip().upper()

        from apps.accounts.departments import (
            _normalize_department_code,
            is_removed_cashier_department,
            removed_department_message,
        )
        if is_removed_cashier_department(target_dept):
            raise NotFound("The Cashier department was merged into Restaurants.")
        if consolidation_msg := removed_department_message(target_dept):
            raise NotFound(consolidation_msg)

        if not is_staff_authorized_for_department(request.user, target_dept):
            raise PermissionDenied("Your account is not authorized to access this department dashboard.")

        dept_obj = Department.objects.filter(
            Q(code__iexact=target_dept) | Q(slug__iexact=target_dept) | Q(name__iexact=target_dept),
            is_active=True,
        ).first()
        canonical_code = dept_obj.code if dept_obj else target_dept
        ws_code = _normalize_department_code(canonical_code)

        today = timezone.localdate()
        my_att = (
            AttendanceRecord.objects.filter(staff=request.user, clock_out_at__isnull=True)
            .order_by("-clock_in_at")
            .first()
        )
        if dept_obj:
            staff_filter = Q(department_ref=dept_obj) | Q(department__iexact=dept_obj.code) | Q(department__iexact=dept_obj.name)
            if ws_code:
                staff_filter |= Q(department__iexact=ws_code)
            dept_staff_qs = StaffProfile.objects.select_related("user").filter(staff_filter)
        else:
            dept_staff_qs = StaffProfile.objects.select_related("user").filter(Q(department__icontains=canonical_code))
        on_duty_count = AttendanceRecord.objects.filter(
            clock_out_at__isnull=True,
            staff_id__in=dept_staff_qs.values_list("user_id", flat=True),
        ).count()

        metrics = {}
        if canonical_code in ("FRONT_DESK", "FRONTDESK", "RECEPTIONIST"):
            from apps.bookings.models import Booking
            from apps.rooms.models import Room

            metrics = {
                "arrivals_today": Booking.objects.filter(check_in=today, status="CONFIRMED").count(),
                "departures_today": Booking.objects.filter(check_out=today, status="CHECKED_IN").count(),
                "in_house_guests": Booking.objects.filter(status="CHECKED_IN").count(),
                "available_rooms": Room.objects.filter(is_active=True, status="AVAILABLE").count(),
            }
        elif canonical_code in ("KITCHEN", "CHEFS", "KITCHEN_ASSISTANT"):
            from apps.pos.models import KitchenTicket

            metrics = {
                "pending_tickets": KitchenTicket.objects.filter(status__in=["NEW", "PREPARING"]).count(),
                "ready_tickets": KitchenTicket.objects.filter(status="READY").count(),
                "completed_today": KitchenTicket.objects.filter(status="SERVED", updated_at__date=today).count(),
            }
        elif canonical_code in ("RESTAURANT_BAR", "RESTAURANT", "WAITERS_WAITRESSES"):
            from apps.pos.models import PosOrder, RestaurantTableSession

            metrics = {
                "open_orders": PosOrder.objects.filter(status__in=["OPEN", "SUBMITTED", "PREPARING", "READY"]).count(),
                "active_tables": RestaurantTableSession.objects.filter(status="OPEN").count(),
                "paid_orders_today": PosOrder.objects.filter(status="PAID", updated_at__date=today).count(),
            }
        elif canonical_code in ("HOUSEKEEPING", "HOUSEKEEPERS"):
            from apps.housekeeping.models import HousekeepingTask
            from apps.maintenance.models import MaintenanceWorkOrder
            from apps.rooms.models import Room

            metrics = {
                "dirty_rooms": Room.objects.filter(is_active=True, status__in=["CLEANING", "MAINTENANCE"]).count(),
                "open_tasks": HousekeepingTask.objects.filter(status__in=["OPEN", "ASSIGNED", "IN_PROGRESS"]).count(),
                "completed_tasks_today": HousekeepingTask.objects.filter(status="COMPLETED", updated_at__date=today).count(),
                "open_maintenance": MaintenanceWorkOrder.objects.filter(status__in=["OPEN", "ASSIGNED", "IN_PROGRESS"]).count(),
            }
        elif canonical_code in ("PORTERING", "PORTER", "PORTERS", "SECURITY"):
            from apps.guest_services.models import ServiceRequest

            open_cnt = ServiceRequest.objects.filter(status__in=["OPEN", "ASSIGNED", "IN_PROGRESS"]).count()
            metrics = {
                "open_guest_requests": open_cnt,
                "pending_requests": open_cnt,
                "my_assigned_requests": ServiceRequest.objects.filter(
                    assigned_to=request.user, status__in=["ASSIGNED", "IN_PROGRESS"]
                ).count(),
                "completed_today": ServiceRequest.objects.filter(status="COMPLETED", updated_at__date=today).count(),
            }
        elif canonical_code in ("STORES_INVENTORY", "STORE", "INVENTORY", "STORE_KEEPER"):
            from apps.inventory.models import StockConsumptionRequest, StockItem

            metrics = {
                "active_items": StockItem.objects.filter(is_active=True).count(),
                "low_stock_items": StockItem.objects.filter(is_active=True, reorder_level__gt=0).count(),
                "pending_pos_handoffs": StockConsumptionRequest.objects.filter(status="PENDING").count(),
            }
        elif canonical_code in ("ADMIN_PROCUREMENT", "PROCUREMENT", "ADMIN"):
            from apps.inventory.models import PurchaseOrder, Supplier

            metrics = {
                "active_suppliers": Supplier.objects.filter(status=Supplier.Status.ACTIVE).count(),
                "open_purchase_orders": PurchaseOrder.objects.filter(status__in=["DRAFT", "SUBMITTED", "APPROVED"]).count(),
                "total_staff_accounts": User.objects.exclude(role=User.Role.GUEST).filter(is_active=True).count(),
            }
        elif canonical_code in ("ACCOUNTS", "ACCOUNTANT"):
            from apps.finance.models import Expense
            from apps.payments.models import Payment

            total_rev = (
                Payment.objects.filter(status="SUCCESS").aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )
            metrics = {
                "total_revenue": str(total_rev),
                "verified_payments_today": Payment.objects.filter(status="SUCCESS", paid_at__date=today).count(),
                "pending_expenses": Expense.objects.filter(status__in=["SUBMITTED", "PENDING_APPROVAL"]).count(),
                "payroll_periods_count": PayrollPeriod.objects.count(),
            }
        else:  # MANAGEMENT
            from apps.bookings.models import Booking
            from apps.rooms.models import Room

            metrics = {
                "active_staff": User.objects.exclude(role=User.Role.GUEST).filter(is_active=True).count(),
                "in_house_bookings": Booking.objects.filter(status="CHECKED_IN").count(),
                "available_rooms": Room.objects.filter(is_active=True, status="AVAILABLE").count(),
                "pending_leave_requests": LeaveRequest.objects.filter(status="PENDING").count(),
            }

        return success_response({
            "department_code": canonical_code,
            "department": DepartmentSerializer(dept_obj, context={"request": request}).data if dept_obj else {
                "code": canonical_code,
                "name": canonical_code.replace("_", " ").title(),
            },
            "my_attendance": {
                "status": my_att.status if my_att else "NOT_CLOCKED_IN",
                "reference": my_att.reference if my_att else None,
                "clock_in_at": my_att.clock_in_at if my_att else None,
            },
            "active_staff_count": dept_staff_qs.filter(employment_status=StaffProfile.EmploymentStatus.ACTIVE).count(),
            "on_duty_count": on_duty_count,
            "department_on_duty_count": on_duty_count,
            "metrics": metrics,
        })


class StaffComprehensiveProfileView(APIView):
    """Dedicated 10-Tab Staff Profile API returning unified personal, employment, biometric, salary, disciplinary, document, and audit records."""
    permission_classes = [HasCapability]
    required_capabilities = ("staff.view", "staff.profile.manage")
    require_any_capability = True

    def get(self, request, user_id: int):
        from decimal import Decimal
        from apps.accounts.capabilities import capability_codes_for_user
        from apps.accounts.models import RoleCapability, UserCapabilityOverride
        from apps.accounts.serializers import UserSerializer
        from apps.audit.models import AuditLog

        staff = (
            User.objects.select_related("staff_profile", "staff_profile__department_ref", "staff_profile__reporting_manager")
            .filter(pk=user_id)
            .first()
        )
        if staff is None:
            raise NotFound("Staff member not found.")

        profile, _ = ensure_staff_profile(staff=staff) if staff.role != User.Role.GUEST else (None, False)
        profile_data = StaffProfileSerializer(profile, context={"request": request}).data if profile else {}
        biometric_data = fingerprint_service.get_staff_biometric_status(staff)

        # Attendance & Shifts
        recent_attendance = AttendanceRecordListSerializer(
            AttendanceRecord.objects.select_related("staff", "shift").filter(staff=staff).order_by("-clock_in_at")[:30],
            many=True,
        ).data
        recent_shifts = ShiftAssignmentListSerializer(
            ShiftAssignment.objects.select_related("staff", "template", "assigned_by")
            .filter(staff=staff)
            .order_by("-planned_start_at")[:30],
            many=True,
        ).data
        leave_requests = LeaveRequestListSerializer(
            LeaveRequest.objects.select_related("staff", "reviewer").filter(staff=staff).order_by("-created_at")[:30],
            many=True,
        ).data

        # Salary & Payment Records (permission-gated)
        can_view_salary = (
            request.user.role == User.Role.ADMIN
            or has_capability(request.user, "payroll.view")
            or has_capability(request.user, "payroll.manage")
            or has_capability(request.user, "payroll.approve")
        )
        salary_section = {"authorized": can_view_salary}
        if can_view_salary:
            from .services.payroll_service import _effective_salary_paid

            comp_rows = list(
                StaffCompensation.objects.select_related("created_by")
                .filter(staff=staff)
                .order_by("-effective_from", "-id")
            )
            current_comp = comp_rows[0] if comp_rows else None
            payroll_lines = list(
                PayrollLine.objects.select_related("period", "period__created_by", "period__reviewed_by")
                .prefetch_related("salary_payments")
                .filter(staff=staff)
                .order_by("-period__ends_on", "-id")[:24]
            )
            salary_payments = list(
                PayrollSalaryPayment.objects.select_related("line", "line__period", "recorded_by")
                .filter(line__staff=staff)
                .order_by("-payment_date", "-id")[:50]
            )
            total_net_payable = sum((l.net_pay for l in payroll_lines), Decimal("0.00"))
            total_paid = sum((_effective_salary_paid(line=l) for l in payroll_lines), Decimal("0.00"))
            salary_section.update({
                "current_compensation": {
                    "id": current_comp.pk,
                    "currency": current_comp.currency,
                    "basic_salary": str(current_comp.basic_salary),
                    "housing_allowance": str(current_comp.housing_allowance),
                    "transport_allowance": str(current_comp.transport_allowance),
                    "overtime_rate": str(current_comp.overtime_rate),
                    "effective_from": current_comp.effective_from,
                    "notes": current_comp.notes,
                } if current_comp else None,
                "compensation_history": [
                    {
                        "id": c.pk,
                        "currency": c.currency,
                        "basic_salary": str(c.basic_salary),
                        "housing_allowance": str(c.housing_allowance),
                        "transport_allowance": str(c.transport_allowance),
                        "overtime_rate": str(c.overtime_rate),
                        "effective_from": c.effective_from,
                        "created_by_name": c.created_by.full_name if c.created_by else "",
                        "notes": c.notes,
                        "created_at": c.created_at,
                    }
                    for c in comp_rows
                ],
                "payroll_history": [
                    {
                        "line_id": l.pk,
                        "period_reference": l.period.reference,
                        "period_status": l.period.status,
                        "starts_on": l.period.starts_on,
                        "ends_on": l.period.ends_on,
                        "currency": l.currency,
                        "basic_pay": str(l.basic_pay),
                        "housing_pay": str(l.housing_pay),
                        "transport_pay": str(l.transport_pay),
                        "overtime_hours": str(l.overtime_hours),
                        "overtime_pay": str(l.overtime_pay),
                        "allowances": l.allowances,
                        "bonus": str(l.bonus),
                        "gross_pay": str(l.gross_pay),
                        "deductions": l.deductions,
                        "deductions_total": str(l.deductions_total),
                        "net_pay": str(l.net_pay),
                        "amount_paid": str(_effective_salary_paid(line=l)),
                        "balance_due": str(max(l.net_pay - _effective_salary_paid(line=l), Decimal("0.00"))),
                        "payment_status": "PAID" if _effective_salary_paid(line=l) >= l.net_pay and l.net_pay > 0 else (
                            "PARTIALLY_PAID" if _effective_salary_paid(line=l) > 0 else "UNPAID"
                        ),
                    }
                    for l in payroll_lines
                ],
                "payment_records": [
                    {
                        "reference": p.reference,
                        "period_reference": p.line.period.reference,
                        "amount": str(p.amount),
                        "currency": p.currency,
                        "payment_method": p.method,
                        "paid_on": p.payment_date,
                        "external_reference": p.external_reference,
                        "status": "REVERSAL" if p.reversal_of_id else "POSTED",
                        "recorded_by_name": p.recorded_by.full_name if p.recorded_by else "",
                        "notes": p.notes,
                        "created_at": p.created_at,
                    }
                    for p in salary_payments
                ],
                "summary": {
                    "total_net_payable": str(total_net_payable),
                    "total_paid": str(total_paid),
                    "outstanding_balance": str(max(total_net_payable - total_paid, Decimal("0.00"))),
                },
            })

        # Offenses & Disciplinary Records
        disciplinary_records = StaffDisciplinaryRecordSerializer(
            StaffDisciplinaryRecord.objects.select_related(
                "staff", "reported_by", "reviewed_by", "deduction_approved_by", "appeal_decided_by"
            ).filter(staff=staff)[:50],
            many=True,
            context={"request": request},
        ).data

        performance_reviews = StaffPerformanceReviewSerializer(
            StaffPerformanceReview.objects.select_related("staff", "reviewer").filter(staff=staff)[:50],
            many=True,
            context={"request": request},
        ).data

        # Documents
        documents = StaffDocumentSerializer(
            StaffDocument.objects.select_related("staff", "uploaded_by")
            .filter(staff=staff, is_active=True)[:50],
            many=True,
            context={"request": request},
        ).data

        # Account & Permissions
        effective_caps = sorted(capability_codes_for_user(staff))
        role_caps = sorted(
            RoleCapability.objects.filter(role=staff.role, capability__is_active=True)
            .values_list("capability__code", flat=True)
        )
        user_overrides = [
            {
                "id": ov.pk,
                "capability_code": ov.capability.code,
                "capability_name": ov.capability.name,
                "effect": ov.effect,
                "reason": ov.reason,
                "updated_at": ov.updated_at,
            }
            for ov in UserCapabilityOverride.objects.select_related("capability").filter(user=staff)
        ]

        # Activity & Audit History
        can_view_audit = request.user.role == User.Role.ADMIN or has_capability(request.user, "staff.profile.manage")
        audit_items = []
        if can_view_audit:
            logs = (
                AuditLog.objects.select_related("actor")
                .filter(Q(actor=staff) | Q(object_id=str(staff.pk), object_type__icontains="user"))
                .order_by("-created_at")[:40]
            )
            audit_items = [
                {
                    "id": item.pk,
                    "action": item.action,
                    "summary": item.summary,
                    "actor_email": item.actor.email if item.actor else "",
                    "actor_name": item.actor.full_name if item.actor else "System",
                    "model_name": item.object_type,
                    "object_repr": item.summary or f"{item.object_type} #{item.object_id}",
                    "changes": item.changes,
                    "metadata": item.metadata,
                    "ip_address": item.ip_address,
                    "created_at": item.created_at,
                }
                for item in logs
            ]

        return success_response({
            "user": UserSerializer(staff, context={"request": request}).data,
            "profile": profile_data,
            "overview": {
                "full_name": staff.full_name,
                "employee_code": profile.employee_code if profile else "",
                "department": profile_data.get("department_name") or profile_data.get("department") or "",
                "job_title": profile.job_title if profile else "",
                "role": staff.role,
                "role_label": staff.get_role_display(),
                "employment_status": profile.employment_status if profile else ("ACTIVE" if staff.is_active else "INACTIVE"),
                "employment_type": profile.employment_type if profile else "FULL_TIME",
                "account_active": staff.is_active,
                "fingerprint_status": biometric_data["enrollment_status"],
                "attendance_status": biometric_data["attendance_status"],
                "has_active_access_code": biometric_data["has_active_access_code"],
                "open_disciplinary_count": sum(1 for d in disciplinary_records if d["status"] in ("OPEN", "UNDER_REVIEW")),
                "documents_count": len(documents),
            },
            "biometrics_attendance": {
                "biometrics": biometric_data,
                "recent_attendance": recent_attendance,
                "recent_shifts": recent_shifts,
            },
            "salary_payments": salary_section,
            "disciplinary": disciplinary_records,
            "performance_reviews": performance_reviews,
            "leave_shifts": {
                "leave_requests": leave_requests,
                "shifts": recent_shifts,
            },
            "documents": documents,
            "account_permissions": {
                "email": staff.email,
                "employee_code": profile.employee_code if profile else "",
                "role": staff.role,
                "role_label": staff.get_role_display(),
                "is_active": staff.is_active,
                "must_change_password": staff.must_change_password,
                "credential_delivery_status": profile.credential_delivery_status if profile else "NOT_SENT",
                "credential_delivered_at": profile.credential_delivered_at if profile else None,
                "last_login": staff.last_login,
                "date_joined": staff.date_joined,
                "effective_capabilities": effective_caps,
                "role_capabilities": role_caps,
                "user_overrides": user_overrides,
            },
            "activity_audit": audit_items,
        })

    def patch(self, request, user_id: int):
        if not has_capability(request.user, "staff.profile.manage") and request.user.role != User.Role.ADMIN:
            raise PermissionDenied("Updating staff profile records requires staff profile management authority.")
        staff = User.objects.filter(pk=user_id).first()
        if staff is None:
            raise NotFound("Staff member not found.")
        profile, _ = ensure_staff_profile(staff=staff)
        detail_view = StaffProfileDetailView()
        detail_view.request = request
        detail_view.patch(request, pk=profile.pk)
        return self.get(request, user_id=user_id)


class StaffDisciplinaryRecordListCreateView(APIView):
    """List or record employee offenses, investigations, and disciplinary actions."""
    permission_classes = [HasCapability]
    required_capabilities = ("staff.view", "staff.profile.manage", "payroll.manage")
    require_any_capability = True

    def get(self, request):
        queryset = StaffDisciplinaryRecord.objects.select_related(
            "staff", "staff__staff_profile", "reported_by", "reviewed_by", "deduction_approved_by"
        ).all()
        if staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if status_val := request.query_params.get("status"):
            queryset = queryset.filter(status=status_val.upper())
        if category := request.query_params.get("category"):
            queryset = queryset.filter(category=category.upper())
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(allegation_summary__icontains=search)
                | Q(staff__first_name__icontains=search)
                | Q(staff__last_name__icontains=search)
                | Q(staff__email__icontains=search)
            )
        return _page(self, request, queryset, StaffDisciplinaryRecordSerializer)

    def post(self, request):
        import secrets
        from django.utils import timezone

        if not has_capability(request.user, "staff.profile.manage") and request.user.role != User.Role.ADMIN:
            raise PermissionDenied("Recording disciplinary cases requires staff management authority.")

        serializer = StaffDisciplinaryRecordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        now = timezone.now()
        ref = f"DSC-{now.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        staff_obj = serializer.validated_data["staff"]
        profile = getattr(staff_obj, "staff_profile", None)
        dept = serializer.validated_data.get("department") or (profile.department if profile else "")

        # Open allegation never automatically deducts salary!
        record = serializer.save(
            reference=ref,
            department=dept,
            reported_by=request.user,
            status=StaffDisciplinaryRecord.Status.OPEN,
            deduction_approved=False,
            approved_deduction_amount=0,
            audit_trail=[
                {
                    "event": "REPORTED",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "timestamp": now.isoformat(),
                    "summary": serializer.validated_data.get("allegation_summary", ""),
                }
            ],
        )
        log_action(
            actor=request.user,
            action="STAFF_DISCIPLINARY_REPORTED",
            instance=record,
            request=request,
            metadata={"reference": record.reference, "staff_id": record.staff_id, "category": record.category},
        )
        return success_response(
            StaffDisciplinaryRecordSerializer(record, context={"request": request}).data,
            message="Disciplinary allegation recorded. Salary is unaffected unless a deduction is formally approved after review.",
            status=status.HTTP_201_CREATED,
        )


class StaffDisciplinaryRecordDetailView(APIView):
    permission_classes = [HasCapability]
    required_capabilities = ("staff.view", "staff.profile.manage", "payroll.manage")
    require_any_capability = True

    def get(self, request, reference: str):
        record = StaffDisciplinaryRecord.objects.select_related(
            "staff", "reported_by", "reviewed_by", "deduction_approved_by", "appeal_decided_by"
        ).filter(reference=reference).first()
        if record is None:
            raise NotFound("Disciplinary record not found.")
        return success_response(StaffDisciplinaryRecordSerializer(record, context={"request": request}).data)


class StaffDisciplinaryActionView(APIView):
    """Execute controlled disciplinary workflow transitions: response, review, resolve, approve_deduction, appeal."""
    permission_classes = [HasCapability]
    required_capabilities = ("staff.profile.manage", "payroll.approve", "payroll.manage")
    require_any_capability = True

    def post(self, request, reference: str):
        from decimal import Decimal
        from django.db import transaction
        from django.utils import timezone
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from apps.core.approvals import require_independent_approver
        from apps.core.utils import hotel_today

        action = (request.data.get("action") or "").strip().lower()
        now = timezone.now()

        with transaction.atomic():
            record = (
                StaffDisciplinaryRecord.objects.select_for_update()
                .select_related("staff")
                .filter(reference=reference)
                .first()
            )
            if record is None:
                raise NotFound("Disciplinary record not found.")

            trail = list(record.audit_trail or [])

            if action == "submit_response":
                record.employee_response = str(request.data.get("employee_response") or "").strip()
                record.employee_responded_at = now
                if record.status == StaffDisciplinaryRecord.Status.OPEN:
                    record.status = StaffDisciplinaryRecord.Status.UNDER_REVIEW
                trail.append({
                    "event": "EMPLOYEE_RESPONSE_RECORDED",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "timestamp": now.isoformat(),
                })
                record.audit_trail = trail
                record.save()

            elif action in ("review", "resolve"):
                record.status = StaffDisciplinaryRecord.Status.RESOLVED
                record.reviewed_by = request.user
                record.reviewed_at = now
                record.investigation_outcome = str(request.data.get("investigation_outcome") or record.investigation_outcome)
                if "disciplinary_action" in request.data:
                    record.disciplinary_action = str(request.data["disciplinary_action"]).upper()
                if "warning_level" in request.data:
                    record.warning_level = str(request.data["warning_level"]).upper()
                if "recommended_deduction_amount" in request.data and request.data["recommended_deduction_amount"] not in (None, ""):
                    record.recommended_deduction_amount = Decimal(str(request.data["recommended_deduction_amount"]))
                trail.append({
                    "event": "INVESTIGATION_RESOLVED",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "disciplinary_action": record.disciplinary_action,
                    "warning_level": record.warning_level,
                    "timestamp": now.isoformat(),
                })
                record.audit_trail = trail
                record.save()

            elif action == "dismiss":
                record.status = StaffDisciplinaryRecord.Status.DISMISSED
                record.disciplinary_action = StaffDisciplinaryRecord.ActionType.EXONERATED
                record.deduction_approved = False
                record.approved_deduction_amount = Decimal("0.00")
                record.reviewed_by = request.user
                record.reviewed_at = now
                record.investigation_outcome = str(request.data.get("investigation_outcome") or "Dismissed / exonerated")
                trail.append({
                    "event": "DISMISSED",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "timestamp": now.isoformat(),
                })
                record.audit_trail = trail
                record.save()

            elif action == "approve_deduction":
                if not (has_capability(request.user, "payroll.approve") or request.user.role == User.Role.ADMIN):
                    raise PermissionDenied("Approving a monetary salary deduction requires payroll approval authority.")
                if record.reported_by_id:
                    require_independent_approver(
                        requester=record.reported_by,
                        actor=request.user,
                        workflow="disciplinary salary deduction approval",
                    )
                raw_amt = request.data.get("approved_deduction_amount", record.recommended_deduction_amount)
                amt = Decimal(str(raw_amt or "0.00"))
                if amt <= Decimal("0.00"):
                    raise DRFValidationError({"approved_deduction_amount": "Approved deduction amount must be greater than zero."})
                record.approved_deduction_amount = amt
                record.deduction_approved = True
                record.deduction_approved_by = request.user
                record.deduction_approved_at = now
                record.effective_date = request.data.get("effective_date") or record.effective_date or hotel_today()
                record.status = StaffDisciplinaryRecord.Status.RESOLVED
                record.disciplinary_action = StaffDisciplinaryRecord.ActionType.MONETARY_DEDUCTION
                trail.append({
                    "event": "DEDUCTION_APPROVED",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "approved_amount": str(amt),
                    "effective_date": str(record.effective_date),
                    "timestamp": now.isoformat(),
                })
                record.audit_trail = trail
                record.save()

            elif action == "submit_appeal":
                record.appeal_status = StaffDisciplinaryRecord.AppealStatus.SUBMITTED
                record.appeal_notes = str(request.data.get("appeal_notes") or "").strip()
                record.appealed_at = now
                record.status = StaffDisciplinaryRecord.Status.APPEALED
                trail.append({
                    "event": "APPEAL_SUBMITTED",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "timestamp": now.isoformat(),
                })
                record.audit_trail = trail
                record.save()

            elif action == "decide_appeal":
                decision = str(request.data.get("appeal_status") or "UPHELD").upper()
                if decision not in ("UPHELD", "OVERTURNED"):
                    raise DRFValidationError({"appeal_status": "Decision must be UPHELD or OVERTURNED."})
                record.appeal_status = decision
                record.appeal_decided_by = request.user
                record.appeal_decided_at = now
                if "appeal_notes" in request.data:
                    record.appeal_notes = str(request.data["appeal_notes"])
                if decision == "OVERTURNED":
                    record.status = StaffDisciplinaryRecord.Status.DISMISSED
                    record.deduction_approved = False
                    record.approved_deduction_amount = Decimal("0.00")
                else:
                    record.status = StaffDisciplinaryRecord.Status.CLOSED
                trail.append({
                    "event": f"APPEAL_{decision}",
                    "actor_id": request.user.pk,
                    "actor_name": request.user.full_name,
                    "timestamp": now.isoformat(),
                })
                record.audit_trail = trail
                record.save()
            else:
                raise DRFValidationError({"action": f"Unknown disciplinary action '{action}'."})

        log_action(
            actor=request.user,
            action=f"STAFF_DISCIPLINARY_{action.upper()}",
            instance=record,
            request=request,
            metadata={"reference": record.reference, "status": record.status, "deduction_approved": record.deduction_approved},
        )
        return success_response(
            StaffDisciplinaryRecordSerializer(record, context={"request": request}).data,
            message=f"Disciplinary action '{action}' recorded.",
        )


class StaffDocumentListCreateView(APIView):
    """List or upload employee HR, contract, certification, asset, and exit documents."""
    permission_classes = [HasCapability]
    required_capabilities = ("staff.view", "staff.profile.manage")
    require_any_capability = True

    def get(self, request):
        queryset = StaffDocument.objects.select_related("staff", "uploaded_by").filter(is_active=True)
        if staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if category := request.query_params.get("category"):
            queryset = queryset.filter(category=category.upper())
        return _page(self, request, queryset, StaffDocumentSerializer)

    def post(self, request):
        import secrets
        from django.utils import timezone

        if not has_capability(request.user, "staff.profile.manage") and request.user.role != User.Role.ADMIN:
            raise PermissionDenied("Uploading staff documents requires staff profile management authority.")

        serializer = StaffDocumentSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        now = timezone.now()
        ref = f"DOC-{now.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        uploaded_file = request.FILES.get("file")
        file_name = uploaded_file.name if uploaded_file else (request.data.get("file_name") or "")
        file_size = uploaded_file.size if uploaded_file else int(request.data.get("file_size") or 0)
        mime_type = getattr(uploaded_file, "content_type", "") if uploaded_file else (request.data.get("mime_type") or "")

        doc = serializer.save(
            reference=ref,
            uploaded_by=request.user,
            file_name=file_name[:255],
            file_size=file_size,
            mime_type=mime_type[:120],
        )
        log_action(
            actor=request.user,
            action="STAFF_DOCUMENT_UPLOADED",
            instance=doc,
            request=request,
            metadata={"reference": doc.reference, "staff_id": doc.staff_id, "category": doc.category},
        )
        return success_response(
            StaffDocumentSerializer(doc, context={"request": request}).data,
            message="Staff document uploaded.",
            status=status.HTTP_201_CREATED,
        )


class StaffDocumentDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "staff.profile.manage"

    def patch(self, request, reference: str):
        doc = StaffDocument.objects.filter(reference=reference).first()
        if doc is None:
            raise NotFound("Staff document not found.")
        serializer = StaffDocumentSerializer(doc, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        doc = serializer.save()
        log_action(actor=request.user, action="STAFF_DOCUMENT_UPDATED", instance=doc, request=request)
        return success_response(
            StaffDocumentSerializer(doc, context={"request": request}).data,
            message="Staff document updated.",
        )

    def delete(self, request, reference: str):
        doc = StaffDocument.objects.filter(reference=reference).first()
        if doc is None:
            raise NotFound("Staff document not found.")
        doc.is_active = False
        doc.save(update_fields=["is_active", "updated_at"])
        log_action(actor=request.user, action="STAFF_DOCUMENT_ARCHIVED", instance=doc, request=request)
        return success_response({"reference": doc.reference, "is_active": False}, message="Staff document removed from active profile.")



@extend_schema(tags=["Admin · Staff operations"], summary="List or record staff performance reviews")
class StaffPerformanceReviewListCreateView(APIView):
    """Manager-side review register. Employees never use this endpoint:
    they receive only their own FINALIZED reviews via ``my-profile/``."""

    permission_classes = [HasCapability]
    required_capabilities = ("staff.view", "staff.profile.manage")
    require_any_capability = True

    def get(self, request):
        queryset = StaffPerformanceReview.objects.select_related("staff", "reviewer").all()
        if staff_id := request.query_params.get("staff_id"):
            queryset = queryset.filter(staff_id=staff_id)
        if status_value := request.query_params.get("status"):
            queryset = queryset.filter(status=status_value.upper())
        return _page(self, request, queryset, StaffPerformanceReviewSerializer)

    def post(self, request):
        if not has_capability(request.user, "staff.profile.manage") and request.user.role != User.Role.ADMIN:
            raise PermissionDenied("Recording performance reviews requires staff profile management authority.")
        serializer = StaffPerformanceReviewSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        staff_obj = serializer.validated_data["staff"]
        if getattr(staff_obj, "role", None) == User.Role.GUEST:
            raise PermissionDenied("Performance reviews can only be recorded for staff members.")
        import secrets

        from django.utils import timezone

        now = timezone.now()
        reference = f"PERF-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"
        while StaffPerformanceReview.objects.filter(reference=reference).exists():
            reference = f"PERF-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"
        review = serializer.save(reference=reference, reviewer=request.user)
        log_action(
            actor=request.user, action="STAFF_PERFORMANCE_REVIEW_RECORDED", instance=review, request=request,
            metadata={"reference": review.reference, "staff_id": review.staff_id, "status": review.status},
        )
        return success_response(
            StaffPerformanceReviewSerializer(review, context={"request": request}).data,
            message="Performance review recorded.", status=status.HTTP_201_CREATED,
        )


class StaffPerformanceReviewDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "staff.profile.manage"

    def get(self, request, reference: str):
        review = StaffPerformanceReview.objects.select_related("staff", "reviewer").filter(reference=reference).first()
        if review is None:
            raise NotFound("Performance review not found.")
        return success_response(StaffPerformanceReviewSerializer(review, context={"request": request}).data)

    def patch(self, request, reference: str):
        review = StaffPerformanceReview.objects.filter(reference=reference).first()
        if review is None:
            raise NotFound("Performance review not found.")
        if review.status == StaffPerformanceReview.Status.FINALIZED:
            # A finalized review is the employee-visible record of record;
            # corrections are new reviews, not silent edits.
            raise PermissionDenied("Finalized performance reviews cannot be edited; record a new review instead.")
        serializer = StaffPerformanceReviewSerializer(review, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        review = serializer.save()
        log_action(actor=request.user, action="STAFF_PERFORMANCE_REVIEW_UPDATED", instance=review, request=request)
        return success_response(
            StaffPerformanceReviewSerializer(review, context={"request": request}).data,
            message="Performance review updated.",
        )
