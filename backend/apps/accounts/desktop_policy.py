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
``STAFF_SIGN_IN_EXEMPT_ROLES``), and the accounts that may record attendance for
somebody else may also run the attendance terminal away from the desk — see
``may_record_attendance_off_desk`` for why that is the same set of accounts. Guest accounts, token refresh, logout and the
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


#: Capability that authorizes signing in from any device. Management holds it by
#: default; anyone else receives it only as an explicit, audited grant
#: (``UserCapabilityOverride``). This is why the exemption is not a rung on a
#: departmental ladder — see ``apps.accounts.departments``.
ANY_DEVICE_CAPABILITY = "sign_in.any_device"


def may_sign_in_anywhere(user: User) -> bool:
    """True when this account is exempt from the front-desk desktop sign-in rule.

    Two routes, both server-side and both checked on every sign-in and refresh:

    * the global management roles, which are outside every department; and
    * the explicit ``sign_in.any_device`` capability, which the persisted
      capability matrix resolves for management by default and for anybody else
      only through an individually granted (and audited) override.

    The capability route exists so that authority is expressed as authority:
    collapsing a departmental "manager" title into the department's one role
    must never silently confine a real manager to a front-desk PC, and adding
    device freedom for a night-shift supervisor must never require inventing a
    new role or a new hierarchy.
    """
    if not user:
        return False
    if user.role in STAFF_SIGN_IN_EXEMPT_ROLES:
        return True
    try:
        from apps.accounts.capabilities import has_capability

        return has_capability(user, ANY_DEVICE_CAPABILITY)
    except Exception:  # noqa: BLE001 - fail closed: still use the desk
        return False


def sign_in_policy_applies(user: User) -> bool:
    """True when this account must sign in through the Receptionist Desktop."""
    if not user or not getattr(user, "is_staff_member", False):
        return False
    return not may_sign_in_anywhere(user)


def receptionist_desktop_required(user: User) -> bool:
    """True when the Receptionist Desktop is the ONLY place this account may act.

    The same management exception as sign-in, expressed as the ONE predicate the
    attendance APIs share, so "who counts as management" can never drift between
    the sign-in policy and the attendance policy.
    """
    return sign_in_policy_applies(user)


def attendance_off_desk_authority(user) -> str:
    """How this account may record attendance away from the Receptionist Desktop.

    Returns the authority ``manual_fallback_authority`` would return for the
    same account (``ADMINISTRATOR`` / ``MANAGER`` / ``NONE``) by DELEGATING to
    it, so two rules that look alike cannot drift apart:

    * who may authorise attendance for somebody else when a fingerprint cannot
      be read, and
    * who may run the attendance terminal when the front desk is not reachable.

    Both are management authority over OTHER people's attendance, both are
    audited with the actor, and both require ``attendance.manage``. Delegating
    rather than restating means a future change to the fallback matrix moves
    this with it instead of quietly opening or closing the station rule.

    Ordinary operational roles always get ``NONE``: their sign-in is already
    bound to the desk, so they never reach this question with a usable session.
    """
    from apps.staff_operations.services.attendance_fallback_service import manual_fallback_authority

    return manual_fallback_authority(user)


def may_record_attendance_off_desk(user) -> bool:
    """True only for an account that actually carries off-desk authority.

    ``NONE`` is a real return value (the service's sentinel), not an empty
    string, so it must be compared by name: a bare truthiness test would let
    every signed-in account through.
    """
    return attendance_off_desk_authority(user) not in ("", "NONE")


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
    """The configured binding as stored: one address/network, or several."""
    from apps.hotel.models import HotelSettings

    return str(HotelSettings.get_settings().front_desk_desktop_ip or "").strip()


def configured_front_desk_desktop_networks() -> list:
    """Every configured entry, parsed. An address becomes its own /32 (or /128).

    A desk whose public address is reassigned by its provider is registered as
    the network it sits on; several entries cover a backup link. Entries that
    no longer parse are ignored here — the model and serializer refuse to store
    them, so this only guards against hand-edited rows.
    """
    from apps.core.client_ip import parse_network

    networks = []
    for item in str(configured_front_desk_desktop_ip() or "").split(","):
        network = parse_network(item)
        if network is not None:
            networks.append(network)
    return networks


def front_desk_desktop_ip_allowed(request) -> bool:
    """True when no desktop IP is configured, or this request uses one of them.

    The hotel-wide value set in Settings is a NETWORK binding: it applies to
    every request the desktop makes, whichever workstation record identified
    the browser. Keeping it separate from the per-workstation
    ``Workstation.ip_address`` means an admin can pin the front desk to one
    network while several desks are registered behind it.
    """
    networks = configured_front_desk_desktop_networks()
    if not networks:
        return True
    client_ip = request_client_ip(request)
    if not client_ip:
        return False
    from apps.core.client_ip import parse_address

    address = parse_address(client_ip)
    if address is None:
        return False
    return any(
        network.version == address.version and address in network
        for network in networks
    )


def configured_front_desk_desktop_cannot_match(client_ip: str) -> bool:
    """True when the SAVED front desk binding could never match this visitor.

    The situation this exists for: the address the server used to report for
    the desk was the hosting platform's own proxy (Railway's ``100.64.0.0/10``
    peer), an administrator saved it, and after the client address is resolved
    correctly that saved value can never equal a real browser's address. Saying
    "this browser is outside the pinned network" would leave the front desk
    permanently locked out with no indication that the SAVED value is the
    problem. Only private/shared/loopback ranges qualify, and only when the
    visitor itself arrived from a public address -- a hotel LAN whose desk
    really is on 192.168.x is a normal, working setup and must stay silent.
    """
    from apps.core.client_ip import is_infrastructure_address, is_infrastructure_network

    if is_infrastructure_address(client_ip):
        return False
    networks = configured_front_desk_desktop_networks()
    if not networks:
        return False
    return all(is_infrastructure_network(network) for network in networks)


def unusable_saved_desktop_addresses(client_ip: str) -> list[str]:
    """Saved desk bindings that no browser arriving from this address can match.

    Both places an administrator can save a desk address are covered: the
    hotel-wide ``HotelSettings.front_desk_desktop_ip`` and each workstation's
    ``ip_address``. Both may hold an address the server reported while it was
    still describing its own proxy (``100.64.0.18`` on Railway), which no
    browser can ever arrive from. Naming the saved values turns "sign-in is
    refused" into "replace THIS value".
    """
    from apps.core.client_ip import is_infrastructure_address, is_infrastructure_network

    if is_infrastructure_address(client_ip):
        # The visitor is itself behind the platform network: the saved value may
        # well be the honest answer for this deployment.
        return []
    def _label(value) -> str:
        """An address network reports as 100.64.0.18/32; show it as it was saved."""
        network = value if hasattr(value, "prefixlen") else None
        if network is not None and network.prefixlen == network.max_prefixlen:
            return str(network.network_address)
        return str(value)

    unusable: list[str] = []
    for network in configured_front_desk_desktop_networks():
        if is_infrastructure_network(network):
            unusable.append(_label(network))
    for workstation in Workstation.objects.filter(
        department=Workstation.Department.FRONT_DESK, is_active=True,
    ).exclude(ip_address=""):
        if is_infrastructure_network(workstation.ip_address):
            unusable.append(_label(workstation.ip_address))
    # Preserve order, drop repeats (the same value is often saved twice).
    seen = set()
    return [value for value in unusable if not (value in seen or seen.add(value))]


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

