# apps/core/client_ip.py
"""Resolve the real client IP, without handing that decision to the client.

WHY THIS MODULE EXISTS
=====================
Two rules in the product depend on knowing where a request actually came from:

* an operational staff account may sign in (and keep its session alive) only
  from the Front Desk desktop, and an administrator may register that desktop
  by network address; and
* attendance for operational staff is recorded at that same desktop.

The obvious implementation — read ``REMOTE_ADDR`` — is correct only when the
browser talks to Django directly. In every managed deployment this project
targets (Render, Fly, Heroku, Railway, any nginx/gunicorn front end) the
connection Django sees comes from the platform's proxy, so ``REMOTE_ADDR`` is
the *proxy's* address and is identical for every guest, phone and staff member
on the internet. Registering a desktop IP then becomes impossible: the value
never matches.

THE RULE
========
``X-Forwarded-For`` cannot simply be trusted either — any client can send it.
So it is honoured **only when the connection itself arrived from a trusted
proxy**, which is decided by ``REMOTE_ADDR``:

* ``REMOTE_ADDR`` is not a trusted proxy -> the peer is the client. The
  forwarding header is ignored entirely (previous behaviour, still the default
  for a directly exposed server).
* ``REMOTE_ADDR`` is a trusted proxy -> the client is taken from the edge's
  OWN single-value header when one is present (see below), and otherwise from
  ``X-Forwarded-For`` read from the RIGHT, skipping any further trusted
  proxies. Reading from the right is what defeats a client that sends its own
  ``X-Forwarded-For``: a proxy *appends* the address it actually saw, so a
  forged prefix can never displace the true last hop.

WHY THE EDGE HEADER COMES FIRST
===============================
``X-Forwarded-For`` alone is not enough on the host this project deploys to.
Render (fronted by Cloudflare) does not rewrite the header a client sent; it
appends to it, so what arrives is::

    X-Forwarded-For: <forged...>, <real client>, <Cloudflare edge>, <Render 10.x>

The first address is client-controlled, and the addresses behind the real
client are the provider's own edge. Walking right-to-left therefore lands on a
Cloudflare edge address -- a different one for every request a desk makes, so a
registered desk address could never match.

Render's edge *does* set ``True-Client-IP`` (and Cloudflare sets
``CF-Connecting-IP``) to the client it actually saw, overwriting anything the
client sent. When the connection already came from a trusted proxy, that
value is the best answer available and is preferred over the chain. Deployments
with a different front end name their own header in
``TRUSTED_CLIENT_IP_HEADERS`` (for example ``x-real-ip`` for nginx).

The trusted set defaults to the private and loopback ranges. That is
deliberate: an address like ``10.0.0.5`` can only be the peer when the
connection came from inside the deployment's own network, which a client on
the public internet cannot arrange. A deployment whose proxy uses public
addresses adds them through ``TRUSTED_PROXY_CIDRS``.
"""
from __future__ import annotations

import ipaddress

from django.conf import settings

#: Used when ``TRUSTED_PROXY_CIDRS`` is not configured. These ranges are only
#: reachable by the deployment's own network, so trusting a forwarding header
#: from them cannot be arranged by an off-network client.
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

FORWARDED_FOR_HEADER = "HTTP_X_FORWARDED_FOR"

#: Single-value headers an edge rewrites with the client it saw. Used only when
#: the connection arrived from a trusted proxy, so a client can only be believed
#: about its own address if it is already inside the deployment's network.
#: ``true-client-ip`` is what Render/Cloudflare write; ``cf-connecting-ip`` is
#: Cloudflare's own. A forged value of either is overwritten at the edge.
DEFAULT_CLIENT_IP_HEADERS = ("HTTP_TRUE_CLIENT_IP", "HTTP_CF_CONNECTING_IP")


def _header_setting_to_meta(value) -> tuple[str, ...]:
    """``"true-client-ip, x-real-ip"`` -> ``("HTTP_TRUE_CLIENT_IP", "HTTP_X_REAL_IP")``."""
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    keys = []
    for item in value or ():
        name = str(item or "").strip()
        if not name:
            continue
        keys.append(name if name.upper().startswith("HTTP_") else name.upper().replace("-", "_").join(("HTTP_", "")))
    return tuple(keys)


def trusted_client_ip_headers() -> tuple[str, ...]:
    """The edge headers to prefer, from ``TRUSTED_CLIENT_IP_HEADERS``.

    Unset or blank keeps the defaults (the edge-written headers); ``[]`` or
    ``none`` turns the preference off and leaves only the forwarding chain.
    """
    return _header_setting_to_meta(_setting_list("TRUSTED_CLIENT_IP_HEADERS", DEFAULT_CLIENT_IP_HEADERS))


def _edge_header_address(meta) -> tuple[str, str]:
    """``(address, header name)`` from the first usable edge header, else ``("", "")``."""
    for key in trusted_client_ip_headers():
        candidate = normalize_ip(meta.get(key, ""))
        if candidate:
            return candidate, key[len("HTTP_"):].lower().replace("_", "-")
    return "", ""


def normalize_ip(value) -> str:
    """A canonical IPv4/IPv6 address, or "" when unset, malformed or a range."""
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except (TypeError, ValueError):
        return ""


