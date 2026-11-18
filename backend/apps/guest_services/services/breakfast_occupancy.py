"""Authoritative, room-level eligibility for the complimentary breakfast desk."""
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, When
from django.utils import timezone

from apps.bookings.models import Booking, BookingRoom, RoomOccupant
from apps.core.utils import hotel_date_bounds, hotel_today
from apps.stays.models import Stay, StayRoom


def eligible_room_queries(service_date):
    """Current physical occupancies, plus evidenced pre-Stay check-ins.

    A later reservation alone cannot qualify. When a Stay exists its active
    occupancy is authoritative; do not fall back to reserved rooms released by
    that stay. The supported booking assign-room action can re-point the linked
    BookingRoom in-place, so use that physical room when its assignment was
    edited after the current occupancy row was assigned.
    """
    today = hotel_today()
    if service_date > today:
        return StayRoom.objects.none(), BookingRoom.objects.none()
    _, next_day = hotel_date_bounds(service_date, service_date)
    now = timezone.now()
    modern = StayRoom.objects.filter(
        stay__status=Stay.Status.IN_HOUSE, stay__actual_check_out_at__isnull=True,
        stay__actual_check_in_at__lt=next_day, stay__actual_check_in_at__lte=now,
        stay__expected_departure__gte=today, stay__guest_id=F("stay__booking__guest_id"),
        released_at__isnull=True, actual_check_out_at__isnull=True,
        actual_check_in_at__lt=next_day, actual_check_in_at__lte=now,
    ).filter(Q(booking_room__isnull=True) | Q(booking_room__checked_in_at__isnull=False,
                                              booking_room__not_arrived_at__isnull=True,
                                              booking_room__booking_id=F("stay__booking_id")))
    modern = modern.annotate(physical_room=Case(
        When(booking_room__updated_at__gt=F("assigned_at"), then=F("booking_room__room__room_number")),
        default=F("room__room_number"), output_field=CharField(),
    ))
    legacy = BookingRoom.objects.filter(
        booking__stay__isnull=True, checked_in_at__lt=next_day, checked_in_at__lte=now,
        not_arrived_at__isnull=True, check_out__gte=today,
    ).annotate(physical_room=F("room__room_number"))
    return modern, legacy


def eligible_occupants(service_date):
    """Named occupants describe arrived rooms; they are not meal entitlements."""
    modern, legacy = eligible_room_queries(service_date)
    return RoomOccupant.objects.filter(
        Q(Exists(modern.filter(booking_room_id=OuterRef("booking_room_id"))))
        | Q(Exists(legacy.filter(pk=OuterRef("booking_room_id"))))
    )


def breakfast_bookings(service_date, *, match=None):
    modern, legacy = eligible_room_queries(service_date)
    modern = modern.filter(stay__booking_id=OuterRef("pk"))
    legacy = legacy.filter(booking_id=OuterRef("pk"))
    if match is not None:
        modern = modern.filter(physical_room__icontains=match)
        legacy = legacy.filter(physical_room__icontains=match)
    return Booking.objects.filter(
        Q(Exists(modern)) | Q(Exists(legacy)), status=Booking.Status.CHECKED_IN,
        checked_out_at__isnull=True, check_in__lte=service_date, check_out__gte=hotel_today(),
    )


def breakfast_room_numbers(booking_ids, service_date):
    if not booking_ids:
        return {}
    modern, legacy = eligible_room_queries(service_date)
    rows = list(modern.filter(stay__booking_id__in=booking_ids).order_by().values_list(
        "stay__booking_id", "physical_room",
    )) + list(legacy.filter(booking_id__in=booking_ids).order_by().values_list("booking_id", "physical_room"))
    rooms = {}
    for booking_id, number in rows:
        if number:
            rooms.setdefault(booking_id, set()).add(number)
    return {key: sorted(numbers) for key, numbers in rooms.items()}


def breakfast_occupant_context(booking_ids, service_date):
    if not booking_ids:
        return {}
    modern, _ = eligible_room_queries(service_date)
    numbers = dict(modern.filter(stay__booking_id__in=booking_ids, booking_room__isnull=False)
                   .values_list("booking_room_id", "physical_room"))
    by_booking = {}
    occupants = eligible_occupants(service_date).filter(booking_room__booking_id__in=booking_ids).select_related("booking_room__room")
    for occupant in occupants:
        if not occupant.full_name:
            continue
        assignment = occupant.booking_room
        key = occupant.email.lower() or f"occupant:{occupant.pk}"
        row = by_booking.setdefault(assignment.booking_id, {}).setdefault(key, {"name": occupant.full_name, "rooms": set()})
        row["rooms"].add(numbers.get(assignment.pk) or assignment.room.room_number)
    return {booking_id: [{**entry, "rooms": sorted(entry["rooms"])} for entry in rows.values()]
            for booking_id, rows in by_booking.items()}
