"""Night audit, daily flash and accounts-receivable aging.

Read-only reporting projections over the existing authoritative records:
the posted double-entry ledger, POS tenders, cash sessions, stays, folios
and bookings. Nothing here posts, mutates or revalues financial records.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.utils import hotel_date_bounds, hotel_today, money

from ..models import FinancialLine, FinancialTransaction
from . import accounting
from .reconciliation_service import SEVERITY_ERROR, collect_checks

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _dec(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _ledger_revenue(business_date):
    """Net recognized revenue per ledger account for one business day."""
    rows = (
        FinancialLine.objects.filter(
            transaction__status=FinancialTransaction.Status.POSTED,
            transaction__business_date=business_date,
        )
        .values("account_code", "direction")
        .annotate(total=Sum("amount"))
    )
    net: dict[str, Decimal] = {}
    for row in rows:
        code = row["account_code"]
        amount = Decimal(row["total"] or 0)
        if code.startswith("REVENUE."):
            signed = amount if row["direction"] == FinancialLine.Direction.CREDIT else -amount
        elif code.startswith("CONTRA_REVENUE."):
            signed = -amount if row["direction"] == FinancialLine.Direction.DEBIT else amount
        else:
            continue
        net[code] = net.get(code, ZERO) + signed
    return net


def daily_reconciliation(*, business_date=None) -> dict:
    """One business day's night-audit pack: flash metrics, tenders, drawers,
    and the reconciliation checks for that day.

    The checks themselves are owned by ``reconciliation_service``: one record
    per condition, with the object it belongs to, the amount, why it is flagged
    and what to do next. This function keeps the historic response shape
    (``cash_drawers``, ``discrepancies``, ``unbilled_in_house_folios``) so no
    existing caller breaks, and adds ``checks`` and ``check_summary`` so the
    dashboard can show the underlying records instead of a bare count.

    ``discrepancies`` stays what it always was — one row per condition that
    needs a person — but it is now built from the checks, so the drawer, room
    and billing subjects can be counted separately and a reviewed condition
    carries its review state instead of looking untouched.
    """
    from apps.pos.models import PosTender
    from apps.rooms.models import Room

    business_date = business_date or hotel_today()
    day_start, day_end = hotel_date_bounds(business_date, business_date)

    revenue_by_account = _ledger_revenue(business_date)
    accommodation_revenue = _dec(revenue_by_account.get(accounting.ACCOMMODATION_REVENUE))
    total_revenue = _dec(sum(revenue_by_account.values(), ZERO))
    active_rooms = Room.objects.filter(is_active=True).count()

    pack = collect_checks(business_date=business_date)
    checks = pack["checks"]
    summary = pack["summary"]
    drawer_rows = pack["cash_drawers"]

    # Occupancy is read from the same blocking-booking set the room checks use,
    # so the occupancy figures and the room checks can never disagree.
    from apps.bookings.models import BookingRoom
    from apps.bookings.services.availability import blocking_booking_q

    occupied_room_ids = set(
        BookingRoom.objects.filter(check_in__lte=business_date, check_out__gt=business_date)
        .filter(blocking_booking_q(prefix="booking"))
        .values_list("room_id", flat=True)
    )
    occupied_rooms = len(occupied_room_ids)
    adr = _dec(accommodation_revenue / occupied_rooms) if occupied_rooms else None
    revpar = _dec(accommodation_revenue / active_rooms) if active_rooms else None
    occupancy_percent = round((occupied_rooms / active_rooms) * 100, 1) if active_rooms else 0.0

    tenders = {row["method"]: _dec(row["total"]) for row in (
        PosTender.objects.filter(status=PosTender.Status.CAPTURED, captured_at__gte=day_start, captured_at__lt=day_end)
        .values("method").annotate(total=Sum("amount"))
    )}
    tender_refunds = {row["method"]: _dec(row["total"]) for row in (
        PosTender.objects.filter(status=PosTender.Status.REFUNDED, captured_at__gte=day_start, captured_at__lt=day_end)
        .values("method").annotate(total=Sum("amount"))
    )}

    discrepancies = [
        {
            "type": check["type"],
            "reference": check["reference"],
            # Kept for callers that render one sentence per row.
            "detail": check["why_flagged"],
            "check_id": check["check_id"],
            "category": check["category"],
            "severity": check["severity"],
            "status": check["status"],
            "amount": check["amount"],
        }
        for check in checks
        if check["severity"] == SEVERITY_ERROR and check["status"] in {"OPEN", "ACKNOWLEDGED"}
    ]

    return {
        "business_date": business_date.isoformat(),
        "currency": "NGN",
        "daily_flash": {
            "total_revenue": money(total_revenue),
            "accommodation_revenue": money(accommodation_revenue),
            "occupied_rooms": occupied_rooms,
            "available_rooms": active_rooms,
            "occupancy_percent": occupancy_percent,
            "adr": money(adr) if adr is not None else None,
            "revpar": money(revpar) if revpar is not None else None,
            "adr_basis": "Accommodation ledger revenue for the day divided by occupied rooms.",
            "revpar_basis": "Accommodation ledger revenue for the day divided by available rooms.",
        },
        "revenue_by_account": [
            {"account_code": code, "amount": money(amount)}
            for code, amount in sorted(revenue_by_account.items())
        ],
        "tenders": {
            "captured": {method: money(amount) for method, amount in sorted(tenders.items())},
            "refunded": {method: money(amount) for method, amount in sorted(tender_refunds.items())},
            "captured_total": money(sum(tenders.values(), ZERO)),
            "refunded_total": money(sum(tender_refunds.values(), ZERO)),
        },
        "cash_drawers": drawer_rows,
        "discrepancies": discrepancies,
        # Full drill-down payload: the records, not a count of them.
        "checks": checks,
        "check_summary": summary,
        "resolved_reviews": pack["resolved_reviews"],
        "in_house_count": summary["in_house_count"],
        "unbilled_in_house_folios": pack["unbilled_in_house_folios"],
        "generated_at": timezone.now().isoformat(),
    }


BUCKETS = (
    ("current", "Current (0–30 days)", 0, 30),
    ("days_31_60", "31–60 days", 31, 60),
    ("days_61_90", "61–90 days", 61, 90),
    ("over_90", "Over 90 days", 91, None),
)


def accounts_receivable_aging(*, as_of=None, limit=100) -> dict:
    """Outstanding guest balances from bookings, aged from check-in.

    A booking's receivable is its authoritative ``amount_due``; cancelled
    bookings never owed the hotel money and are excluded.
    """
    from apps.bookings.models import Booking
    from .folio_service import booking_folio_charge_map

    as_of = as_of or hotel_today()
    outstanding = (
        Booking.objects.exclude(status=Booking.Status.CANCELLED)
        .filter(check_in__lte=as_of)
        .select_related("guest")
        .order_by("check_in", "pk")
    )
    buckets = {key: {"label": label, "total": ZERO, "count": 0} for key, label, _low, _high in BUCKETS}
    items = []
    total = ZERO
    # Fetch the bounded booking cohort once; one aggregate covers every folio,
    # including multiple room-level folios belonging to the same booking.
    bookings = list(outstanding)
    extras = booking_folio_charge_map(booking_ids=[b.pk for b in bookings])
    for booking in bookings:
        due = max(_dec(booking.amount_due) + _dec(extras.get(booking.pk)), ZERO)
        if due <= ZERO:
            continue
        age_days = max((as_of - (booking.check_in or booking.created_at.date())).days, 0)
        bucket_key = next(
            key for key, _label, low, high in BUCKETS if age_days >= low and (high is None or age_days <= high)
        )
        buckets[bucket_key]["total"] += due
        buckets[bucket_key]["count"] += 1
        total += due
        items.append({
            "booking_reference": booking.booking_reference,
            "guest": booking.guest.full_name if booking.guest_id else "",
            "check_in": booking.check_in.isoformat() if booking.check_in else None,
            "check_out": booking.check_out.isoformat() if booking.check_out else None,
            "status": booking.status,
            "payment_status": booking.payment_status,
            "total_amount": money(booking.total_amount),
            "amount_paid": money(booking.amount_paid),
            "balance_due": money(due),
            "folio_charges_net": money(extras.get(booking.pk, ZERO)),
            "age_days": age_days,
            "bucket": bucket_key,
        })
    items.sort(key=lambda row: (-Decimal(row["balance_due"]), row["age_days"]))
    return {
        "as_of": as_of.isoformat(),
        "currency": "NGN",
        "total_outstanding": money(total),
        "buckets": [
            {"key": key, "label": data["label"], "total": money(data["total"]), "count": data["count"]}
            for key, data in buckets.items()
        ],
        "items": items[:limit],
        "items_truncated": len(items) > limit,
        "definitions": {
            "balance_due": "Booking total minus payments plus posted net non-accommodation folio charges across all room folios; no future bookings.",
            "age_basis": "Days since check-in; future check-ins age as current.",
        },
    }
