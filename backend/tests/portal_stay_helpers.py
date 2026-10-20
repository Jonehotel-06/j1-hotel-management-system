"""Shared fixtures for portal tests: a booking that is genuinely IN HOUSE.

Portal eligibility is derived from live stay state (see apps/portal/eligibility.py),
so tests must create the same operational records a real check-in produces.
"""
from django.utils import timezone

from apps.bookings.models import BookingRoom, RoomOccupant
from apps.stays.models import Stay, StayRoom


def make_in_house_stay(booking, *, guest=None, rooms=None, occupants=None):
    """Create an IN_HOUSE stay with active room occupancy for ``booking``.

    ``occupants`` maps a room (Room instance) to an email for a room guest.
    """
    rooms = list(rooms if rooms is not None else booking.room_assignments.all().values_list("room", flat=True))
    stay = Stay.objects.create(
        reference=f"STY-{booking.booking_reference}",
        booking=booking,
        guest=guest or booking.guest,
        status=Stay.Status.IN_HOUSE,
        expected_arrival=booking.check_in,
        expected_departure=booking.check_out,
    )
    now = timezone.now()
    for room in rooms:
        from apps.rooms.models import Room

        room_obj = room if isinstance(room, Room) else Room.objects.get(pk=room)
        booking_room = BookingRoom.objects.filter(booking=booking, room=room_obj).first()
        if booking_room is None:
            booking_room = BookingRoom.objects.create(
                booking=booking, room=room_obj, check_in=booking.check_in, check_out=booking.check_out,
            )
        booking_room.checked_in_at = now
        booking_room.save(update_fields=["checked_in_at", "updated_at"])
        StayRoom.objects.create(stay=stay, room=room_obj, booking_room=booking_room, actual_check_in_at=now)
    for room_obj, email in (occupants or {}).items():
        booking_room = BookingRoom.objects.get(booking=booking, room=room_obj)
        RoomOccupant.objects.create(booking_room=booking_room, email=email, full_name="Room Guest")
    return stay
