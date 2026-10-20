"""DRF authentication for short-lived opaque guest-portal sessions."""
from dataclasses import dataclass

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .services import INVALID_PORTAL_CREDENTIAL_MESSAGE, session_for_token
from .session_cookie import CSRF_HEADER_VALUE, SAFE_METHODS, SESSION_COOKIE_NAME


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
    """Authenticate the portal session from its HttpOnly cookie or ``X-Portal-Session``.

    The browser uses the cookie (never readable by page scripts). Because a
    browser attaches cookies automatically, a cookie-authenticated state-changing
    request must also send ``X-Portal-Request: 1``: a cross-site form or simple
    request cannot add a custom header, so this blocks CSRF. The header path is
    for non-browser clients and is not CSRF-exposed (browsers never attach it).
    Both paths resolve through ``session_for_token``, which re-checks eligibility.
    """

    header_name = "HTTP_X_PORTAL_SESSION"

    def authenticate(self, request):
        raw_token = str(request.META.get(self.header_name, "") or "").strip()
        via_cookie = False
        if not raw_token:
            raw_token = str(request.COOKIES.get(SESSION_COOKIE_NAME, "") or "").strip()
            via_cookie = bool(raw_token)
            if via_cookie and request.method not in SAFE_METHODS:
                if request.META.get("HTTP_X_PORTAL_REQUEST", "") != CSRF_HEADER_VALUE:
                    raise AuthenticationFailed(INVALID_PORTAL_CREDENTIAL_MESSAGE)
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
