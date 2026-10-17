# apps/reports/services/reports.py
"""Report computations (revenue, occupancy, bookings).

Revenue is computed purely via DB aggregation over Payment rows.
Occupancy is computed per calendar day from room assignments for
inventory-active bookings within the requested window (bounded ranges).
"""
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate

from apps.bookings.models import Booking, BookingRoom
from apps.core.utils import hotel_date_bounds, money
from apps.payments.models import Payment
from apps.rooms.models import Room, RoomType
from apps.bookings.services.availability import blocking_booking_q

MAX_RANGE_DAYS = 366


def _validate_range(start_date, end_date):
    if start_date is None or end_date is None:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"detail": "start_date and end_date are required (YYYY-MM-DD)."})
    if end_date < start_date:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"end_date": ["Must be on or after start_date."]})
    if (end_date - start_date).days > MAX_RANGE_DAYS:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"detail": f"Date range cannot exceed {MAX_RANGE_DAYS} days."})
    return start_date, end_date


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------
def revenue_report(start_date, end_date):
    _validate_range(start_date, end_date)
    range_start, range_end = hotel_date_bounds(start_date, end_date)
    # Half-open datetime bounds preserve local calendar-day semantics without
    # applying DATE()/a cast to paid_at, so the status + paid_at index is usable.
    payments = Payment.objects.filter(
        status=Payment.Status.SUCCESS, paid_at__gte=range_start, paid_at__lt=range_end
    )

    totals = payments.aggregate(total=Sum("amount"), count=Count("id"))
    by_day = list(
        payments.annotate(day=TruncDate("paid_at"))
        .values("day")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("day")
    )
    by_provider = list(
        payments.values("provider").annotate(total=Sum("amount"), count=Count("id")).order_by("provider")
    )
    refunds = Booking.objects.filter(
        cancelled_at__gte=range_start,
        cancelled_at__lt=range_end,
        refund_amount__gt=0,
    ).aggregate(total=Sum("refund_amount"), count=Count("id"))

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "currency": "NGN",
        "grand_total": money(totals["total"] or 0),
        "transactions": totals["count"],
        "refunds": {"total": money(refunds["total"] or 0), "count": refunds["count"]},
        "by_day": [
            {"date": row["day"].isoformat(), "total": money(row["total"]), "transactions": row["count"]}
            for row in by_day
        ],
        "by_provider": [
            {"provider": row["provider"], "total": money(row["total"]), "transactions": row["count"]}
            for row in by_provider
        ],
    }


# ---------------------------------------------------------------------------
# Occupancy
# ---------------------------------------------------------------------------
def _hotel_local_date(value):
    """A timestamp as the hotel's calendar date (occupancy is per hotel night)."""
    if value is None:
        return None
    from django.utils import timezone as django_timezone

    return django_timezone.localtime(value).date()


