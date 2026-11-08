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
3. **Configured Front Desk desktop address** — an administrator saved the same
   address (or network) in ``HotelSettings.front_desk_desktop_ip``, which is
   where the Settings page reports ``request_ip`` for exactly this purpose, and
   the request arrives from it. An active FRONT_DESK ``Workstation`` must still
   exist, so the desk registration is real and can be deactivated; the hotel
   cannot be talked into trusting an address it retired. This route exists
   because saving the address in Settings used to be restriction-only: an
   administrator who followed the on-screen instruction and copied
   ``request_ip`` into ``front_desk_desktop_ip`` got a value that could REFUSE
   a sign-in and could never AUTHORIZE one, leaving staff at that very desk
   refused and pushed onto the numeric access code.
4. **Approved device credential** — the browser was approved once from the
   Terminals page (an administrator reviews the request there) and presents the
   token it was issued in ``X-JONE-Device-Token``. Only a SHA-256 digest is
   stored, the token is bound to one staff account and to one active FRONT_DESK
   workstation, and it is revocable at any time. This route exists because an
   address is NOT a stable identity: after an unexpected shutdown the router
   hands the desk a different address, and an address-based rule then refuses
   sign-in AND kills every live session at its next token refresh. The device
   is the durable fact; the address is metadata that is logged when it changes.

Route 2 and route 3 need a trustworthy client address, which behind a managed
host is only available from a forwarding header. That header is read
exclusively from a trusted proxy hop — see ``apps.core.client_ip`` — so a client
cannot vote for its own address.

PRECEDENCE (one consistent rule; the first match wins, and the two registration
sources can no longer contradict each other silently):

1. a presented desktop key that resolves to an active FRONT_DESK workstation;
2. a presented device token that resolves to an ACTIVE ``FrontDeskDevice`` whose
   workstation is an active FRONT_DESK record and whose bound staff account is
   the account signing in (or, on refresh, the account the token belongs to);
3. the request arriving from an address registered on an active FRONT_DESK
   workstation (``Workstation.ip_address``);
4. the request arriving from an address/network configured in
   ``HotelSettings.front_desk_desktop_ip``, while at least one active FRONT_DESK
   workstation exists;
5. otherwise the request is outside the Receptionist Desktop.

An Admin may additionally set ``HotelSettings.front_desk_desktop_ip``. When
set, staff identified by route 1 must ALSO reach sign-in, token refresh, and
attendance clock-in from that network — that is the pin that makes a stolen
desktop key useless off-site, and it is unchanged. Routes 2 and 3 already prove
the address, so the pin is satisfied by definition on those paths. Changing
either the registered address or the settings value is an Admin configuration
change, never a code change.

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

#: Header carrying the approved-device token. A browser keeps it in local
#: storage and sends it on sign-in, refresh and attendance calls. It is a bearer
#: secret like the desktop key: hashed at rest, returned once, never logged.
DEVICE_HEADER = "HTTP_X_JONE_DEVICE_TOKEN"
MAX_DEVICE_TOKEN_LENGTH = 256
#: How many distinct addresses a device row remembers. A desk that moves keeps a
#: short history (enough to answer "which address was it on last week"), bounded
#: so the row cannot grow without limit.
DEVICE_IP_HISTORY_LIMIT = 20

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


def new_device_token() -> str:
    """A fresh 256-bit device token, URL-safe (32 bytes)."""
    return secrets.token_urlsafe(32)


def presented_device_token(request) -> str:
    if request is None:
        return ""
    return str(request.META.get(DEVICE_HEADER, "") or "").strip()


def front_desk_device_for_token(raw_token: str, *, staff=None) -> "FrontDeskDevice | None":
    """Resolve an ACTIVE approved device from the token it presented.

    Every refusal path is answered with ``None`` — the caller cannot tell an
    inactive device from a wrong token, and there is nothing to learn from that
    difference. What is checked here is the complete authority chain, so a token
    cannot outlive any part of it:

    * the device row exists, is active and is not revoked;
    * its workstation still exists and is an ACTIVE ``FRONT_DESK`` record
      (retiring the desk retires its devices);
    * its bound staff account is still active, and — when the caller names the
      account that is signing in — it is the SAME account, so a copied token
      cannot be replayed by a different user.
    """
    from .models import FrontDeskDevice

    raw_token = str(raw_token or "").strip()
    if not raw_token or len(raw_token) > MAX_DEVICE_TOKEN_LENGTH:
        return None
    device = (
        FrontDeskDevice.objects.select_related("workstation", "staff")
        .filter(token_hash=FrontDeskDevice.hash_token(raw_token), is_active=True)
        .exclude(token_hash="")
        .first()
    )
    if device is None or device.revoked_at is not None:
        return None
    workstation = device.workstation
    if workstation is None or not workstation.is_active:
        return None
    if workstation.department != Workstation.Department.FRONT_DESK:
        return None
    if device.staff is not None:
        if not device.staff.is_active:
            return None
        if staff is not None and device.staff_id != getattr(staff, "pk", None):
            return None
    elif staff is not None:
        # An unbound device may only be used while it is being claimed at
        # enrolment; after that it always carries an owner.
        return None
    return device


