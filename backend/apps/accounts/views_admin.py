# apps/accounts/views_admin.py
"""Staff account management: administrator writes, manager-tier reads.

The staff account collection (list/retrieve) answers the Administrator fully
and the manager tier (Manager, General Manager) read-only — see
``apps.core.permissions.IsAdminOrManagerRead``. Account creation, updates,
role changes and activation stay administrator-only, as do the permission
matrix, per-user capability overrides and audit logs.
"""
import logging

from django.db.models import Count, Q
from drf_spectacular.utils import extend_schema
from rest_framework import filters, generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from apps.audit.services import log_action
from apps.bookings.services.booking_service import UNCOUNTED_BOOKING_STATUSES
from apps.core.pagination import StandardPagination
from apps.core.permissions import CanViewStaffProfile, HasCapability, IsAdminOrManagerRead, IsAdminRole
from apps.core.responses import success_response

from .desktop_policy import clear_desktop_key, issue_desktop_key
from .models import User, Workstation
from .serializers import (
    AdminStaffProfileSerializer,
    OperationalStaffDirectorySerializer,
    AdminUserCreateSerializer,
    AdminUserListSerializer,
    AdminUserUpdateSerializer,
    UserSerializer,
    WorkstationSerializer,
)

logger = logging.getLogger("apps")


@extend_schema(tags=["Admin · Operations"], summary="Find active operational staff for a permitted dispatch")
class OperationalStaffDirectoryView(APIView):
    """Bounded, non-sensitive staff picker for dispatch and shift forms.

    The legacy administrator user API intentionally remains administrator-only.
    This projection is separately capability-gated so a receptionist who can
    dispatch a housekeeping or maintenance task can select a valid assignee
    without gaining account-management data or rights.
    """

    permission_classes = [HasCapability]
    required_capabilities = (
        "guest_request.assign", "housekeeping.task.assign", "maintenance.work_order.assign", "shift.manage",
        "staff.profile.manage", "payroll.manage",
    )
    require_any_capability = True

    def get(self, request):
        # The Administrator account is the hotel owner, not an operational staff
        # member: it is never offered as the assignee of a shift, a task, an
        # attendance record, a leave request or a disciplinary case, so the
        # owner cannot be rostered or disciplined by mistake. Owner identity is
        # presented on the profile page instead.
        queryset = (
            User.objects.filter(is_active=True)
            .exclude(role__in=(User.Role.GUEST, User.Role.ADMIN))
            .select_related("staff_profile")
        )
        roles_raw = request.query_params.get("roles") or request.query_params.get("role") or ""
        if roles_raw:
            valid_roles = set(User.Role.values) - {User.Role.GUEST}
            roles = [value.strip().upper() for value in str(roles_raw).split(",") if value.strip().upper() in valid_roles]
            queryset = queryset.filter(role__in=roles) if roles else queryset.none()
        if search := request.query_params.get("search"):
            queryset = queryset.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
                | Q(staff_profile__employee_code__icontains=search)
            )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset.order_by("first_name", "last_name", "email", "pk"), request, view=self)
        # The serializer needs the request to decide whether this caller is
        # authorized to see account fields (email); see OperationalStaffDirectorySerializer.
        return paginator.get_paginated_response(
            OperationalStaffDirectorySerializer(page, many=True, context={"request": request}).data
        )


