"""Authoritative answer to "may this verified email use the guest portal right now?"

Portal eligibility is derived ONLY from operational stay state, read from the
database on every decision:

* the Stay is ``IN_HOUSE`` and its booking is neither cancelled nor checked out, and
* the verified address is either the booking contact (who sees every room of the
  stay that is still actively occupied) or a recorded occupant of a room that is
  still actively assigned to that stay.

A booking existing, a guest account existing, an old link, or an old browser
session never grants access by itself. Checkout releases every ``StayRoom`` and
moves the ``Stay`` to ``CHECKED_OUT`` in the same transaction, so eligibility ends
at the moment checkout commits.

A single email may hold several eligible stays (for example two bookings in
house at once). Callers scope every read/write to ``eligible_stay_ids_for_email``
so that completing one stay never removes access to another, independent one.
"""
from __future__ import annotations

from django.db.models import Q

from apps.bookings.models import Booking
from apps.stays.models import Stay, StayRoom

# Booking states that never grant portal access, even if a stay row still looks
# in house because of an inconsistent or interrupted operation.
INELIGIBLE_BOOKING_STATUSES = (Booking.Status.CHECKED_OUT, Booking.Status.CANCELLED)


def normalize_eligibility_email(email: str) -> str:
    return str(email or "").strip().lower()


def active_stay_rooms_for_email(email: str):
    """Active room occupancies this verified address may act for.

    Contact: every active room of an in-house stay for the booking they hold.
    Occupant: only the active room(s) on which their address is recorded.
    """
    email = normalize_eligibility_email(email)
    if not email:
        return StayRoom.objects.none()
    return (
        StayRoom.objects.filter(
            released_at__isnull=True,
            stay__status=Stay.Status.IN_HOUSE,
        )
        .exclude(stay__booking__status__in=INELIGIBLE_BOOKING_STATUSES)
        .filter(
            Q(stay__guest__email__iexact=email)
            | Q(booking_room__occupants__email__iexact=email)
        )
    )


def eligible_stay_ids_for_email(email: str) -> set[int]:
    """Primary keys of every in-house stay this verified address may use now."""
    return set(active_stay_rooms_for_email(email).values_list("stay_id", flat=True).distinct())


def has_active_portal_stay(email: str) -> bool:
    return active_stay_rooms_for_email(email).exists()


def guest_emails_for_stay(stay) -> set[str]:
    """Every address that could hold a portal identity for this stay's booking."""
    from apps.bookings.models import RoomOccupant

    emails = set()
    contact = normalize_eligibility_email(getattr(stay.guest, "email", ""))
    if contact:
        emails.add(contact)
    occupant_emails = (
        RoomOccupant.objects.filter(booking_room__booking_id=stay.booking_id)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    emails.update(normalize_eligibility_email(value) for value in occupant_emails)
    emails.discard("")
    return emails