def device_belongs_to_staff(raw_token: str, staff) -> bool:
    """True when this device token was issued to THIS staff account.

    A device credential proves "this is the approved front desk browser"; it must
    never also prove "which staff member is here". Sign-in therefore requires
    the token's owner to be the account that just proved its password, so a
    token copied to another machine cannot sign anybody else in. A token with no
    owner yet (a pending enrolment) matches nobody: it carries no authority
    until an administrator approves it.
    """
    device = front_desk_device_for_token(raw_token, staff=staff)
    return device is not None and device.staff_id is not None and device.staff_id == getattr(staff, "pk", None)


def issue_device_token(device, staff, *, device_label: str = "", request=None) -> str:
    """Approve (or re-approve) a device and return its token — exactly once.

    Rotation is the same call: an EXISTING token is replaced, so re-approving a
    browser after a reinstall is a single operation and never leaves two live
    credentials for one desk. Only the digest is stored; the raw value exists in
    this return value and nowhere else, and is never written to the audit trail.

    The device must sit at an ACTIVE FRONT_DESK workstation: approving a browser
    for a retired desk would create a credential with no authority, which is a
    support call waiting to happen.
    """
    from .models import FrontDeskDevice

    workstation = device.workstation
    if workstation is None or not workstation.is_active or workstation.department != Workstation.Department.FRONT_DESK:
        raise ValueError("Approved devices can only be issued for an active Front Desk workstation.")
    raw_token = new_device_token()
    client_ip = request_client_ip(request) if request is not None else ""
    device.token_hash = FrontDeskDevice.hash_token(raw_token)
    device.staff = staff
    device.device_label = str(device_label or device.device_label or "")[:160]
    device.is_active = True
    device.revoked_at = None
    device.revoked_by = None
    device.revoked_reason = ""
    device.first_enrolled_ip = device.first_enrolled_ip or client_ip
    device.last_ip_address = client_ip or device.last_ip_address
    device.save(update_fields=[
        "token_hash", "staff", "device_label", "is_active", "revoked_at", "revoked_by",
        "revoked_reason", "first_enrolled_ip", "last_ip_address", "updated_at",
    ])
    if client_ip:
        record_device_seen(device, client_ip)
    return raw_token


def revoke_device(device, *, by=None, reason: str = "") -> None:
    """Withdraw a device credential. Takes effect on the very next request."""
    device.is_active = False
    device.revoked_at = timezone.now()
    device.revoked_by = by
    device.revoked_reason = str(reason or "")[:255]
    device.save(update_fields=["is_active", "revoked_at", "revoked_by", "revoked_reason", "updated_at"])


def record_device_seen(device: "FrontDeskDevice", client_ip: str) -> None:
    """Remember that the device was used, and from which address.

    THE ADDRESS IS METADATA. It is recorded so an administrator can SEE that the
    desk moved (and when), and so a refused sign-in can name the address to
    review. It is never required to match anything: a changed address is a
    logged fact, not a refusal reason. Nothing here rewrites the workstation's
    registered address — an enrolment must be an explicit administrative act,
    never a side effect of somebody signing in.

    Saved with a targeted ``update_fields`` so a refresh cannot clobber a
    concurrent edit to the same row.
    """
    from django.utils import timezone as _timezone

    now = _timezone.now()
    client_ip = str(client_ip or "").strip()
    updates: dict = {"last_seen_at": now, "updated_at": now}
    history = list(device.ip_addresses or [])
    if client_ip and client_ip not in history:
        history = (history + [client_ip])[-DEVICE_IP_HISTORY_LIMIT:]
    if client_ip and device.last_ip_address != client_ip:
        updates["last_ip_address"] = client_ip
    if history != list(device.ip_addresses or []):
        updates["ip_addresses"] = history
    device.last_seen_at = now
    device.last_ip_address = updates.get("last_ip_address", device.last_ip_address)
    device.ip_addresses = history
    type(device).objects.filter(pk=device.pk).update(**updates)


def device_credential_overrides_unusable_ip_pin(request, method: str) -> bool:
    """Allow an APPROVED DEVICE past an IP pin that no browser could ever satisfy.

    An administrator-issued device token is a stronger, revocable statement of
    identity than a network address: it is bound to one staff account and one
    active front desk workstation, and can be withdrawn instantly. But the
    hotel-wide ``HotelSettings.front_desk_desktop_ip`` pin still applies
    wherever it CAN work.

    It is ignored in exactly one situation, mirroring
    ``desktop_key_override_for_unusable_ip_pin``: the request arrived from a
    public address and EVERY saved pin is a private/shared/loopback address that
    no browser on the internet can match (the address the platform's own proxy
    used to report). Keeping the two overrides identical means the two
    enrolment routes cannot disagree about when a saved pin is dead.
    """
    if method != DESKTOP_BY_DEVICE:
        return False
    return configured_front_desk_desktop_cannot_match(request_client_ip(request))


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
DESKTOP_BY_DEVICE = "approved_device"
DESKTOP_BY_IP = "registered_ip"
DESKTOP_BY_SETTINGS_IP = "settings_ip"
DESKTOP_BY_NONE = ""


