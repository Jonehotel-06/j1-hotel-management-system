# apps/accounts/serializers.py
"""Account serializers. Never expose password hashes or role-equality tricks."""
import logging

from django.contrib.auth import password_validation
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.audit.services import log_action
from apps.core.client_ip import normalize_ip_or_network
from apps.core.exceptions import StaffSignInRestrictedError
from apps.core.storage import absolute_media_url

from .capabilities import capability_codes_for_user, has_capability
from .desktop_policy import (
    front_desk_desktop_ip_allowed,
    request_client_ip,
    resolve_receptionist_desktop,
    sign_in_policy_applies,
    unusable_saved_desktop_addresses,
)
from .models import User, Workstation

logger = logging.getLogger("apps")


class UserSerializer(serializers.ModelSerializer):
    """Safe public representation of the authenticated user."""

    full_name = serializers.CharField(read_only=True)
    profile_image_url = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    employee_code = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "middle_name", "last_name", "full_name", "phone",
            "role", "department", "employee_code", "capabilities", "email_verified",
            "must_change_password", "profile_image_url", "date_joined", "last_login",
        ]
        read_only_fields = fields  # role changes only happen via admin endpoints

    def get_profile_image_url(self, obj):
        return absolute_media_url(obj.profile_image, self.context.get("request"))

    def get_capabilities(self, obj):
        # The matrix is resolved server-side; this is a display hint for the
        # frontend, never a substitute for the endpoint permission check.
        return sorted(capability_codes_for_user(obj))

    def get_employee_code(self, obj):
        profile = getattr(obj, "staff_profile", None)
        return profile.employee_code if profile else ""

    def get_department(self, obj):
        if not getattr(obj, "is_staff_member", False):
            return ""
        from apps.staff_operations.services.fingerprint_service import staff_department_code

        return staff_department_code(obj)


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "middle_name", "last_name", "phone", "profile_image"]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone", "password", "password_confirm"]

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": ["Passwords do not match."]})
        password_validation.validate_password(attrs["password"])
        return attrs

    def create(self, validated_data):
        # Public registration always produces a GUEST. Staff roles are only
        # ever granted by an administrator through the admin endpoints.
        return User.objects.create_user(role=User.Role.GUEST, **validated_data)


class JOneTokenObtainPairSerializer(TokenObtainPairSerializer):
    default_error_messages = {"no_active_account": "Invalid email or password."}

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role  # display convenience; never authorization input
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        request = self.context.get("request")
        desktop = None
        desktop_method = ""
        if sign_in_policy_applies(self.user):
            # Enforced after the password check so the response reveals nothing
            # about an account until its credentials are proven. The desktop is
            # identified either by the key its browser was enrolled with, or by
            # the request arriving from the address an administrator registered
            # for a front desk workstation (nothing typed at sign-in).
            desktop, desktop_method = resolve_receptionist_desktop(request)
            if desktop is None:
                log_staff_sign_in_rejected(
                    self.user, request, client_ip=request_client_ip(request),
                )
                unusable = unusable_saved_desktop_addresses(request_client_ip(request))
                raise StaffSignInRestrictedError(
                    client_ip=request_client_ip(request),
                    unusable_saved_addresses=unusable,
                )
            if not front_desk_desktop_ip_allowed(request):
                log_staff_sign_in_rejected(
                    self.user, request, reason="front_desk_ip_mismatch",
                    client_ip=request_client_ip(request),
                )
                raise StaffSignInRestrictedError(
                    client_ip=request_client_ip(request), network_mismatch=True,
                    unusable_saved_addresses=unusable_saved_desktop_addresses(
                        request_client_ip(request)
                    ),
                )
        if request is not None and getattr(self.user, "is_staff_member", False):
            target_dept = (
                (request.data.get("department") if hasattr(request.data, "get") else "")
                or request.headers.get("X-JONE-Department")
                or ""
            ).strip()
            if target_dept:
                from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
                from apps.staff_operations.services.fingerprint_service import is_staff_authorized_for_department

                if not is_staff_authorized_for_department(self.user, target_dept):
                    log_action(
                        actor=self.user,
                        action="CROSS_DEPARTMENT_SIGN_IN_DENIED",
                        request=request,
                        metadata={"target_department": target_dept, "role": self.user.role},
                        summary="Staff sign-in refused for unauthorized department subdomain",
                    )
                    raise DjangoPermissionDenied("Your account is not authorized to sign in to this department dashboard.")
        log_staff_sign_in(self.user, request, desktop, method=desktop_method)
        data["user"] = UserSerializer(self.user, context=self.context).data
        return data