class WorkstationListCreateView(APIView):
    """Register or list workstation labels; the generated reference is attribution only."""

    permission_classes = [HasCapability]
    required_capability = "terminal.manage"
    pagination_class = StandardPagination

    def get(self, request):
        queryset = Workstation.objects.select_related("current_staff").order_by("department", "name", "pk")
        if active := (request.query_params.get("active") or "").strip().lower():
            if active not in {"true", "false", "1", "0"}:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"active": "Use true or false."})
            queryset = queryset.filter(is_active=active in {"true", "1"})
        if department := (request.query_params.get("department") or "").strip().upper():
            queryset = queryset.filter(department=department)
        if search := (request.query_params.get("search") or "").strip():
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(name__icontains=search)
                | Q(location__icontains=search)
                | Q(department__icontains=search)
                | Q(current_staff__email__icontains=search)
                | Q(current_staff__first_name__icontains=search)
                | Q(current_staff__last_name__icontains=search)
            )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(WorkstationSerializer(page, many=True).data)

    def post(self, request):
        serializer = WorkstationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        terminal = serializer.save(created_by=request.user, updated_by=request.user)
        payload = WorkstationSerializer(terminal).data
        message = "Workstation registered. Its reference is optional attribution, not a login credential."
        if terminal.department == Workstation.Department.FRONT_DESK and terminal.is_active:
            # The Receptionist Desktop key is shown once; only its digest is stored.
            raw_key = issue_desktop_key(terminal)
            terminal.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at", "updated_at"])
            payload = WorkstationSerializer(terminal).data
            payload["receptionist_desktop_key"] = raw_key
            message = "Receptionist Desktop registered. Copy its key now; it will not be shown again."
        log_action(
            actor=request.user, action="WORKSTATION_REGISTERED", instance=terminal, request=request,
            metadata={"reference": terminal.reference, "department": terminal.department,
                      "receptionist_key_issued": bool(payload.get("receptionist_desktop_key"))},
        )
        return success_response(payload, message=message, status=status.HTTP_201_CREATED)


class WorkstationDetailView(APIView):
    permission_classes = [HasCapability]
    required_capability = "terminal.manage"

    def patch(self, request, pk):
        terminal = Workstation.objects.select_related("current_staff").filter(pk=pk).first()
        if terminal is None:
            from rest_framework.exceptions import NotFound
            raise NotFound("Workstation not found.")
        serializer = WorkstationSerializer(terminal, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        terminal = serializer.save(updated_by=request.user)
        if terminal.department != Workstation.Department.FRONT_DESK and terminal.sign_in_key_hash:
            # A non-reception workstation can never act as the Receptionist Desktop.
            clear_desktop_key(terminal)
            terminal.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at", "updated_at"])
        log_action(
            actor=request.user, action="WORKSTATION_UPDATED", instance=terminal, request=request,
            metadata={"reference": terminal.reference, "is_active": terminal.is_active},
        )
        return success_response(WorkstationSerializer(terminal).data, message="Workstation updated.")


@extend_schema(tags=["Admin · Users"], summary="Issue a new Receptionist Desktop key (rotates the previous key)")
class WorkstationDesktopKeyView(APIView):
    """Rotate a front-desk workstation's key. The new key is returned exactly once."""

    permission_classes = [HasCapability]
    required_capability = "terminal.manage"

    def post(self, request, pk):
        terminal = Workstation.objects.filter(pk=pk).first()
        if terminal is None:
            raise NotFound("Workstation not found.")
        if terminal.department != Workstation.Department.FRONT_DESK:
            raise ValidationError({"department": "Only a FRONT_DESK workstation can be the Receptionist Desktop."})
        if not terminal.is_active:
            raise ValidationError({"is_active": "Activate this workstation before issuing its desktop key."})
        raw_key = issue_desktop_key(terminal)
        terminal.updated_by = request.user
        terminal.save(update_fields=["sign_in_key_hash", "sign_in_key_issued_at", "updated_by", "updated_at"])
        log_action(
            actor=request.user, action="RECEPTIONIST_DESKTOP_KEY_ISSUED", instance=terminal, request=request,
            metadata={"reference": terminal.reference},
            summary="Receptionist Desktop key issued; any previous key was revoked",
        )
        payload = WorkstationSerializer(terminal).data
        payload["receptionist_desktop_key"] = raw_key
        return success_response(
            payload, message="Receptionist Desktop key issued. Copy it now; it will not be shown again.",
        )


class AdminUserQuerysetMixin:
    # Batch 07: the manager tier (Manager, General Manager) may READ the staff
    # account collection for the staff management interface; every write on
    # these views is still administrator-only via IsAdminOrManagerRead.
    permission_classes = [IsAdminOrManagerRead]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = User.objects.select_related("staff_profile").annotate(
            bookings_count=Count(
                "guest_profile__bookings",
                filter=~Q(guest_profile__bookings__status__in=UNCOUNTED_BOOKING_STATUSES),
                distinct=True,
            )
        )
        params = self.request.query_params
        role = params.get("role")
        if role:
            qs = qs.filter(role=role.upper())
        # `role__in=ADMIN,MANAGER,RECEPTIONIST` — lets the staff console ask for
        # "every staff account, whatever its role" without listing guest
        # accounts. Additive: an absent parameter changes nothing.
        roles_in = params.get("role__in") or params.get("roles")
        if roles_in:
            wanted = [r.strip().upper() for r in str(roles_in).split(",") if r.strip()]
            if wanted:
                qs = qs.filter(role__in=wanted)
        is_active = params.get("is_active")
        if is_active is not None and is_active != "":
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))
        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(phone__icontains=search)
            )
        ordering = params.get("ordering", "-date_joined")
        allowed = {"date_joined", "-date_joined", "email", "-email", "role", "-role", "last_login", "-last_login"}
        if ordering in allowed:
            qs = qs.order_by(ordering)
        return qs


