# apps/bookings/access.py
"""Single authorization rule for guest-facing booking/payment endpoints.

Who may act on a booking:

* staff (any role) authenticated with their normal JWT;
* the account owner (legacy linked accounts) authenticated with a JWT;
* the anonymous guest presenting the booking-scoped bearer token issued at
  checkout in the ``X-Guest-Access-Token`` header. Only a SHA-256 digest of
  that token is stored; comparison is constant-time and expiry-aware
  (see ``Booking.guest_token_matches``).

Callers that fail this check must respond 404 (never 403) so booking
references alone can not be used to probe for other guests' bookings.
"""

GUEST_TOKEN_HEADER = "X-Guest-Access-Token"


def can_access_booking(request, booking) -> bool:
    """True when the requester is staff, the owning account, or presents the
    booking's valid guest access token."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        if getattr(user, "is_staff_member", False):
            return True
        if booking.guest.user_id == user.id:
            return True
    return booking.guest_token_matches(request.headers.get(GUEST_TOKEN_HEADER, ""))
