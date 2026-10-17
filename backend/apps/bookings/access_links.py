# apps/bookings/access_links.py
"""Signed links that let a guest (re)claim a booking from any device.

Why this exists
---------------
A guest booking is protected by a 256-bit bearer token that is handed to the
browser once, at checkout, and only its SHA-256 digest is stored. That is the
right model for the session — but it means the token lives in ONE browser's
storage. A guest who opens the reservation from their email on a phone, clears
site data, or returns from the payment provider on another device has no
credential, and every booking endpoint correctly answers 404. The guest is then
stuck: the booking exists and is payable, but nothing on the page can prove it.

The emailed link therefore carries a *signed* access value (Django's signer,
bound to the booking reference and expiring) that can be exchanged exactly
once per page load for a normal guest token. Possession of the emailed link is
the credential — the same trust level as the reference+token link the system
already emails, but now usable on any device and revocable by expiry.

The signature is generated with ``SECRET_KEY``, so it cannot be forged or
enumerated, and it is bound to the reference so a link for booking A can never
unlock booking B.
"""
from __future__ import annotations

from django.conf import settings
from django.core import signing

SALT = "bookings.guest-access-link"
DEFAULT_MAX_AGE_SECONDS = 14 * 24 * 60 * 60  # two weeks


def access_link_max_age() -> int:
    """Seconds an emailed access link stays usable.

    Read from settings so a deployment can tune it (and tests can shorten it)
    without touching this module.
    """
    return int(getattr(settings, "BOOKING_ACCESS_LINK_MAX_AGE", DEFAULT_MAX_AGE_SECONDS))


def issue_access_link_token(booking) -> str:
    """Return the signed ``access`` value for one booking's emailed link."""
    return signing.dumps({"ref": str(booking.booking_reference)}, salt=SALT, compress=True)


def read_access_link_token(value, reference) -> bool:
    """True only for an untampered, unexpired token issued for this reference."""
    if not value:
        return False
    try:
        payload = signing.loads(
            str(value), salt=SALT, max_age=access_link_max_age()
        )
    except signing.BadSignature:
        # Covers SignatureExpired (a BadSignature subclass), a tampered value,
        # and a value signed with a different SECRET_KEY (e.g. after a key
        # rotation) — all indistinguishable to the caller, which is correct.
        return False
    except Exception:  # pragma: no cover - defensive: never 500 on a bad link
        return False
    return str(payload.get("ref") or "") == str(reference or "")
