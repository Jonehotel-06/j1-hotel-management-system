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


# ---------------------------------------------------------------------------
# Staff display names (targeted fixes 01 — tasks 10 and 11)
#
# Two different screens reported an employee by an INTERNAL identifier where a
# person needed to read a name:
#
#   * payroll validation said "Compensation is missing for:
#     STF-20261028-169B2A646559, ..."                     (staff code)
#   * Restaurant/Bar receipts said "Cashier: waiter@jonehotels.com"
#                                                        (e-mail address)
#
# This helper is the single, authoritative way to turn a staff account into the
# name a human should see. It reads ONLY the registered name on the staff
# record — never the e-mail address, never the primary key, and it never
# invents or guesses anything. Internal identifiers are untouched: the audit
# trail, payroll lines and transaction ownership keep using them.
# ---------------------------------------------------------------------------
STAFF_NAME_UNAVAILABLE = "Name unavailable"


def staff_person_name(user) -> str:
    """The registered first/middle/last name of a staff account.

    Returns ``""`` (empty) when the account genuinely has no usable name, so
    each caller can apply its own fallback. An e-mail address is deliberately
    NOT a name and is never returned here.
    """
    if user is None:
        return ""
    parts = [
        getattr(user, "first_name", "") or "",
        getattr(user, "middle_name", "") or "",
        getattr(user, "last_name", "") or "",
    ]
    return " ".join(part.strip() for part in parts if str(part).strip()).strip()


def staff_display_name(user, *, department: str = "", fallback: str = "") -> str:
    """A staff account's name for a user-facing message or a printed receipt.

    ``department`` is appended in brackets when supplied, so two employees with
    the same name can be told apart. ``fallback`` is used only when the record
    has no name at all; callers on a receipt pass ``""`` so the receipt simply
    omits the line rather than printing "Name unavailable" to a customer.
    """
    name = staff_person_name(user)
    if not name:
        return fallback
    department = str(department or "").strip()
    return f"{name} ({department})" if department else name


def staff_reference(user) -> str:
    """The internal identifier a receipt/message may fall back to.

    Employee code first (an administrator recognises it), then the account
    reference; never the e-mail address.
    """
    profile = getattr(user, "staff_profile", None)
    code = str(getattr(profile, "employee_code", "") or "").strip()
    if code:
        return code
    return str(getattr(user, "pk", "") or "")


def money(value) -> str:
    """Canonical API representation of a Decimal money amount."""
    from decimal import Decimal

    if value is None:
        return None
    return f"{Decimal(value).quantize(Decimal('0.01'))}"