def active_front_desk_workstation() -> Workstation | None:
    """Any active FRONT_DESK workstation, or None.

    Used by the Settings-registered desktop route: the address is what admits
    the request, but the hotel must still own a real, deactivatable front desk
    record, so retiring the desk (``is_active=False``) also retires the address
    an administrator once saved for it.
    """
    return (
        Workstation.objects.filter(
            department=Workstation.Department.FRONT_DESK,
            is_active=True,
        )
        .order_by("id")
        .first()
    )


def front_desk_workstation_missing() -> bool:
    """True when the hotel has no active FRONT_DESK workstation record at all.

    Every Receptionist Desktop enrolment route dead-ends without one: a desktop
    key can only be issued for a workstation record, ``Workstation.ip_address``
    lives on that record, and the ``HotelSettings.front_desk_desktop_ip``
    binding only takes effect while an active FRONT_DESK workstation exists.
    A sign-in refusal that answers "register this address" while no workstation
    exists sends the administrator in a circle — the address alone can never
    be enough — so this flag lets the refusal say the one thing that is
    actually missing.
    """
    return active_front_desk_workstation() is None


def resolve_receptionist_desktop(request) -> tuple[Workstation | None, str]:
    """Identify the Receptionist Desktop behind this request.

    Four enrolment routes, all server-verified and tried in the documented
    precedence order:

    * a desktop key presented in ``X-JONE-Desktop-Key`` (the desk browser was
      enrolled once from the Workstations page);
    * an approved-device token presented in ``X-JONE-Device-Token`` (the browser
      was approved from the Terminals page and survives address changes; see
      ``front_desk_device_for_token``);
    * the request arriving from the address an administrator registered on a
      front desk workstation (``Workstation.ip_address``) — the desktop is then
      identified by where it is, with no code to enter on every sign-in; or
    * the request arriving from an address/network an administrator saved in
      ``HotelSettings.front_desk_desktop_ip``, while at least one active
      FRONT_DESK workstation exists. This is the value the Settings page
      reports as ``request_ip`` and invites an administrator to save, so saving
      it must authorize the desk it describes — before this route existed the
      saved value could only ever REFUSE a sign-in and staff at that very desk
      were pushed onto the numeric access code.

    Returns ``(workstation, method)`` where ``method`` is ``"key"``,
    ``"approved_device"``, ``"registered_ip"``, ``"settings_ip"`` or ``""``. A caller that gets
    ``(None, "")`` must treat the request as coming from outside the
    Receptionist Desktop.
    """
    desk = receptionist_desktop_for_key(presented_desktop_key(request))
    if desk is not None:
        return desk, DESKTOP_BY_KEY
    # Second route: an APPROVED DEVICE. Placed before the address routes on
    # purpose — the device is the durable identity, so a desk whose address has
    # just changed is recognised here rather than after every address rule has
    # failed. ``staff`` is not passed: at sign-in the account is not yet bound
    # to the request, so the caller re-checks ownership (see
    # ``device_belongs_to_staff``); on refresh the token's own account is the
    # subject.
    device = front_desk_device_for_token(presented_device_token(request))
    if device is not None:
        return device.workstation, DESKTOP_BY_DEVICE
    client_ip = request_client_ip(request)
    desk = receptionist_desktop_for_ip(client_ip)
    if desk is not None:
        return desk, DESKTOP_BY_IP
    # Third route: the address an administrator saved in Settings. Only a
    # CONFIGURED binding can admit anything here — an empty setting must not
    # turn every device on the internet into the front desk.
    if client_ip and configured_front_desk_desktop_networks() and front_desk_desktop_ip_allowed(request):
        desk = active_front_desk_workstation()
        if desk is not None:
            return desk, DESKTOP_BY_SETTINGS_IP
    return None, DESKTOP_BY_NONE


def register_desktop_ip(workstation: Workstation, value) -> str:
    """Set (or clear) a workstation's registered address. Returns the stored value."""
    cleaned = normalize_ip_or_network(value)
    workstation.ip_address = cleaned
    workstation.desktop_ip_registered_at = timezone.now() if cleaned else None
    return cleaned


def desktop_key_override_for_unusable_ip_pin(request, method: str) -> bool:
    """Allow an admin-issued desktop key past an impossible saved IP pin.

    A dynamically assigned public ISP address is expected. The durable identity
    is the hashed, admin-issued ``X-JONE-Desktop-Key``; it remains subject to
    workstation activation/revocation and staff credentials. The legacy
    ``HotelSettings.front_desk_desktop_ip`` pin still applies everywhere it can
    work. It is ignored only when the request arrived from a public address and
    EVERY saved network is private/shared/loopback infrastructure that no public
    browser can match. Arbitrary forwarded headers are still never trusted.
    """
    if method != DESKTOP_BY_KEY:
        return False
    return configured_front_desk_desktop_cannot_match(request_client_ip(request))
