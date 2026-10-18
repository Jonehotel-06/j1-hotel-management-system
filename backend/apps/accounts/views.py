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
from apps.core.client_ip import (
    client_ip_source,
    is_infrastructure_address,
    normalize_ip,
    resolve_client_ip,
)
from apps.core.emails import queue_email
from apps.core.exceptions import StaffSignInRestrictedError
from apps.core.responses import success_response
from apps.core.serializers import EmptySerializer

from .capabilities import capability_codes_for_user
from .desktop_policy import (
    front_desk_desktop_ip_allowed, request_client_ip, resolve_receptionist_desktop,
    sign_in_policy_applies,
)
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


def _tokens_for(user, workstation_credential=None):
    refresh = RefreshToken.for_user(user)
    refresh["role"] = user.role
    if workstation_credential is not None:
        refresh["workstation_credential_id"] = workstation_credential.pk
        refresh["department"] = workstation_credential.department or ""
        access = refresh.access_token
        access["role"] = user.role
        access["workstation_credential_id"] = workstation_credential.pk
        access["department"] = workstation_credential.department or ""
        return {"access": str(access), "refresh": str(refresh)}
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
        # Support temporary biometric workstation credential token or 6-digit numeric access code
        temp_token = request.data.get("temporary_token")
        access_code = (
            request.data.get("access_code")
            or request.data.get("numeric_code")
            or request.data.get("workstation_code")
        )
        if temp_token:
            from apps.staff_operations.services.fingerprint_service import (
                consume_temporary_workstation_credential_record,
                is_staff_authorized_for_department,
            )
            cred = consume_temporary_workstation_credential_record(raw_token=str(temp_token))
            if not cred or not cred.staff or not cred.staff.is_active:
                raise AuthenticationFailed("Invalid or expired temporary workstation credential.")
            user = cred.staff
            target_dept = (
                request.data.get("department")
                or request.headers.get("X-JONE-Department")
                or ""
            ).strip()
            if target_dept and not is_staff_authorized_for_department(user, target_dept):
                raise AuthenticationFailed("Temporary credential is not authorized for this department dashboard.")
            tokens = _tokens_for(user, workstation_credential=cred)
            payload = {
                "user": UserSerializer(user, context={"request": request}).data,
                "tokens": tokens,
            }
            return success_response(payload, message="Logged in with temporary biometric credential.")

        if access_code is not None and str(access_code).strip() != "":
            from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
            from apps.staff_operations.services.fingerprint_service import authenticate_numeric_workstation_code

            target_dept = (
                request.data.get("department")
                or request.headers.get("X-JONE-Department")
                or ""
            ).strip()
            try:
                cred = authenticate_numeric_workstation_code(
                    access_code=str(access_code).strip(),
                    email=str(request.data.get("email") or "").strip(),
                    employee_code=str(request.data.get("employee_code") or "").strip(),
                    department=target_dept,
                    workstation_reference=str(request.data.get("workstation_reference") or "").strip(),
                )
            except DjangoPermissionDenied as exc:
                raise AuthenticationFailed(str(exc)) from exc
            user = cred.staff
            tokens = _tokens_for(user, workstation_credential=cred)
            log_action(
                actor=user,
                action="STAFF_SIGN_IN",
                request=request,
                metadata={
                    "role": user.role,
                    "workstation_reference": cred.workstation_reference,
                    "department": cred.department,
                    "policy": "numeric_workstation_code",
                },
                summary="Staff signed in with numeric workstation code",
            )
            payload = {
                "user": UserSerializer(user, context={"request": request}).data,
                "tokens": tokens,
            }
            return success_response(payload, message="Logged in with numeric workstation access code.")

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
        ws_cred = _refuse_staff_refresh_outside_desk(request)
        response = super().post(request, *args, **kwargs)
        access_str = response.data.get("access")
        if ws_cred is not None and access_str:
            from rest_framework_simplejwt.tokens import AccessToken

            access_obj = AccessToken(access_str)
            access_obj["workstation_credential_id"] = ws_cred.pk
            access_obj["department"] = ws_cred.department or ""
            access_str = str(access_obj)
        tokens = {"access": access_str}
        if response.data.get("refresh"):
            tokens["refresh"] = response.data["refresh"]
        return success_response({"tokens": tokens}, message="Token refreshed.")