def normalize_ip_or_network(value) -> str:
    """A canonical single address or CIDR network, or "" when invalid.

    A desktop may be registered as one exact address (preferred) or as the
    network its router hands out. ``strict=False`` keeps host bits from making
    an otherwise valid entry look malformed (``192.168.1.5/24`` -> ``192.168.1.0/24``).
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        if "/" in raw:
            return str(ipaddress.ip_network(raw, strict=False))
        return str(ipaddress.ip_address(raw))
    except (TypeError, ValueError):
        return ""


def parse_address(value) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """A single address, or None. A network ("/24") is deliberately not a host."""
    raw = str(value or "").strip()
    if not raw or "/" in raw:
        return None
    try:
        return ipaddress.ip_address(raw)
    except (TypeError, ValueError):
        return None


def parse_network(value) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """A network. A bare address is treated as its own single-host network."""
    raw = normalize_ip_or_network(value)
    if not raw:
        return None
    try:
        return ipaddress.ip_network(raw, strict=False)
    except (TypeError, ValueError):
        return None


def _network_contains(network, address) -> bool:
    """Host-in-network, version-safe.

    ``ipaddress`` only answers ``address in network`` correctly for a single
    address; asking whether one *network* sits inside another (which is what
    passing a parsed /32 would do) is answered differently between Python
    versions, so the containment check is always done on the address.
    """
    if network is None or address is None:
        return False
    return network.version == address.version and address in network


def _setting_list(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated setting, tolerating the two ways it can be "empty".

    A deployment copies ``.env.example`` and often leaves a key blank; blank must
    mean "unset" (the defaults apply) rather than "trust nothing", because the
    latter silently breaks the very feature the setting exists for. An explicit
    empty list — or the word ``none`` — is how a deployment turns it OFF.
    """
    configured = getattr(settings, name, None)
    if configured is None:
        return defaults
    if isinstance(configured, str):
        if not configured.strip():
            return defaults
        if configured.strip().lower() in {"none", "off", "-"}:
            return ()
        configured = configured.split(",")
    cleaned = tuple(str(item).strip() for item in configured if str(item).strip())
    return cleaned


def _configured_proxy_cidrs() -> tuple[str, ...]:
    return _setting_list("TRUSTED_PROXY_CIDRS", DEFAULT_TRUSTED_PROXY_CIDRS)


def trusted_proxy_networks() -> list:
    """Parsed trusted-proxy networks. Defaults apply when unset or unparsable."""
    networks = [network for network in (parse_network(item) for item in _configured_proxy_cidrs()) if network]
    return networks


def is_trusted_proxy(address: str) -> bool:
    parsed = parse_address(address)
    if parsed is None:
        return False
    # A v4 address and a v6 network (or vice versa) are never comparable.
    return any(_network_contains(network, parsed) for network in trusted_proxy_networks())


def forwarded_addresses(header_value) -> list[str]:
    """Every usable address in an ``X-Forwarded-For`` header, left to right."""
    addresses = []
    for part in str(header_value or "").split(","):
        candidate = normalize_ip(part)
        if candidate:
            addresses.append(candidate)
    return addresses


def resolve_client_ip(request) -> str:
    """The client address for this request, as best the server can establish.

    Returns "" when nothing usable is available, so callers can tell "no
    address" apart from "an address that did not match".
    """
    if request is None:
        return ""
    meta = getattr(request, "META", {}) or {}
    peer = normalize_ip(meta.get("REMOTE_ADDR", ""))
    addresses = forwarded_addresses(meta.get(FORWARDED_FOR_HEADER, ""))
    if not addresses:
        return peer
    if not is_trusted_proxy(peer):
        # Directly exposed (or an untrusted peer): the peer IS the client and
        # its forwarding headers are only claims.
        return peer
    if trusted_client_ip_headers():
        edge_address, _header = _edge_header_address(meta)
        if edge_address:
            return edge_address
    # Walk from the right, stepping over our own proxies. The last hop the
    # trusted proxy actually observed is the first address that is not ours.
    for candidate in reversed(addresses):
        if not is_trusted_proxy(candidate):
            return candidate
    # Every hop was one of ours (e.g. a bare health check through the proxy):
    # the leftmost entry is the closest thing to a client we have.
    return addresses[0]


def client_ip_source(request) -> str:
    """Which rule produced the answer, for auditing and for the admin UI.

    ``peer`` (the direct connection), a forwarding header's name
    (``true-client-ip``, ``cf-connecting-ip``, ``x-real-ip`` ...), or
    ``forwarded`` (walked out of ``X-Forwarded-For``).
    """
    if request is None:
        return ""
    meta = getattr(request, "META", {}) or {}
    peer = normalize_ip(meta.get("REMOTE_ADDR", ""))
    if not is_trusted_proxy(peer):
        return "peer"
    if trusted_client_ip_headers():
        _address, header = _edge_header_address(meta)
        if header:
            return header
    return "forwarded" if forwarded_addresses(meta.get(FORWARDED_FOR_HEADER, "")) else "peer"


def address_in_network(address: str, network_value: str) -> bool:
    """True when ``address`` is a single host inside ``network_value`` (or equal to it).

    ``network_value`` may be one exact address (the preferred registration) or a
    CIDR range. A blank or malformed value never matches.
    """
    return _network_contains(parse_network(network_value), parse_address(address))