def log_staff_sign_in_rejected(user, request, reason="no_receptionist_desktop", client_ip=""):
    """Audit + log a refused staff sign-in. Never records the password or key.

    The observed client address IS recorded: when a desk has moved, the
    administrator needs to know which address to register, and the audit trail
    is where that answer has to be recoverable from.
    """
    logger.warning(
        "Staff sign-in refused: user_id=%s role=%s reason=%s client_ip=%s",
        user.pk, user.role, reason, client_ip or "-",
    )
    log_action(
        actor=user, action="STAFF_SIGN_IN_RESTRICTED", request=request,
        metadata={"reason": reason, "role": user.role, "client_ip": client_ip},
        summary="Staff sign-in refused outside the Receptionist Desktop",
    )


def log_staff_sign_in(user, request, desktop, method=""):
    """Record successful operational-staff sign-ins (exempt roles included).

    ``method`` is how the browser was accepted as the Receptionist Desktop
    (``key`` | ``registered_ip`` | ``""``). It is recorded because the two
    enrolment routes fail differently: a key that stopped working is a browser
    problem, while a desk whose ``registered_ip`` stopped matching usually means
    the desk moved and an administrator needs to re-register its address.

    The resolved client address is recorded here too (rather than relying on the
    audit row's own ``ip_address``, which is the connection peer and behind a
    managed host is the platform proxy) so the sign-in record itself answers
    "which address did this desk sign in from".
    """
    if not getattr(user, "is_staff_member", False):
        return
    client_ip = request_client_ip(request)
    logger.info(
        "Staff sign-in: user_id=%s role=%s desktop=%s method=%s client_ip=%s",
        user.pk, user.role, desktop.reference if desktop else "management-exempt",
        method or "-", client_ip or "-",
    )
    log_action(
        actor=user, action="STAFF_SIGN_IN", request=request,
        metadata={
            "role": user.role,
            "desktop_reference": desktop.reference if desktop else "",
            "desktop_method": method,
            "client_ip": client_ip,
            "policy": "receptionist_desktop" if desktop else "management_exempt",
            "front_desk_ip_checked": bool(desktop),
        },
        summary="Staff signed in",
    )


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    new_password_confirm = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError({"new_password_confirm": ["Passwords do not match."]})
        password_validation.validate_password(attrs["new_password"], user=self.context["request"].user)
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password", "updated_at"])
        return user


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return User.objects.normalize_email(value)


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    new_password_confirm = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError({"new_password_confirm": ["Passwords do not match."]})
        try:
            uid = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise serializers.ValidationError({"uid": ["Invalid password reset link."]})
        if not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError({"token": ["This reset link is invalid or has expired."]})
        password_validation.validate_password(attrs["new_password"], user=user)
        attrs["user"] = user
        return attrs

    def save(self, **kwargs):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])
        return user


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------
class OperationalStaffDirectorySerializer(serializers.ModelSerializer):
    """Minimal assignment-picker projection for capability-scoped operations.

    Dispatch screens need a stable ID and an active staff label—not profiles,
    contact details, credentials, or account-management controls.

    ``email`` is an ACCOUNT field, not an assignment field. It is returned only
    to callers that hold ``account.view`` (the same capability that unlocks the
    full staff-account directory). A dispatch-only role such as RECEPTIONIST
    can pick an assignee by id/name/role without receiving staff email
    addresses (audit N7). The key stays present so existing consumers keep
    working, and is ``null`` when the caller is not authorized to see it.
    """

    full_name = serializers.CharField(read_only=True)
    email = serializers.SerializerMethodField()

    def _may_view_account_details(self) -> bool:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None:
            # No request context (internal server-side use): keep the old
            # behaviour rather than silently dropping data for our own code.
            return True
        return has_capability(user, "account.view")

    def get_email(self, obj):
        return obj.email if self._may_view_account_details() else None

    class Meta:
        model = User
        fields = ["id", "full_name", "email", "role"]
        read_only_fields = fields


class AdminUserListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    profile_image_url = serializers.SerializerMethodField()
    bookings_count = serializers.SerializerMethodField()
    employee_code = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    job_title = serializers.SerializerMethodField()
    employment_status = serializers.SerializerMethodField()
    employment_type = serializers.SerializerMethodField()
    credential_delivery_status = serializers.SerializerMethodField()

    def get_profile_image_url(self, obj):
        return absolute_media_url(obj.profile_image, self.context.get("request"))

    def get_employee_code(self, obj):
        p = getattr(obj, "staff_profile", None)
        return p.employee_code if p else ""

    def get_department(self, obj):
        p = getattr(obj, "staff_profile", None)
        if p and p.department:
            return p.department
        if getattr(obj, "is_staff_member", False):
            from apps.staff_operations.services.fingerprint_service import staff_department_code
            return staff_department_code(obj)
        return ""

    def get_job_title(self, obj):
        p = getattr(obj, "staff_profile", None)
        return p.job_title if p else ""

    def get_employment_status(self, obj):
        p = getattr(obj, "staff_profile", None)
        return p.employment_status if p else ("ACTIVE" if obj.is_active else "INACTIVE")

    def get_employment_type(self, obj):
        p = getattr(obj, "staff_profile", None)
        return p.employment_type if p else "FULL_TIME"

    def get_credential_delivery_status(self, obj):
        p = getattr(obj, "staff_profile", None)
        return p.credential_delivery_status if p else "NOT_SENT"

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "middle_name", "last_name", "full_name", "phone", "role",
            "employee_code", "department", "job_title", "employment_status", "employment_type",
            "credential_delivery_status", "must_change_password",
            "profile_image_url", "is_active", "email_verified", "bookings_count", "date_joined", "last_login",
        ]

    def get_bookings_count(self, obj):
        return getattr(obj, "bookings_count", None)


class AdminStaffProfileSerializer(serializers.ModelSerializer):
    """Complete, non-credential staff profile for the staff profile modal.

    Everything here is already stored on the account (or derived with cheap
    aggregate counts) — nothing is invented. Password hashes, tokens and any
    other credential material are deliberately absent.
    """

    full_name = serializers.CharField(read_only=True)
    profile_image_url = serializers.SerializerMethodField()
    role_label = serializers.SerializerMethodField()
    is_admin = serializers.BooleanField(read_only=True)
    is_staff_member = serializers.BooleanField(read_only=True)
    stats = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "full_name", "phone", "role",
            "role_label", "profile_image_url", "is_active", "email_verified",
            "is_staff", "is_admin", "is_staff_member", "date_joined", "last_login",
            "updated_at", "stats", "can_manage",
        ]
        read_only_fields = fields

    def get_profile_image_url(self, obj):
        return absolute_media_url(obj.profile_image, self.context.get("request"))

    def get_role_label(self, obj):
        return obj.get_role_display()

    def get_can_manage(self, obj):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.role == User.Role.ADMIN)

    def get_stats(self, obj):
        from django.db.models import Count, Max

        from apps.audit.models import AuditLog

        payments = obj.payments_made.aggregate(count=Count("id"), last=Max("paid_at"))
        audit = AuditLog.objects.filter(actor=obj).aggregate(
            count=Count("id"), last=Max("created_at")
        )
        return {
            "bookings_created": obj.bookings_created.count(),
            "payments_recorded": payments["count"] or 0,
            "last_payment_at": payments["last"].isoformat() if payments["last"] else None,
            "actions_logged": audit["count"] or 0,
            "last_action_at": audit["last"].isoformat() if audit["last"] else None,
        }