def _refuse_staff_refresh_outside_desk(request):
    raw = request.data.get("refresh") if hasattr(request.data, "get") else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        token = RefreshToken(raw)
    except TokenError:
        return None  # simplejwt returns its standard 401 for malformed or expired tokens
    user = User.objects.filter(pk=token.get("user_id")).first()
    if user is None:
        return None
    cred_id = token.get("workstation_credential_id")
    if cred_id:
        from apps.staff_operations.biometric_models import TemporaryWorkstationCredential

        cred = (
            TemporaryWorkstationCredential.objects.select_related("attendance_record")
            .filter(pk=cred_id, staff_id=user.pk)
            .first()
        )
        if (
            cred is None
            or not cred.session_active
            or cred.revoked_at is not None
            or (cred.attendance_record and cred.attendance_record.clock_out_at is not None)
        ):
            raise AuthenticationFailed("Workstation session ended upon clock-out.", code="workstation_clocked_out")
        return cred
    if not sign_in_policy_applies(user):
        return None
    # The same two enrolment routes as sign-in: the browser that proved it was
    # the desktop at sign-in (key, or registered address) keeps its session
    # alive from that same desktop. A shift that began at the desk must not be
    # killed mid-shift just because this header is missing on a refresh.
    desktop, _method = resolve_receptionist_desktop(request)
    if desktop is not None:
        if front_desk_desktop_ip_allowed(request):
            return None
        logger.warning("Staff session refresh refused: user_id=%s role=%s reason=front_desk_ip_mismatch", user.pk, user.role)
        log_action(
            actor=user, action="STAFF_SIGN_IN_RESTRICTED", request=request,
            metadata={
                "reason": "refresh_front_desk_ip_mismatch", "role": user.role,
                "client_ip": request_client_ip(request),
            },
            summary="Staff session refresh refused from a non-Front-Desk IP address",
        )
        raise StaffSignInRestrictedError(
            client_ip=request_client_ip(request), network_mismatch=True,
        )
    logger.warning("Staff session refresh refused: user_id=%s role=%s reason=no_receptionist_desktop", user.pk, user.role)
    log_action(
        actor=user, action="STAFF_SIGN_IN_RESTRICTED", request=request,
        metadata={
            "reason": "refresh_outside_receptionist_desktop", "role": user.role,
            "client_ip": request_client_ip(request),
        },
        summary="Staff session refresh refused outside the Receptionist Desktop",
    )
    raise StaffSignInRestrictedError(client_ip=request_client_ip(request))


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
            token = RefreshToken(refresh)
        except TokenError:
            # Expired or already-blacklisted token: nothing left to revoke.
            logger.info("Logout with stale refresh token: user_id=%s", request.user.id)
            return success_response(message="Logged out successfully.")
        except Exception:
            # Malformed token: still treat as logged out — logout must never
            # trap a user in a session they are trying to leave.
            logger.info("Logout with unusable refresh token: user_id=%s", request.user.id)
            return success_response(message="Logged out successfully.")

        if str(token.get("user_id")) != str(request.user.pk):
            # A logout may only end the caller's OWN session. Supplying somebody
            # else's refresh token would otherwise let any authenticated account
            # end another account's session - and, if the token had merely leaked
            # (logs, a shared device, a copied request), to do so without ever
            # holding a working credential of its own. The answer stays the
            # documented idempotent success, so a confused client still clears its
            # local state; nothing is revoked.
            logger.warning(
                "Logout refused for another account's refresh token: caller=%s", request.user.id
            )
            return success_response(message="Logged out successfully.")

        try:
            token.blacklist()
        except TokenError:
            logger.info("Logout with stale refresh token: user_id=%s", request.user.id)
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


@extend_schema(tags=["Auth"], summary="The client address this request reached the API from")
class ClientAddressView(APIView):
    """What the server sees as this browser's address.

    Needed to register a front desk desktop: the administrator configuring the
    workstation has to know the address the desk actually arrives on, and
    guessing it from a phone or from ``whatismyip`` produces a different answer
    whenever the hotel is behind carrier NAT or the deployment sits behind a
    proxy. It reports the caller's OWN address only.

    ``source`` names the rule that produced the answer — ``peer`` (the direct
    connection), the edge header the deployment trusts (``true-client-ip``,
    ``cf-connecting-ip``, ...), or ``forwarded`` (walked out of the forwarding
    chain) — so an administrator can tell a directly observed address from one
    the deployment had to take its proxy's word for.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = EmptySerializer

    def get(self, request):
        client_ip = resolve_client_ip(request)
        peer = normalize_ip(getattr(request, "META", {}).get("REMOTE_ADDR", ""))
        return success_response({
            "client_ip": client_ip,
            "source": client_ip_source(request),
            # True when the answer did not come from the connection itself,
            # i.e. the deployment's proxy had to speak for this client.
            "trusted_proxy_hops": bool(client_ip and client_ip != peer),
            # True when the resolved address belongs to hosting infrastructure
            # (the platform's own proxy/CGNAT range) rather than to a desk.
            # Registering it would describe the server's network, not the
            # reception desk, and would never match a real sign-in.
            "client_ip_is_infrastructure": is_infrastructure_address(client_ip),
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
        target = self.get_object()
        previous_image_name = getattr(target.profile_image, "name", "") or ""
        serializer = self.get_serializer(target, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        has_new_photo = (
            "profile_image" in getattr(serializer, "validated_data", {})
            and serializer.validated_data.get("profile_image") is not None
        )
        # The storage write happens inside save(); a backend that refuses the
        # file must become a message the staff member can act on instead of a
        # generic failure, and never a silent success.
        from apps.core.storage import StorageUploadError, save_upload, verify_stored_upload

        try:
            user = save_upload(serializer.save, context="profile photo")
        except StorageUploadError:
            if previous_image_name and (getattr(target.profile_image, "name", "") or "") != previous_image_name:
                target.profile_image = previous_image_name
                target.save(update_fields=["profile_image", "updated_at"])
            raise

        stored, warning = True, ""
        if "profile_image" in getattr(serializer, "validated_data", {}):
            # 200 must MEAN the photo is stored: say so, or say exactly what is
            # wrong so the uploader can show it.
            try:
                stored, warning = verify_stored_upload(user.profile_image)
            except StorageUploadError:
                if has_new_photo and previous_image_name:
                    user.profile_image = previous_image_name
                    user.save(update_fields=["profile_image", "updated_at"])
                raise
            if has_new_photo and not stored and previous_image_name:
                user.profile_image = previous_image_name
                user.save(update_fields=["profile_image", "updated_at"])

        payload = UserSerializer(user, context={"request": request}).data
        if "profile_image" in getattr(serializer, "validated_data", {}):
            payload["profile_image_stored"] = stored
            payload["profile_image_warning"] = warning
        return success_response(payload, message="Profile updated.")


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
