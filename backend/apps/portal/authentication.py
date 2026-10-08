"""DRF authentication for short-lived opaque guest-portal sessions."""
from dataclasses import dataclass

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .services import INVALID_PORTAL_CREDENTIAL_MESSAGE, session_for_token


@dataclass(frozen=True)
class PortalPrincipal:
    """Minimal authenticated principal; portal access is email-scoped only."""

    email: str
    portal_session_id: int
    is_authenticated: bool = True
    is_active: bool = True
    is_anonymous: bool = False
    role: str = "PORTAL_GUEST"

    @property
    def pk(self):  # Django/DRF integrations occasionally inspect this attribute.
        return None


class PortalSessionAuthentication(BaseAuthentication):
    """Authenticate ``X-Portal-Session`` without sharing staff JWT privileges."""

    header_name = "HTTP_X_PORTAL_SESSION"

    def authenticate(self, request):
        raw_token = str(request.META.get(self.header_name, "") or "").strip()
        if not raw_token:
            return None
        # Opaque tokens are URL-safe and fixed-size in current issuance. Bound
        # input before hashing/querying to avoid turning a header into work.
        if len(raw_token) < 32 or len(raw_token) > 256:
            raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)
        session = session_for_token(raw_token)
        request.portal_session = session
        return PortalPrincipal(email=session.email, portal_session_id=session.pk), session

    def authenticate_header(self, request):
        return "PortalSession"