class AdminUserCreateSerializer(serializers.ModelSerializer):
    """Administrator-created staff accounts (roles above GUEST)."""

    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False, required=False, allow_blank=True)
    department = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=120)
    job_title = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=120)
    employment_type = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=24)
    employment_status = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=16)
    employment_start = serializers.DateField(write_only=True, required=False, allow_null=True)
    date_of_birth = serializers.DateField(write_only=True, required=False, allow_null=True)
    gender = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=24)
    residential_address = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=1000)
    emergency_contact_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=160)
    emergency_contact_relationship = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=80)
    emergency_contact_phone = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=40)
    emergency_contact_address = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=1000)
    work_location = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=120)
    job_description = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=5000)
    notes = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=5000)

    class Meta:
        model = User
        fields = [
            "email", "first_name", "middle_name", "last_name", "phone", "role",
            "password", "is_active", "must_change_password", "profile_image",
            "department", "job_title", "employment_type", "employment_status", "employment_start",
            "date_of_birth", "gender", "residential_address",
            "emergency_contact_name", "emergency_contact_relationship",
            "emergency_contact_phone", "emergency_contact_address",
            "work_location", "job_description", "notes",
        ]

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate_password(self, value):
        if value:
            password_validation.validate_password(value)
        return value

    def validate_role(self, value):
        """One role per department: a retired departmental title is refused.

        The role IS the department (``apps.accounts.departments``). An operator
        who wants a bar supervisor to have an extra right grants that right
        explicitly and audits it, instead of recreating a hierarchy by title.
        """
        from .departments import RetiredDepartmentalRole, assert_assignable_role

        try:
            return assert_assignable_role(value)
        except RetiredDepartmentalRole as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        attrs = super().validate(attrs)
        from .departments import validate_role_department

        department = str(attrs.get("department") or "").strip()
        role = str(attrs.get("role") or User.Role.RECEPTIONIST).strip()
        try:
            canonical_department = validate_role_department(role, department)
        except ValueError as exc:
            raise serializers.ValidationError({"department": str(exc)}) from exc
        if department:
            attrs["department"] = canonical_department
        return attrs

    def create(self, validated_data):
        import secrets
        from apps.staff_operations.services.staff_operations_service import ensure_staff_profile

        profile_keys = (
            "department", "job_title", "employment_type", "employment_status", "employment_start",
            "date_of_birth", "gender", "residential_address",
            "emergency_contact_name", "emergency_contact_relationship",
            "emergency_contact_phone", "emergency_contact_address",
            "work_location", "job_description", "notes",
        )
        profile_data = {k: validated_data.pop(k) for k in profile_keys if k in validated_data}
        if hasattr(self, "initial_data") and "is_active" not in self.initial_data:
            validated_data["is_active"] = True
        if hasattr(self, "initial_data") and "must_change_password" not in self.initial_data:
            validated_data["must_change_password"] = False

        raw_pw = validated_data.get("password")
        if not raw_pw:
            raw_pw = f"Jone#{secrets.token_hex(4)}!9"
            validated_data["password"] = raw_pw
            validated_data["must_change_password"] = True

        # is_staff grants Django-admin access for privileged roles only.
        role = validated_data.get("role", User.Role.RECEPTIONIST)
        validated_data["is_staff"] = role in (User.Role.ADMIN,)
        user = User.objects.create_user(**validated_data)
        if user.role != User.Role.GUEST and user.is_active and any(v not in (None, "") for v in profile_data.values()):
            profile, _ = ensure_staff_profile(staff=user)
            for k, v in profile_data.items():
                if v is not None and v != "":
                    setattr(profile, k, v)
            profile.save()
        return user