@extend_schema(tags=["Admin · Users"])
class AdminUserListCreateView(AdminUserQuerysetMixin, generics.ListCreateAPIView):
    def get_serializer_class(self):
        return AdminUserCreateSerializer if self.request.method == "POST" else AdminUserListSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        log_action(
            actor=request.user,
            action="USER_CREATED",
            instance=user,
            metadata={"role": user.role, "email": user.email},
            request=request,
        )
        logger.info("Admin %s created user %s (%s)", request.user.id, user.id, user.role)
        return success_response(
            AdminUserListSerializer(user).data, message="User created.", status=status.HTTP_201_CREATED
        )


@extend_schema(tags=["Admin · Users"])
class AdminUserDetailView(AdminUserQuerysetMixin, generics.RetrieveUpdateAPIView):
    http_method_names = ["get", "patch", "head", "options"]

    def get_serializer_class(self):
        return AdminUserUpdateSerializer if self.request.method == "PATCH" else AdminUserListSerializer

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        data = AdminUserListSerializer(instance, context={"request": request}).data
        data["profile"] = UserSerializer(instance, context={"request": request}).data
        return success_response(data)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        previous_image_name = getattr(instance.profile_image, "name", "") or ""
        before = {"role": instance.role, "is_active": instance.is_active}
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        has_new_photo = (
            "profile_image" in getattr(serializer, "validated_data", {})
            and serializer.validated_data.get("profile_image") is not None
        )
        from apps.core.storage import StorageUploadError, save_upload, verify_stored_upload

        # Same rule as the self-service profile: a refused photo upload is an
        # error the administrator can read, not a 500 and not a silent success.
        try:
            instance = save_upload(serializer.save, context="staff photo")
        except StorageUploadError:
            if previous_image_name and (getattr(instance.profile_image, "name", "") or "") != previous_image_name:
                instance.profile_image = previous_image_name
                instance.save(update_fields=["profile_image", "updated_at"])
            raise

        stored, warning = True, ""
        if "profile_image" in getattr(serializer, "validated_data", {}):
            try:
                stored, warning = verify_stored_upload(instance.profile_image)
            except StorageUploadError:
                if has_new_photo and previous_image_name:
                    instance.profile_image = previous_image_name
                    instance.save(update_fields=["profile_image", "updated_at"])
                raise
            if has_new_photo and not stored and previous_image_name:
                instance.profile_image = previous_image_name
                instance.save(update_fields=["profile_image", "updated_at"])

        changes = {}
        for field in before:
            old = before[field]
            new = getattr(instance, field)
            if old != new:
                changes[field] = [old, new]
        log_action(
            actor=request.user,
            action="USER_ROLE_CHANGED" if "role" in changes else "USER_UPDATED",
            instance=instance,
            changes=changes,
            request=request,
        )
        logger.info("Admin %s updated user %s: %s", request.user.id, instance.id, changes)
        payload = AdminUserListSerializer(instance, context={"request": request}).data
        if "profile_image" in getattr(serializer, "validated_data", {}):
            payload["profile_image_stored"] = stored
            payload["profile_image_warning"] = warning
        return success_response(payload, message="User updated.")


