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


# Aging buckets now live with the receivable calculation itself, in
# ``guest_receivables_service.AGING_BUCKETS`` (batch 01, task 7).


def accounts_receivable_aging(*, as_of=None, limit=100, currency: str = "NGN") -> dict:
    """Aged guest receivables, taken from the guest-receivable control account.

    Batch 01, task 7. The previous implementation added ``Booking.amount_due``
    (a contract balance covering nights that have not happened yet) to a
    partial folio-charge map, clamped the result at zero, loaded every
    non-cancelled booking into Python, ignored ``as_of`` for the folio half,
    counted drafts and voided transactions, and labelled every currency as
    naira. It could not agree with the balance sheet and it silently deleted
    guest credit balances.

    This now reports the posted ledger: the same evidence the balance sheet
    uses, with a reconciliation block proving the two agree. The response keys
    the dashboard already consumes (``as_of``, ``currency``,
    ``total_outstanding``, ``buckets``, ``items``, ``items_truncated``,
    ``definitions``) are preserved; everything else is additive.
    """
    from .guest_receivables_service import AGING_BUCKETS, guest_receivables

    as_of = as_of or hotel_today()
    report = guest_receivables(as_of=as_of, currency=currency, limit=limit)
    labels = {
        "current": "Current (not yet due)",
        "days_1_30": "1–30 days",
        "days_31_60": "31–60 days",
        "days_61_90": "61–90 days",
        "days_over_90": "Over 90 days",
    }
    items = [
        {
            "booking_reference": row["booking_reference"],
            "guest": row["guest_name"],
            "folio_reference": row["folio_reference"],
            "stay_reference": row["stay_reference"],
            "rooms": row["rooms"],
            "check_in": row["check_in"],
            "check_out": row["check_out"],
            "status": row["booking_status"],
            "folio_status": row["folio_status"],
            "currency": row["currency"],
            "accommodation_charges": row["accommodation_charges"],
            "food_beverage_charges": row["food_beverage_charges"],
            "other_charges": row["other_charges"],
            "total_charges": row["total_charges"],
            "payments_allocated": row["payments_allocated"],
            "discounts": row["discounts"],
            "refunds": row["refunds"],
            "balance_due": row["outstanding"],
            "credit_balance": row["credit_balance"],
            "age_days": row["age_days"],
            "bucket": row["age_bucket"],
        }
        for row in report["items"]
    ]
    return {
        "as_of": report["as_of"],
        "currency": report["currency"],
        "total_outstanding": report["total_outstanding"],
        "gross_receivables": report["gross_receivables"],
        "guest_credit_balances": report["guest_credit_balances"],
        "unassigned_receivables": report["unassigned_receivables"],
        "unapplied_guest_deposits": report["unapplied_guest_deposits"],
        "net_receivable": report["net_receivable"],
        "folios_owing": report["folios_owing"],
        "folios_in_credit": report["folios_in_credit"],
        "stay_value_not_yet_charged": report["stay_value_not_yet_charged"],
        "bookings_not_yet_charged": report["bookings_not_yet_charged"],
        "total_stay_exposure": report["total_stay_exposure"],
        "buckets": [
            {
                "key": key,
                "label": labels.get(key, key),
                "total": report["buckets"][key],
                "count": report["bucket_counts"][key],
            }
            for key, _low, _high in AGING_BUCKETS
        ],
        "items": items,
        "by_booking": report["by_booking"],
        "items_truncated": report["items_truncated"],
        "reconciliation": report["reconciliation"],
        "other_currency_balances": report["other_currency_balances"],
        "definitions": {
            "balance_due": (
                "Debit balance of the guest's folio on ASSET.GUEST_RECEIVABLE at the as-of date, from "
                "posted transactions only. Charges actually raised, less payments actually applied; "
                "nights not yet posted are not a receivable."
            ),
            "credit_balance": (
                "Credit balance on the same folio — an advance or overpayment the hotel holds. Reported, "
                "never clamped to zero and never netted against another guest."
            ),
            "age_basis": "Days since arrival (or folio opening when there is no booking); future arrivals age as current.",
            **report["definitions"],
        },
    }
