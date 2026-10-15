"""Receptionist Desktop sign-in policy.

Operational staff may sign in only from the Receptionist Desktop: an active
``Workstation`` in the FRONT_DESK department that has been issued a desktop
key. The desktop presents that key with the sign-in request in the
``X-JONE-Desktop-Key`` header. The check runs on the server after the password
has been verified, so a crafted client, a direct API call, or a copied browser
form cannot bypass it without the key.

An Admin may additionally set ``HotelSettings.front_desk_desktop_ip``. When
set, the same staff must also reach sign-in, token refresh, and attendance
clock-in from that exact server-seen IP address. This binds the enrolled
desktop browser to the Front Desk desktop's network address; changing the
desktop is an Admin settings change, not a code change. Client-supplied
forwarding headers are not trusted for this decision.

Management roles keep their existing access from any device (see
``STAFF_SIGN_IN_EXEMPT_ROLES``). Guest accounts, token refresh, logout and the
password-reset flows are not affected, so active sessions continue to work.

The key is a bearer secret for the desk browser, not a cryptographic device
attestation. It is hashed at rest, never returned after issue, and can be
rotated or revoked by a holder of ``terminal.manage``.
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets

from django.utils import timezone

from .models import User, Workstation

DESKTOP_HEADER = "HTTP_X_JONE_DESKTOP_KEY"
MAX_KEY_LENGTH = 128

# Management retains access from any device. Everyone else who holds an
# operational staff role must present a Receptionist Desktop key.
STAFF_SIGN_IN_EXEMPT_ROLES = frozenset({
    User.Role.ADMIN,
    User.Role.MANAGER,
    User.Role.GENERAL_MANAGER,
})


def new_desktop_key() -> str:
    return secrets.token_urlsafe(32)


def hash_desktop_key(raw_key: str) -> str:
    return hashlib.sha256(str(raw_key or "").encode("utf-8")).hexdigest()


def issue_desktop_key(workstation: Workstation) -> str:
    """Rotate the workstation's key. Returns the raw key exactly once."""
    raw_key = new_desktop_key()
    workstation.sign_in_key_hash = hash_desktop_key(raw_key)
    workstation.sign_in_key_issued_at = timezone.now()
    return raw_key


def clear_desktop_key(workstation: Workstation) -> None:
    workstation.sign_in_key_hash = ""
    workstation.sign_in_key_issued_at = None


def sign_in_policy_applies(user: User) -> bool:
    """True when this account must sign in through the Receptionist Desktop."""
    if not user or not getattr(user, "is_staff_member", False):
        return False
    return user.role not in STAFF_SIGN_IN_EXEMPT_ROLES


def receptionist_desktop_required(user: User) -> bool:
    """True when the Receptionist Desktop is the ONLY place this account may act.

    The same management exception as sign-in, expressed as the ONE predicate the
    attendance APIs share, so "who counts as management" can never drift between
    the sign-in policy and the attendance policy.
    """
    return sign_in_policy_applies(user)


def receptionist_desktop_for_key(raw_key: str) -> Workstation | None:
    """Resolve an active FRONT_DESK workstation from its presented key."""
    raw_key = str(raw_key or "").strip()
    if not raw_key or len(raw_key) > MAX_KEY_LENGTH:
        return None
    return (
        Workstation.objects.filter(
            sign_in_key_hash=hash_desktop_key(raw_key),
            department=Workstation.Department.FRONT_DESK,
            is_active=True,
        )
        .exclude(sign_in_key_hash="")
        .first()
    )


def normalize_ip_address(value) -> str:
    """Canonical exact IPv4/IPv6 address, or "" when unset/invalid."""
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except (TypeError, ValueError):
        return ""


def request_client_ip(request) -> str:
    """The client IP as seen by Django (``REMOTE_ADDR`` only).

    ``X-Forwarded-For`` is deliberately not trusted here: it is client
    controlled unless a separately configured trusted proxy overwrites it.
    Behind a reverse proxy, the Admin should configure the address the
    server actually sees for the Front Desk desktop (shown in Settings).
    """
    if request is None:
        return ""
    return normalize_ip_address(getattr(request, "META", {}).get("REMOTE_ADDR", ""))


def configured_front_desk_desktop_ip() -> str:
    from apps.hotel.models import HotelSettings

    return normalize_ip_address(HotelSettings.get_settings().front_desk_desktop_ip)


def front_desk_desktop_ip_allowed(request) -> bool:
    """True when no desktop IP is configured, or this request uses it."""
    configured = configured_front_desk_desktop_ip()
    if not configured:
        return True
    return request_client_ip(request) == configured


def presented_desktop_key(request) -> str:
    if request is None:
        return ""
    return str(request.META.get(DESKTOP_HEADER, "") or "")
