# apps/core/utils.py
"""Small shared helpers."""
import secrets
from datetime import date, datetime, time, timedelta

from django.utils import timezone


def hotel_today() -> date:
    """'Today' in the hotel's local timezone (Africa/Lagos)."""
    return timezone.localdate()


def combine_hotel_datetime(day: date, at: time) -> datetime:
    """Attach the hotel-local wall-clock time to a booking date."""
    value = datetime.combine(day, at)
    return timezone.make_aware(value) if timezone.is_aware(timezone.now()) else value


def hotel_date_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Return hotel-local ``[start, day-after-end)`` bounds for a date range.

    Filtering a ``DateTimeField`` with ``field__date`` makes MySQL apply a
    function to the indexed column. These bounds preserve the same inclusive
    calendar-day meaning while allowing a normal range index scan.
    """
    return (
        combine_hotel_datetime(start_date, time.min),
        combine_hotel_datetime(end_date + timedelta(days=1), time.min),
    )


def generate_booking_reference() -> str:
    """Human-friendly, unguessable, collision-checked callerside."""
    return f"J1-{timezone.now():%Y%m%d}-{secrets.token_hex(4).upper()}"


def generate_payment_reference() -> str:
    return f"J1P-{timezone.now():%Y%m%d}-{secrets.token_hex(5).upper()}"


def generate_cancellation_reference() -> str:
    return f"J1C-{timezone.now():%Y%m%d}-{secrets.token_hex(5).upper()}"


def money(value) -> str:
    """Canonical API representation of a Decimal money amount."""
    from decimal import Decimal

    if value is None:
        return None
    return f"{Decimal(value).quantize(Decimal('0.01'))}"