@extend_schema(tags=["Admin · Users"], summary="Staff profile card (photo, role, activity)")
class AdminStaffProfileView(generics.RetrieveAPIView):
    """Read-only profile card used by the staff profile modal.

    Access is decided entirely by :class:`CanViewStaffProfile` (admins and
    managers see every account, receptionists only themselves). Mutations stay
    on the ADMIN-only ``/api/admin/users/{id}/`` endpoint.
    """

    permission_classes = [CanViewStaffProfile]
    serializer_class = AdminStaffProfileSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        return (
            User.objects.all()
            .select_related()
            .prefetch_related("guest_profile")
            .order_by("-date_joined")
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        data = self.get_serializer(instance, context={"request": request}).data
        # Cheap, honest operational context: how many bookings this account has
        # as a hotel guest (staff accounts are usually 0).
        # Cancelled/expired bookings are excluded, matching the guests console.
        data["guest_bookings_count"] = (
            instance.guest_profile.bookings.exclude(
                status__in=UNCOUNTED_BOOKING_STATUSES
            ).count()
            if hasattr(instance, "guest_profile")
            else 0
        )
        return success_response(data)


class AdminUserAccountActionView(APIView):
    """Administrator / Staff Management lifecycle controls for employee accounts."""
    permission_classes = [HasCapability]
    required_capability = "staff.profile.manage"

    def post(self, request, pk: int):
        import secrets
        from django.utils import timezone
        from apps.core.emails import send_email_safe
        from apps.staff_operations.models import Department
        from apps.staff_operations.services.fingerprint_service import revoke_workstation_credentials_on_clock_out
        from apps.staff_operations.services.staff_operations_service import ensure_staff_profile

        target = User.objects.select_related("staff_profile").filter(pk=pk).first()
        if target is None:
            raise NotFound("Staff user not found.")

        action = (request.data.get("action") or "").strip().lower()
        if not action:
            raise ValidationError({"action": "An account action is required."})

        # Non-ADMIN managers cannot modify an ADMIN account or their own active/role state
        if target.role == User.Role.ADMIN and request.user.role != User.Role.ADMIN:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only an Administrator can modify an Administrator account.")

        now = timezone.now()

        if action in ("activate", "reactivate"):
            target.is_active = True
            target.save(update_fields=["is_active", "updated_at"])
            profile, _ = ensure_staff_profile(staff=target) if target.role != User.Role.GUEST else (None, False)
            if profile:
                profile.employment_status = profile.EmploymentStatus.ACTIVE
                profile.employment_end_date = None
                profile.employment_end_reason = ""
                profile.save(update_fields=["employment_status", "employment_end_date", "employment_end_reason", "updated_at"])
            log_action(actor=request.user, action="STAFF_ACCOUNT_REACTIVATED", instance=target, request=request)
            return success_response(AdminUserListSerializer(target, context={"request": request}).data, message="Staff account reactivated.")

        profile = getattr(target, "staff_profile", None)
        if profile is None and target.is_active and target.role != User.Role.GUEST:
            profile, _ = ensure_staff_profile(staff=target)

        if action == "suspend":
            if target.pk == request.user.pk:
                raise ValidationError({"action": "You cannot suspend your own account."})
            target.is_active = False
            target.save(update_fields=["is_active", "updated_at"])
            if profile:
                profile.employment_status = profile.EmploymentStatus.SUSPENDED
                profile.save(update_fields=["employment_status", "updated_at"])
            revoke_workstation_credentials_on_clock_out(staff=target, reason="SUSPENDED")
            log_action(
                actor=request.user, action="STAFF_ACCOUNT_SUSPENDED", instance=target, request=request,
                metadata={"reason": str(request.data.get("reason") or "")[:250]},
            )
            return success_response(AdminUserListSerializer(target, context={"request": request}).data, message="Staff member suspended.")

        if action in ("deactivate", "archive"):
            if target.pk == request.user.pk:
                raise ValidationError({"action": "You cannot deactivate your own account."})
            target.is_active = False
            target.save(update_fields=["is_active", "updated_at"])
            if profile:
                profile.employment_status = profile.EmploymentStatus.INACTIVE
                profile.employment_end_date = request.data.get("employment_end_date") or now.date()
                profile.employment_end_reason = str(request.data.get("reason") or "Archived / Deactivated")[:255]
                profile.save(update_fields=["employment_status", "employment_end_date", "employment_end_reason", "updated_at"])
            revoke_workstation_credentials_on_clock_out(staff=target, reason="DEACTIVATED")
            log_action(
                actor=request.user, action="STAFF_ACCOUNT_DEACTIVATED", instance=target, request=request,
                metadata={"reason": str(request.data.get("reason") or "")[:250]},
            )
            return success_response(AdminUserListSerializer(target, context={"request": request}).data, message="Staff account deactivated and archived safely.")

        if action in ("reset_password", "resend_credentials"):
            if request.user.role != User.Role.ADMIN and not (request.user.pk != target.pk):
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("Password reset requires Administrator or independent HR authority.")
            new_pw = (request.data.get("new_password") or "").strip()
            generated = False
            if not new_pw:
                new_pw = f"Jone#{secrets.token_hex(4)}!9"
                generated = True
            target.set_password(new_pw)
            target.must_change_password = bool(request.data.get("must_change_password", True))
            target.save(update_fields=["password", "must_change_password", "updated_at"])

            delivery_status = "PENDING_DELIVERY"
            if request.data.get("send_email", True):
                ok = send_email_safe(
                    subject="J-One Hotel & Lodge — Staff Account Credentials",
                    message=(
                        f"Hello {target.full_name},\n\n"
                        f"Your J-One Hotel & Lodge staff account ({target.email}) credentials have been issued/reset.\n"
                        f"Temporary Password: {new_pw}\n\n"
                        f"Please sign in at your department portal and change your password immediately."
                    ),
                    recipient_list=[target.email],
                )
                delivery_status = "SENT" if ok else "PENDING_DELIVERY"
            if profile:
                profile.credential_delivery_status = delivery_status
                if delivery_status == "SENT":
                    profile.credential_delivered_at = now
                profile.save(update_fields=["credential_delivery_status", "credential_delivered_at", "updated_at"])
            log_action(
                actor=request.user, action="STAFF_CREDENTIALS_ISSUED", instance=target, request=request,
                metadata={"delivery_status": delivery_status, "must_change_password": target.must_change_password},
            )
            res_payload = AdminUserListSerializer(target, context={"request": request}).data
            res_payload["credential_delivery_status"] = delivery_status
            if generated and request.user.role == User.Role.ADMIN:
                res_payload["temporary_password"] = new_pw
            return success_response(res_payload, message=f"Credentials updated ({delivery_status.lower().replace('_', ' ')}).")

        if action == "transfer_department":
            from apps.accounts.departments import (
                RetiredDepartmentalRole, assert_assignable_role, validate_role_department,
            )

            new_dept = (request.data.get("department") or "").strip()
            new_title = (request.data.get("job_title") or "").strip()
            requested_role = (request.data.get("role") or "").strip().upper()
            if not new_dept and not requested_role:
                raise ValidationError({"department": "Specify a new department or canonical role for the transfer."})
            old_dept = profile.department if profile else ""
            old_role = target.role
            try:
                new_role = assert_assignable_role(requested_role or target.role)
                canonical_department = validate_role_department(new_role, new_dept)
            except (RetiredDepartmentalRole, ValueError) as exc:
                raise ValidationError({"role": str(exc)}) from exc
            if new_role != target.role and request.user.role != User.Role.ADMIN:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("Only an Administrator can change an employee's assigned role.")
            if new_role != target.role:
                target.role = new_role
                target.save(update_fields=["role", "updated_at"])
            if profile:
                # Store the validated canonical department, never the unchecked
                # browser label. Keep the optional lookup relation only when it
                # resolves to the same authoritative department.
                profile.department = canonical_department
                if canonical_department:
                    matching = None
                    from apps.accounts.departments import _normalize_department_code
                    for candidate in Department.objects.all():
                        if canonical_department in {
                            _normalize_department_code(candidate.code),
                            _normalize_department_code(candidate.name),
                        }:
                            matching = candidate
                            break
                    profile.department_ref = matching
                else:
                    profile.department_ref = None
                if new_title:
                    profile.job_title = new_title
                profile.save()
            log_action(
                actor=request.user, action="STAFF_DEPARTMENT_TRANSFERRED", instance=target, request=request,
                metadata={"from_department": old_dept, "to_department": profile.department if profile else new_dept, "from_role": old_role, "to_role": target.role},
            )
            return success_response(AdminUserListSerializer(target, context={"request": request}).data, message="Department / role transfer completed.")

        raise ValidationError({"action": f"Unsupported action '{action}'."})


class AdminPermissionMatrixView(APIView):
    """Administrator-only view to inspect and manage the role & user permission matrix."""
    permission_classes = [IsAdminRole]

    def get(self, request):
        from .capabilities import CAPABILITY_DEFINITIONS, ROLE_CAPABILITY_CODES
        from .departments import ASSIGNABLE_ROLES, DEPARTMENT_ROLES
        from .models import Capability, RoleCapability, UserCapabilityOverride

        capabilities = list(
            Capability.objects.filter(is_active=True).order_by("category", "code")
        )
        active_matrix_roles = [User.Role.ADMIN, *DEPARTMENT_ROLES.values()]
        active_matrix_role_set = set(ASSIGNABLE_ROLES) - {User.Role.GUEST}
        role_labels = dict(User.Role.choices)
        role_grants = {role_val: set() for role_val in active_matrix_roles if role_val in active_matrix_role_set}
        for rc in RoleCapability.objects.select_related("capability").filter(
            role__in=active_matrix_role_set, capability__is_active=True
        ):
            role_grants.setdefault(rc.role, set()).add(rc.capability.code)
        if User.Role.ADMIN in role_grants:
            role_grants[User.Role.ADMIN] = {c.code for c in capabilities}

        overrides = [
            {
                "id": ov.pk,
                "user_id": ov.user_id,
                "user_email": ov.user.email,
                "user_name": ov.user.full_name,
                "user_role": ov.user.role,
                "capability_code": ov.capability.code,
                "effect": ov.effect,
                "reason": ov.reason,
                "updated_by_email": ov.updated_by.email if ov.updated_by else "",
                "updated_at": ov.updated_at,
            }
            for ov in UserCapabilityOverride.objects.select_related("user", "capability", "updated_by").all()[:200]
        ]

        return success_response({
            "capabilities": [
                {
                    "id": c.pk,
                    "code": c.code,
                    "name": c.name,
                    "category": c.category,
                    "description": c.description,
                    "admin_only": c.code == "audit.view",
                }
                for c in capabilities
            ],
            "roles": [
                {
                    "role": role_val,
                    "label": role_labels.get(role_val, role_val),
                    "capabilities": sorted(role_grants.get(role_val, set())),
                    "default_capabilities": sorted(ROLE_CAPABILITY_CODES.get(role_val, frozenset())),
                }
                for role_val in active_matrix_roles
                if role_val in active_matrix_role_set
            ],
            "user_overrides": overrides,
        })

    def post(self, request):
        from .models import Capability, RoleCapability, UserCapabilityOverride
        from .departments import RetiredDepartmentalRole, assert_assignable_role

        role = (request.data.get("role") or "").strip().upper()
        capability_code = (request.data.get("capability_code") or "").strip()
        effect = (request.data.get("effect") or "GRANT").strip().upper()

        try:
            role = assert_assignable_role(role)
        except RetiredDepartmentalRole as exc:
            raise ValidationError({"role": str(exc)}) from exc
        if not role or role not in User.Role.values or role == User.Role.GUEST:
            raise ValidationError({"role": "Select a valid assignable staff role."})
        if role == User.Role.ADMIN:
            raise ValidationError({"role": "The ADMIN role permanently holds all active capabilities."})
        cap = Capability.objects.filter(code=capability_code, is_active=True).first()
        if cap is None:
            raise ValidationError({"capability_code": "Unknown capability code."})
        if cap.code == "audit.view":
            raise ValidationError({"capability_code": "The audit.view capability is strictly reserved for the Administrator role."})

        if effect == "GRANT":
            RoleCapability.objects.get_or_create(role=role, capability=cap)
        elif effect == "REVOKE":
            RoleCapability.objects.filter(role=role, capability=cap).delete()
        else:
            raise ValidationError({"effect": "Effect must be GRANT or REVOKE."})

        log_action(
            actor=request.user,
            action="ROLE_CAPABILITY_UPDATED",
            instance=cap,
            request=request,
            metadata={"role": role, "capability_code": cap.code, "effect": effect},
        )
        return self.get(request)


class AdminUserPermissionsView(APIView):
    """Administrator-only view to inspect and override individual user capabilities."""
    permission_classes = [IsAdminRole]

    def get(self, request, pk: int):
        from .capabilities import capability_codes_for_user
        from .models import RoleCapability, UserCapabilityOverride

        target = User.objects.filter(pk=pk).first()
        if target is None:
            raise NotFound("User not found.")
        if hasattr(target, "_jone_capability_codes"):
            delattr(target, "_jone_capability_codes")
        effective = sorted(capability_codes_for_user(target))
        role_defaults = sorted(
            RoleCapability.objects.filter(role=target.role, capability__is_active=True)
            .values_list("capability__code", flat=True)
        )
        overrides = [
            {
                "id": ov.pk,
                "capability_code": ov.capability.code,
                "capability_name": ov.capability.name,
                "effect": ov.effect,
                "reason": ov.reason,
                "updated_at": ov.updated_at,
            }
            for ov in UserCapabilityOverride.objects.select_related("capability").filter(user=target)
        ]
        return success_response({
            "user_id": target.pk,
            "email": target.email,
            "full_name": target.full_name,
            "role": target.role,
            "effective_capabilities": effective,
            "role_capabilities": role_defaults,
            "overrides": overrides,
        })

    def post(self, request, pk: int):
        from .models import Capability, UserCapabilityOverride

        target = User.objects.filter(pk=pk).first()
        if target is None:
            raise NotFound("User not found.")
        if target.role == User.Role.ADMIN:
            raise ValidationError({"user": "Administrator accounts hold all capabilities by default."})

        capability_code = (request.data.get("capability_code") or "").strip()
        effect = (request.data.get("effect") or "").strip().upper()
        reason = str(request.data.get("reason") or "")[:255]

        if capability_code:
            cap = Capability.objects.filter(code=capability_code, is_active=True).first()
            if cap is None:
                raise ValidationError({"capability_code": "Unknown capability code."})
            if effect in ("CLEAR", "DEFAULT", "REMOVE"):
                UserCapabilityOverride.objects.filter(user=target, capability=cap).delete()
            elif effect in ("GRANT", "REVOKE"):
                UserCapabilityOverride.objects.update_or_create(
                    user=target,
                    capability=cap,
                    defaults={"effect": effect, "reason": reason, "updated_by": request.user},
                )
            else:
                raise ValidationError({"effect": "Effect must be GRANT, REVOKE, or CLEAR."})
        else:
            grants = request.data.get("grants") or []
            revocations = request.data.get("revocations") or []
            UserCapabilityOverride.objects.filter(user=target).delete()
            for code in grants:
                cap = Capability.objects.filter(code=code, is_active=True).first()
                if cap:
                    UserCapabilityOverride.objects.create(
                        user=target, capability=cap, effect="GRANT", reason=reason, updated_by=request.user
                    )
            for code in revocations:
                cap = Capability.objects.filter(code=code, is_active=True).first()
                if cap:
                    UserCapabilityOverride.objects.create(
                        user=target, capability=cap, effect="REVOKE", reason=reason, updated_by=request.user
                    )

        if hasattr(target, "_jone_capability_codes"):
            delattr(target, "_jone_capability_codes")
        log_action(
            actor=request.user,
            action="USER_CAPABILITY_OVERRIDE_UPDATED",
            instance=target,
            request=request,
            metadata={"capability_code": capability_code, "effect": effect, "reason": reason},
        )
        return self.get(request, pk=pk)

