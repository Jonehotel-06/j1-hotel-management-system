# apps/bookings/access.py
"""Single authorization rule for guest-facing booking/payment endpoints.

Who may access a booking:

* active staff with the explicit server-side ``booking.read`` or
  ``booking.manage`` capability;
* the account owner (legacy linked accounts) authenticated with a JWT;
* the anonymous guest presenting the booking-scoped bearer token issued at
  checkout in the ``X-Guest-Access-Token`` header. Only a SHA-256 digest of
  that token is stored; comparison is constant-time and expiry-aware
  (see ``Booking.guest_token_matches``).

Callers that fail this check must respond 404 (never 403) so booking
references alone can not be used to probe for other guests' bookings.
"""

from apps.accounts.capabilities import has_capability


GUEST_TOKEN_HEADER = "X-Guest-Access-Token"


def can_access_booking(request, booking, *, staff_required_capabilities=None) -> bool:
    """Check owner/token access or an explicit staff capability scope.

    Ordinary booking reads accept either ``booking.read`` or ``booking.manage``.
    Mutating payment-init/verification callers can pass a stricter tuple; staff
    must hold every named capability, while the owning guest/token remains
    unaffected by staff-only capability checks.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        if staff_required_capabilities is None:
            if has_capability(user, "booking.read") or has_capability(user, "booking.manage"):
                return True
        elif staff_required_capabilities and all(
            has_capability(user, code) for code in staff_required_capabilities
        ):
            return True
        if booking.guest.user_id == user.id:
            return True
    return booking.guest_token_matches(request.headers.get(GUEST_TOKEN_HEADER, ""))
