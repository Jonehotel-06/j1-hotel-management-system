# apps/core/authentication.py
"""Authentication that cannot turn a signed-out browser into a dead end.

Every guest-facing booking and payment endpoint is ``AllowAny``: a guest proves
the booking with the booking-scoped token in ``X-Guest-Access-Token``, not with
an account. But DRF performs authentication before the view decides anything,
so an **expired or invalid** ``Authorization: Bearer`` header anywhere in the
browser's storage makes those endpoints answer ``401 UNAUTHORIZED`` before the
guest token is ever looked at.

That is not hypothetical. A hotelier who signed into the dashboard on their
phone, a guest whose 30-minute access token aged out while they filled the
booking form, or any leftover token in local storage produces exactly this — and
the booking page can only report it as "this device is no longer recognised for
your booking", which is false and unfixable by the guest.

An unusable credential on a public endpoint means "not signed in", not "denied".
These views authorize independently (guest token, signed access link, or staff
capability), so ignoring the bad bearer grants nothing: the request simply
continues as anonymous.
"""
from __future__ import annotations

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class LenientJWTAuthentication(JWTAuthentication):
    """Treat an invalid/expired bearer token as anonymous instead of 401.

    Only for views that are already open to anonymous callers. Never use it
    where being signed in is what grants access.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None
