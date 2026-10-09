# apps/accounts/serializers.py
"""Account serializers. Never expose password hashes or role-equality tricks."""
import logging

from django.contrib.auth import password_validation
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.audit.services import log_action
from apps.core.exceptions import StaffSignInRestrictedError
from apps.core.storage import absolute_media_url

from .capabilities import capability_codes_for_user
from .desktop_policy import presented_desktop_key, receptionist_desktop_for_key, sign_in_policy_applies
from .models import User, Workstation

logger = logging.getLogger("apps")


class UserSerializer(serializers.ModelSerializer):
    """Safe public representation of the authenticated user."""

    full_name = serializers.CharField(read_only=True)
    profile_image_url = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "full_name", "phone",
            "role", "capabilities", "email_verified", "profile_image_url", "date_joined", "last_login",
        ]
        read_only_fields = fields  # role changes only happen via admin endpoints

    def get_profile_image_url(self, obj):
        return absolute_media_url(obj.profile_image, self.context.get("request"))

    def get_capabilities(self, obj):
        # The matrix is resolved server-side; this is a display hint for the
        # frontend, never a substitute for the endpoint permission check.
        return sorted(capability_codes_for_user(obj))


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone", "profile_image"]


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
        if sign_in_policy_applies(self.user):
            # Enforced after the password check so the response reveals nothing
            # about an account until its credentials are proven.
            desktop = receptionist_desktop_for_key(presented_desktop_key(request))
            if desktop is None:
                log_staff_sign_in_rejected(self.user, request)
                raise StaffSignInRestrictedError()
        log_staff_sign_in(self.user, request, desktop)
        data["user"] = UserSerializer(self.user, context=self.context).data
        return data


def log_staff_sign_in_rejected(user, request):
    """Audit + log a refused staff sign-in. Never records the password or key."""
    logger.warning("Staff sign-in refused: user_id=%s role=%s reason=no_receptionist_desktop", user.pk, user.role)
    log_action(
        actor=user, action="STAFF_SIGN_IN_RESTRICTED", request=request,
        metadata={"reason": "no_receptionist_desktop", "role": user.role},
        summary="Staff sign-in refused outside the Receptionist Desktop",
    )


def log_staff_sign_in(user, request, desktop):
    """Record successful operational-staff sign-ins (exempt roles included)."""
    if not getattr(user, "is_staff_member", False):
        return
    logger.info(
        "Staff sign-in: user_id=%s role=%s desktop=%s",
        user.pk, user.role, desktop.reference if desktop else "management-exempt",
    )
    log_action(
        actor=user, action="STAFF_SIGN_IN", request=request,
        metadata={
            "role": user.role,
            "desktop_reference": desktop.reference if desktop else "",
            "policy": "receptionist_desktop" if desktop else "management_exempt",
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
        user.save(update_fields=["password", "updated_at"])
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
    """

    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name", "email", "role"]
        read_only_fields = fields


class AdminUserListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    profile_image_url = serializers.SerializerMethodField()
    bookings_count = serializers.SerializerMethodField()

    def get_profile_image_url(self, obj):
        return absolute_media_url(obj.profile_image, self.context.get("request"))

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "full_name", "phone", "role",
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

    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone", "role", "password", "is_active", "profile_image"]

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def create(self, validated_data):
        # is_staff grants Django-admin access for privileged roles only.
        role = validated_data.get("role", User.Role.RECEPTIONIST)
        validated_data["is_staff"] = role in (User.Role.ADMIN,)
        return User.objects.create_user(**validated_data)


class WorkstationSerializer(serializers.ModelSerializer):
    current_staff_email = serializers.CharField(source="current_staff.email", read_only=True, allow_null=True)
    current_staff_name = serializers.CharField(source="current_staff.full_name", read_only=True, allow_null=True)
    # Whether a Receptionist Desktop key is currently issued. The key itself is
    # never serialized; it is returned once from the create/rotate endpoints.
    receptionist_key_configured = serializers.SerializerMethodField()

    class Meta:
        model = Workstation
        fields = [
            "id", "reference", "name", "department", "location", "is_active", "last_seen_at",
            "current_staff_email", "current_staff_name", "receptionist_key_configured",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "last_seen_at", "current_staff_email", "current_staff_name",
            "receptionist_key_configured", "created_at", "updated_at",
        ]

    def get_receptionist_key_configured(self, obj):
        return bool(obj.sign_in_key_hash)


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone", "role", "is_active", "email_verified", "profile_image"]

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
        return attrs
