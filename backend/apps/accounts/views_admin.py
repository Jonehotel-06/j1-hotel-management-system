# apps/accounts/views_admin.py
"""Administrator-only user & staff account management (ADMIN role)."""
import logging

from django.db.models import Count, Q
from drf_spectacular.utils import extend_schema
from rest_framework import filters, generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from apps.audit.services import log_action
from apps.bookings.services.booking_service import UNCOUNTED_BOOKING_STATUSES
from apps.core.pagination import StandardPagination
from apps.core.permissions import CanViewStaffProfile, HasCapability, IsAdminRole
from apps.core.responses import success_response

from .models import User
from .serializers import (
    AdminStaffProfileSerializer,
    OperationalStaffDirectorySerializer,
    AdminUserCreateSerializer,
    AdminUserListSerializer,
    AdminUserUpdateSerializer,
    UserSerializer,
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
        "guest_request.assign", "housekeeping.task.assign", "maintenance.work_order.assign", "shift.manage", "staff.profile.manage",
    )
    require_any_capability = True

    def get(self, request):
        queryset = User.objects.filter(is_active=True).exclude(role=User.Role.GUEST)
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
            )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset.order_by("first_name", "last_name", "email", "pk"), request, view=self)
        return paginator.get_paginated_response(OperationalStaffDirectorySerializer(page, many=True).data)


class AdminUserQuerysetMixin:
    permission_classes = [IsAdminRole]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = User.objects.all().annotate(
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
        before = {"role": instance.role, "is_active": instance.is_active}
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
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
        return success_response(
            AdminUserListSerializer(instance, context={"request": request}).data, message="User updated."
        )


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
