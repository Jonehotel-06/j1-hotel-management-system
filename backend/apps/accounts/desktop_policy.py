"""Receptionist Desktop sign-in policy.

Operational staff may sign in only from the Receptionist Desktop: an active
``Workstation`` in the FRONT_DESK department. That desktop is identified by
EITHER of two enrolment routes, both verified on the server after the password
has passed:

1. **Desktop key** — the browser was enrolled once with the key issued from the
   Workstations page and presents it in ``X-JONE-Desktop-Key``.
2. **Registered address** — an administrator registered the address the desk
   reaches the API from on the workstation record itself
   (``Workstation.ip_address``), and the request arrives from it. Nothing is
   typed at sign-in: being at the desk is the proof. This is the route that
   lets a receptionist sign in with only their own email and password.

Route 2 needs a trustworthy client address, which behind a managed host is only
available from a forwarding header. That header is read exclusively from a
trusted proxy hop — see ``apps.core.client_ip`` — so a client cannot vote for
its own address.

An Admin may additionally set ``HotelSettings.front_desk_desktop_ip``. When
set, the same staff must also reach sign-in, token refresh, and attendance
clock-in from that network. This is a separate hotel-wide binding: it applies on
top of whichever workstation record identified the browser, so the front desk
can be pinned to one network while several desks are registered behind it.
Changing either the registered address or the settings value is an Admin
configuration change, never a code change.

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

from apps.core.client_ip import (
    address_in_network,
    normalize_ip_or_network,
    resolve_client_ip,
)

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
    """The client IP for this request, resolved by ``apps.core.client_ip``.

    ``REMOTE_ADDR`` is used when the connection arrived directly. When it
    arrived from a trusted proxy (any managed host), the address the proxy
    actually observed is used instead — otherwise every client on the internet
    would present the same address and a registered desktop could never match.
    A forwarding header from an untrusted peer is still ignored.
    """
    return resolve_client_ip(request)


def configured_front_desk_desktop_ip() -> str:
    from apps.hotel.models import HotelSettings

    return normalize_ip_address(HotelSettings.get_settings().front_desk_desktop_ip)


def front_desk_desktop_ip_allowed(request) -> bool:
    """True when no desktop IP is configured, or this request uses it.

    The single hotel-wide address set in Settings is a NETWORK binding: it
    applies to every request the desktop makes, whichever workstation record
    identified the browser. Keeping it separate from the per-workstation
    ``Workstation.ip_address`` means an admin can pin the front desk to one
    network while several desks are registered behind it.
    """
    configured = configured_front_desk_desktop_ip()
    if not configured:
        return True
    return address_in_network(request_client_ip(request), configured)


def presented_desktop_key(request) -> str:
    if request is None:
        return ""
    return str(request.META.get(DESKTOP_HEADER, "") or "")


def receptionist_desktops_for_ip(client_ip: str) -> list[Workstation]:
    """Active FRONT_DESK workstations registered for this address.

    Returns every match rather than one: duplicate registration is an
    administrative mistake worth surfacing, not something to resolve silently
    in favour of whichever row the database happened to return first.
    """
    client_ip = str(client_ip or "").strip()
    if not client_ip:
        return []
    candidates = Workstation.objects.filter(
        department=Workstation.Department.FRONT_DESK,
        is_active=True,
    ).exclude(ip_address="")
    return [desk for desk in candidates if address_in_network(client_ip, desk.ip_address)]


def receptionist_desktop_for_ip(client_ip: str) -> Workstation | None:
    """The active FRONT_DESK workstation registered at this exact address."""
    matches = receptionist_desktops_for_ip(client_ip)
    return matches[0] if len(matches) == 1 else None


#: How the request proved it was the Receptionist Desktop.
DESKTOP_BY_KEY = "key"
DESKTOP_BY_IP = "registered_ip"
DESKTOP_BY_NONE = ""


def resolve_receptionist_desktop(request) -> tuple[Workstation | None, str]:
    """Identify the Receptionist Desktop behind this request.

    Two enrolment routes, both server-verified:

    * a desktop key presented in ``X-JONE-Desktop-Key`` (the desk browser was
      enrolled once from the Workstations page); or
    * the request simply arriving from the address an administrator registered
      for a front desk workstation — the desktop is then identified by where it
      is, with no code to enter on every sign-in.

    Returns ``(workstation, method)`` where ``method`` is ``"key"``,
    ``"registered_ip"`` or ``""``. A caller that gets ``(None, "")`` must treat
    the request as coming from outside the Receptionist Desktop.
    """
    desk = receptionist_desktop_for_key(presented_desktop_key(request))
    if desk is not None:
        return desk, DESKTOP_BY_KEY
    desk = receptionist_desktop_for_ip(request_client_ip(request))
    if desk is not None:
        return desk, DESKTOP_BY_IP
    return None, DESKTOP_BY_NONE


def register_desktop_ip(workstation: Workstation, value) -> str:
    """Set (or clear) a workstation's registered address. Returns the stored value."""
    cleaned = normalize_ip_or_network(value)
    workstation.ip_address = cleaned
    workstation.desktop_ip_registered_at = timezone.now() if cleaned else None
    return cleaned

