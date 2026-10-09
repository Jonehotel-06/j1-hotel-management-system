# apps/accounts/views.py
"""Authentication endpoints (public/guest area of the API)."""
import logging

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from apps.audit.services import log_action
from apps.core.emails import queue_email
from apps.core.exceptions import StaffSignInRestrictedError
from apps.core.responses import success_response
from apps.core.serializers import EmptySerializer

from .capabilities import capability_codes_for_user
from .desktop_policy import presented_desktop_key, receptionist_desktop_for_key, sign_in_policy_applies
from .models import User
from .serializers import (
    JOneTokenObtainPairSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    LogoutSerializer,
    UserSerializer,
)
from rest_framework_simplejwt.views import TokenObtainPairView

logger = logging.getLogger("apps")


def _tokens_for(user):
    refresh = RefreshToken.for_user(user)
    refresh["role"] = user.role
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


@extend_schema(tags=["Auth"], summary="Register an optional guest account")
class RegisterView(generics.CreateAPIView):
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return success_response(
            {"user": UserSerializer(user, context={"request": request}).data,
             "tokens": _tokens_for(user)},
            message="Account created successfully.", status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Auth"], summary="Log in with email and password, or temporary biometric token")
class LoginView(TokenObtainPairView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"
    # Explicit serializer: simplejwt's TOKEN_OBTAIN_SERIALIZER setting is only
    # honored by its own view implementation path; our view opts in explicitly.
    serializer_class = JOneTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        # Support temporary biometric workstation credential token
        temp_token = request.data.get("temporary_token")
        if temp_token:
            from apps.staff_operations.services.fingerprint_service import consume_temporary_workstation_credential
            user = consume_temporary_workstation_credential(temp_token)
            if not user or not user.is_active:
                raise AuthenticationFailed("Invalid or expired temporary workstation credential.")
            tokens = _tokens_for(user)
            payload = {
                "user": UserSerializer(user, context={"request": request}).data,
                "tokens": tokens,
            }
            return success_response(payload, message="Logged in with temporary biometric credential.")

        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            # Log the refusal without the email, password, or any token.
            logger.info("Sign-in refused: invalid credentials request_id=%s", getattr(request, "request_id", ""))
            raise
        payload = {"user": response.data.get("user"), "tokens": {
            "access": response.data.get("access"), "refresh": response.data.get("refresh")}}
        return success_response(payload, message="Logged in successfully.")


@extend_schema(tags=["Auth"], summary="Exchange a refresh token for new tokens")
class JOneTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        # Checked before simplejwt rotates/blacklists the refresh token, so a
        # refused refresh leaves the session untouched. This closes the gap where
        # a refresh token issued before the desk rule would otherwise keep
        # operational staff signed in indefinitely from any device.
        _refuse_staff_refresh_outside_desk(request)
        response = super().post(request, *args, **kwargs)
        tokens = {"access": response.data.get("access")}
        if response.data.get("refresh"):
            tokens["refresh"] = response.data["refresh"]
        return success_response({"tokens": tokens}, message="Token refreshed.")


def _refuse_staff_refresh_outside_desk(request):
    raw = request.data.get("refresh") if hasattr(request.data, "get") else None
    if not isinstance(raw, str) or not raw:
        return
    try:
        token = RefreshToken(raw)
    except TokenError:
        return  # simplejwt returns its standard 401 for malformed or expired tokens
    user = User.objects.filter(pk=token.get("user_id")).first()
    if user is None or not sign_in_policy_applies(user):
        return
    if receptionist_desktop_for_key(presented_desktop_key(request)) is not None:
        return
    logger.warning("Staff session refresh refused: user_id=%s role=%s reason=no_receptionist_desktop", user.pk, user.role)
    log_action(
        actor=user, action="STAFF_SIGN_IN_RESTRICTED", request=request,
        metadata={"reason": "refresh_outside_receptionist_desktop", "role": user.role},
        summary="Staff session refresh refused outside the Receptionist Desktop",
    )
    raise StaffSignInRestrictedError()


@extend_schema(tags=["Auth"], request=None, summary="Log out (blacklist refresh token)")
class LogoutView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LogoutSerializer

    def post(self, request):
        """Blacklist the supplied refresh token.

        Logout is IDEMPOTENT and must always end gracefully for the client: if
        the refresh token is already expired or already blacklisted, the
        session is effectively dead server-side anyway, so the request still
        succeeds (the frontend clears its local session regardless). Only a
        missing token is a client bug worth reporting.
        """
        refresh = request.data.get("refresh")
        if not refresh:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"refresh": ["Refresh token is required."]})
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            # Expired or already-blacklisted token: nothing left to revoke.
            logger.info("Logout with stale refresh token: user_id=%s", request.user.id)
        except Exception:
            # Malformed token: still treat as logged out — logout must never
            # trap a user in a session they are trying to leave.
            logger.info("Logout with unusable refresh token: user_id=%s", request.user.id)
        logger.info("User logged out: user_id=%s", request.user.id)
        return success_response(message="Logged out successfully.")


@extend_schema(tags=["Auth"], summary="Read server-resolved capabilities for the current account")
class MyCapabilitiesView(APIView):
    """A UI convenience endpoint; endpoint-level checks remain authoritative."""

    permission_classes = [IsAuthenticated]
    serializer_class = EmptySerializer

    def get(self, request):
        return success_response({
            "role": request.user.role,
            "capabilities": sorted(capability_codes_for_user(request.user)),
        })


@extend_schema(tags=["Auth"], summary="Get or update the current profile")
class ProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        # Role/is_active/flags are read-only here; only profile fields are PATCHable.
        return ProfileUpdateSerializer if self.request.method in ("PUT", "PATCH") else UserSerializer

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)

    def update(self, request, *args, **kwargs):
        partial = request.method == "PATCH"
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success_response(
            UserSerializer(self.get_object(), context={"request": request}).data,
            message="Profile updated.",
        )


@extend_schema(tags=["Auth"], summary="Change password (authenticated)")
class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PasswordChangeSerializer

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        logger.info("Password changed: user_id=%s", request.user.id)
        return success_response(message="Password changed successfully.")


@extend_schema(tags=["Auth"], summary="Request a password reset email")
class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PasswordResetRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        # Response is identical whether or not the account exists — do not
        # leak account existence to the internet.
        if user:
            from apps.core.email_design import render_notice_email

            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = f"{settings.FRONTEND_URL}/reset-password.html?uid={uid}&token={token}"
            text_body, html_body = render_notice_email(
                category="Account security",
                title="Reset your password",
                greeting=f"Hello {user.first_name},",
                paragraphs=[
                    "We received a request to reset the password for your account. "
                    "Use the button below to choose a new password.",
                ],
                cta_label="Reset Password",
                cta_url=reset_url,
                footnote=(
                    "If you did not request this, you can safely ignore this email — "
                    "your password will not be changed."
                ),
                preheader="Use this link to reset your account password.",
            )
            queue_email(
                kind="PASSWORD_RESET",
                subject="Reset your J-ONE HOTEL & LODGE password",
                message=text_body,
                html_message=html_body,
                recipients=[user.email],
            )
        return success_response(message="If an account exists for this email, a reset link has been sent.")


@extend_schema(tags=["Auth"], summary="Confirm a password reset")
class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PasswordResetConfirmSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        logger.info("Password reset completed: user_id=%s", user.id)
        return success_response(message="Password has been reset successfully. You can now log in.")