class WorkstationSerializer(serializers.ModelSerializer):
    current_staff_email = serializers.CharField(source="current_staff.email", read_only=True, allow_null=True)
    current_staff_name = serializers.CharField(source="current_staff.full_name", read_only=True, allow_null=True)
    # Whether a Receptionist Desktop key is currently issued. The key itself is
    # never serialized; it is returned once from the create/rotate endpoints.
    receptionist_key_configured = serializers.SerializerMethodField()
    # Whether this record can already identify the desk at sign-in, by either
    # enrolment route. The Terminals page shows it so an administrator can see
    # at a glance whether a front desk PC is actually enrolled.
    receptionist_desktop_ready = serializers.SerializerMethodField()

    class Meta:
        model = Workstation
        fields = [
            "id", "reference", "name", "department", "location", "is_active", "last_seen_at",
            "current_staff_email", "current_staff_name", "receptionist_key_configured",
            "receptionist_desktop_ready", "ip_address", "desktop_ip_registered_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "last_seen_at", "current_staff_email", "current_staff_name",
            "receptionist_key_configured", "receptionist_desktop_ready",
            "desktop_ip_registered_at", "created_at", "updated_at",
        ]

    def get_receptionist_key_configured(self, obj):
        return bool(obj.sign_in_key_hash)

    def get_receptionist_desktop_ready(self, obj):
        if obj.department != Workstation.Department.FRONT_DESK or not obj.is_active:
            return False
        return bool(obj.sign_in_key_hash or obj.ip_address)

    def validate_ip_address(self, value):
        """Accept one exact address (preferred) or a CIDR network, and store it canonically.

        Stored normalized so equality against a resolved client address is a
        string compare, and so ``192.168.001.5`` cannot masquerade as a
        different entry from the same desk's ``192.168.1.5``.
        """
        raw = str(value or "").strip()
        if not raw:
            return ""
        cleaned = normalize_ip_or_network(raw)
        if not cleaned:
            raise serializers.ValidationError(
                "Enter one IP address (e.g. 102.89.34.7) or a network (e.g. 192.168.1.0/24)."
            )
        return cleaned

    def update(self, instance, validated_data):
        if "ip_address" in validated_data and validated_data["ip_address"] != instance.ip_address:
            # Only a changed address re-stamps the registration date; clearing
            # it is a removal, and an unchanged save is not a new enrolment.
            instance.desktop_ip_registered_at = (
                timezone.now() if validated_data["ip_address"] else None
            )
        return super().update(instance, validated_data)


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "first_name", "middle_name", "last_name", "phone", "role",
            "is_active", "email_verified", "must_change_password", "profile_image",
        ]

    def validate(self, attrs):
        request = self.context["request"]
        target = self.instance
        # An admin cannot lock themselves out or strip their own admin role.
        if target and target.id == request.user.id:
            if attrs.get("is_active") is False:
                raise serializers.ValidationError({"is_active": ["You cannot deactivate your own account."]})
            if "role" in attrs and attrs["role"] != User.Role.ADMIN:
                raise serializers.ValidationError({"role": ["You cannot remove your own admin role."]})
        if target and target.is_superuser and request.user != target and not request.user.is_superuser:
            raise serializers.ValidationError("Only a superuser can modify another superuser account.")
        if "role" in attrs:
            from .departments import RetiredDepartmentalRole, assert_assignable_role

            try:
                attrs["role"] = assert_assignable_role(attrs["role"])
            except RetiredDepartmentalRole as exc:
                raise serializers.ValidationError({"role": str(exc)}) from exc
        return attrs

    def update(self, instance, validated_data):
        # Role is the authoritative department identity. Keep an existing
        # profile in step when an administrator transfers the account; a stale
        # profile department must never override the newly assigned role.
        from django.db import transaction
        from .departments import department_for_role

        with transaction.atomic():
            user = super().update(instance, validated_data)
            if "role" in validated_data:
                try:
                    profile = user.staff_profile
                except Exception:  # noqa: BLE001 - accounts need not have profiles
                    profile = None
                if profile is not None:
                    from .departments import _normalize_department_code
                    from apps.staff_operations.models import Department

                    department = department_for_role(user.role)
                    profile.department = department
                    if profile.department_ref_id:
                        current_ref_code = _normalize_department_code(profile.department_ref.code)
                        ref_matches = current_ref_code == department or (
                            current_ref_code == "RESTAURANT_BAR" and department in {"RESTAURANT", "BAR"}
                        )
                        if not ref_matches:
                            profile.department_ref = None
                    if department and not profile.department_ref_id:
                        for candidate in Department.objects.all():
                            if department in {
                                _normalize_department_code(candidate.code),
                                _normalize_department_code(candidate.name),
                            }:
                                profile.department_ref = candidate
                                break
                    profile.save(update_fields=["department", "department_ref", "updated_at"])
            return user