def occupancy_report(start_date, end_date):
    """Rooms sold per night, plus the rooms a guest actually slept in.

    Two different questions, deliberately reported together:

    * ``occupied_rooms`` — rooms sold for that night. This is the commercial
      definition of occupancy and is unchanged; a paid reservation counts even
      if the guest has not reached the desk yet.
    * ``arrived_rooms`` — rooms whose guest had been checked in by that night,
      from the per-room check-in timestamp. A three-room booking that arrives
      one room at a time therefore shows one occupied guest room until the
      others come in.
    * ``unarrived_rooms`` — rooms whose booking closed without that room ever
      being handed over. These are the no-shows the hotel still paid for.

    The per-room timestamps are recorded by the front desk, so this is evidence,
    not an assumption: a room nobody arrived in is never counted as slept-in.
    """
    _validate_range(start_date, end_date)
    days = (end_date - start_date).days + 1
    active_rooms = Room.objects.filter(is_active=True).count()

    assignments = (
        BookingRoom.objects.filter(check_in__lte=end_date, check_out__gt=start_date)
        .filter(blocking_booking_q(prefix="booking"))
        .select_related("room")
        .values("room_id", "check_in", "check_out", "checked_in_at", "not_arrived_at")
    )
    days_list = [start_date + timedelta(days=i) for i in range(days)]
    per_day_occupied = {day: set() for day in days_list}
    per_day_arrived = {day: set() for day in days_list}
    per_day_unarrived = {day: set() for day in days_list}
    for row in assignments:
        arrived_on = _hotel_local_date(row["checked_in_at"])
        never_arrived = row["not_arrived_at"] is not None
        day = max(row["check_in"], start_date)
        stop = min(row["check_out"] - timedelta(days=1), end_date)
        while day <= stop:
            per_day_occupied[day].add(row["room_id"])
            if never_arrived:
                per_day_unarrived[day].add(row["room_id"])
            elif arrived_on is not None and arrived_on <= day:
                per_day_arrived[day].add(row["room_id"])
            day += timedelta(days=1)

    series, occupied_room_nights, arrived_room_nights, unarrived_room_nights = [], 0, 0, 0
    for day in days_list:
        count = len(per_day_occupied[day])
        arrived = len(per_day_arrived[day])
        unarrived = len(per_day_unarrived[day])
        occupied_room_nights += count
        arrived_room_nights += arrived
        unarrived_room_nights += unarrived
        series.append({
            "date": day.isoformat(),
            "occupied_rooms": count,
            "arrived_rooms": arrived,
            "unarrived_rooms": unarrived,
            "occupancy_rate_percent": round((count / active_rooms) * 100, 1) if active_rooms else 0.0,
            "arrival_rate_percent": round((arrived / count) * 100, 1) if count else 0.0,
        })

    total_room_nights = active_rooms * days
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "definitions": {
            "occupied_rooms": "Rooms sold for the night (a paid reservation counts even before the guest reaches the desk).",
            "arrived_rooms": "Rooms whose guest had been checked in by that night, per the front desk's per-room check-in record.",
            "unarrived_rooms": "Rooms on a booking that closed without that room ever being handed over (no-show).",
        },
        "days": days,
        "active_rooms": active_rooms,
        "occupied_room_nights": occupied_room_nights,
        "arrived_room_nights": arrived_room_nights,
        "unarrived_room_nights": unarrived_room_nights,
        "available_room_nights": total_room_nights,
        "average_occupancy_percent": round((occupied_room_nights / total_room_nights) * 100, 1)
        if total_room_nights else 0.0,
        "average_arrival_percent": round((arrived_room_nights / occupied_room_nights) * 100, 1)
        if occupied_room_nights else 0.0,
        "by_day": series,
    }


# ---------------------------------------------------------------------------
# Bookings overview
# ---------------------------------------------------------------------------
def bookings_report(start_date, end_date):
    _validate_range(start_date, end_date)
    bookings = Booking.objects.filter(
        check_in__gte=start_date, check_in__lte=end_date
    )
    by_status = list(bookings.values("status").annotate(count=Count("id")).order_by("status"))
    by_payment_status = list(
        bookings.values("payment_status").annotate(count=Count("id")).order_by("payment_status")
    )
    by_room_type = list(
        bookings.filter(
            status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT)
        )
        .values("room_type__name")
        .annotate(count=Count("id"), revenue=Sum("total_amount"))
        .order_by("-revenue")
    )
    total = bookings.count()
    cancelled = bookings.filter(status=Booking.Status.CANCELLED).count()
    no_shows = bookings.filter(status=Booking.Status.NO_SHOW).count()
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_bookings": total,
        "cancelled": cancelled,
        "no_shows": no_shows,
        "cancellation_rate_percent": round((cancelled / total) * 100, 1) if total else 0.0,
        "by_status": [{"status": row["status"], "count": row["count"]} for row in by_status],
        "by_payment_status": [
            {"payment_status": row["payment_status"], "count": row["count"]} for row in by_payment_status
        ],
        "by_room_type": [
            {
                "room_type": row["room_type__name"],
                "bookings": row["count"],
                "revenue": money(row["revenue"] or 0),
            }
            for row in by_room_type
        ],
    }
