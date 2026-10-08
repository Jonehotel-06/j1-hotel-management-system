# apps/core/formatting.py
"""Presentation-only formatting helpers for guest-facing documents/emails.

These functions never touch business logic — they take already-computed values
(Decimals, ISO strings, dates) and render them the way a Nigerian hotel guest
expects to read them:

* money  →  ``₦100,000.00``     (never ``100000.00 NGN``)
* dates  →  ``12 September 2026`` / ``12 Sep 2026, 7:09 PM``

They are deliberately tolerant of the string forms produced by the DRF
serializers (e.g. ``money()`` returns ``"100000.00"``) as well as raw Decimals,
ints and ISO-8601 timestamps, so callers can pass serialized receipt payloads
directly.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

# Currency symbols for the currencies this hotel actually transacts in. NGN is
# the only live currency, but keeping a small map means an unexpected value is
# rendered clearly (``USD 100.00``) instead of mislabelled as Naira.
_CURRENCY_SYMBOLS = {
    "NGN": "₦",
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
}


def format_money(value, currency="NGN"):
    """Render a monetary amount with grouping + 2dp, e.g. ``₦100,000.00``.

    Accepts Decimal, int, float or the serialized string form (``"100000.00"``).
    Unparseable input falls back to a safe ``<symbol>0.00`` rather than raising,
    because a receipt must never crash the delivery pipeline over formatting.
    """
    currency = (currency or "NGN").upper()
    symbol = _CURRENCY_SYMBOLS.get(currency)
    try:
        amount = Decimal(str(value if value not in (None, "") else "0")).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0.00")
    # Thousands separators with exactly two decimals.
    formatted = f"{amount:,.2f}"
    if symbol:
        return f"{symbol}{formatted}"
    # Unknown currency: keep the code visible so the amount is never ambiguous.
    return f"{currency} {formatted}"


def _parse_dt(value):
    """Best-effort parse of a date/datetime/ISO string into an aware datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if timezone.is_naive(dt):
        return dt
    # Present everything in the hotel's local timezone (Africa/Lagos).
    return timezone.localtime(dt)


def format_date(value):
    """Human date, e.g. ``14 September 2026``. Empty string if unparseable.

    Built from integer components + the (cross-platform) ``%B`` month name so
    it never relies on the glibc-only ``%-d`` directive, which raises
    ``ValueError: Invalid format string`` on Windows. Works identically on
    Windows development machines and hosted Linux production.
    """
    dt = _parse_dt(value)
    if dt is None:
        return ""
    return f"{dt.day} {dt.strftime('%B')} {dt.year}"


def format_datetime(value):
    """Human date + time, e.g. ``14 September 2026, 7:09 PM``.

    Like :func:`format_date`, the day and 12-hour clock are assembled from
    integer components so no platform-specific ``strftime`` directive
    (``%-d`` / ``%-I``) is ever used. ``%B`` (full month name) and ``%p``-style
    AM/PM are derived manually to stay correct on every platform and locale.
    """
    dt = _parse_dt(value)
    if dt is None:
        return ""
    hour_12 = dt.hour % 12 or 12          # 0/12 -> 12, 13 -> 1, ...
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{dt.day} {dt.strftime('%B')} {dt.year}, {hour_12}:{dt.minute:02d} {meridiem}"
