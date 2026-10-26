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

Route 2 and route 3 need a trustworthy client address, which behind a managed
host is only available from a forwarding header. That header is read
exclusively from a trusted proxy hop — see ``apps.core.client_ip`` — so a client
cannot vote for its own address.

PRECEDENCE (one consistent rule; the first match wins, and the two registration
sources can no longer contradict each other silently):

1. a presented desktop key that resolves to an active FRONT_DESK workstation;
2. the request arriving from an address registered on an active FRONT_DESK
   workstation (``Workstation.ip_address``);
3. the request arriving from an address/network configured in
   ``HotelSettings.front_desk_desktop_ip``, while at least one active FRONT_DESK
   workstation exists;
4. otherwise the request is outside the Receptionist Desktop.

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
