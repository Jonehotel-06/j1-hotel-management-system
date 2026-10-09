"""Receptionist Desktop sign-in policy.

Operational staff may sign in only from the Receptionist Desktop: an active
``Workstation`` in the FRONT_DESK department that has been issued a desktop
key. The desktop presents that key with the sign-in request in the
``X-JONE-Desktop-Key`` header. The check runs on the server after the password
has been verified, so a crafted client, a direct API call, or a copied browser
form cannot bypass it without the key.

Management roles keep their existing access from any device (see
``STAFF_SIGN_IN_EXEMPT_ROLES``). Guest accounts, token refresh, logout and the
password-reset flows are not affected, so active sessions continue to work.

The key is a bearer secret for the desk browser, not a cryptographic device
attestation. It is hashed at rest, never returned after issue, and can be
rotated or revoked by a holder of ``terminal.manage``.
"""
from __future__ import annotations

import hashlib
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


def presented_desktop_key(request) -> str:
    if request is None:
        return ""
    return str(request.META.get(DESKTOP_HEADER, "") or "")
