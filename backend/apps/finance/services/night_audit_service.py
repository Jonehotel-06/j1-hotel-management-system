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

from ..models import CashMovement, CashSession, FinancialLine, FinancialTransaction, Folio, FolioPosting
from . import accounting

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
    and occupied-room versus billed-stay exceptions."""
    from apps.bookings.models import BookingRoom
    from apps.bookings.services.availability import blocking_booking_q
    from apps.pos.models import PosTender
    from apps.rooms.models import Room
    from apps.stays.models import Stay

    business_date = business_date or hotel_today()
    day_start, day_end = hotel_date_bounds(business_date, business_date)

    revenue_by_account = _ledger_revenue(business_date)
    accommodation_revenue = _dec(revenue_by_account.get(accounting.ACCOMMODATION_REVENUE))
    total_revenue = _dec(sum(revenue_by_account.values(), ZERO))

    active_rooms = Room.objects.filter(is_active=True).count()
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

    sessions = (
        CashSession.objects.filter(business_date=business_date)
        .select_related("cashier")
        .order_by("opened_at", "pk")
    )
    drops = {
        row["cash_session_id"]: _dec(row["total"])
        for row in CashMovement.objects.filter(
            cash_session__in=sessions, type=CashMovement.Type.SAFE_DROP
        ).values("cash_session_id").annotate(total=Sum("amount"))
    }
    drawer_rows, discrepancies = [], []
    for session in sessions:
        counted = _dec(session.counted_cash) if session.counted_cash is not None else None
        variance = _dec(session.variance) if session.variance is not None else None
        row = {
            "reference": session.reference,
            "cashier": (session.cashier.full_name or session.cashier.email) if session.cashier_id else "",
            "location": session.location,
            "status": session.status,
            "opening_float": money(session.opening_float),
            "safe_drops": money(drops.get(session.pk, ZERO)),
            "expected_cash": money(session.expected_cash),
            "counted_cash": money(counted) if counted is not None else None,
            "variance": money(variance) if variance is not None else None,
        }
        drawer_rows.append(row)
        if variance is not None and variance != ZERO:
            discrepancies.append({
                "type": "CASH_VARIANCE",
                "reference": session.reference,
                "detail": f"Drawer counted {money(counted)} against expected {money(session.expected_cash)} (variance {money(variance)}).",
            })
        if session.status == CashSession.Status.OPEN and session.business_date < hotel_today():
            discrepancies.append({
                "type": "DRAWER_STILL_OPEN",
                "reference": session.reference,
                "detail": "Cash session from this business day is still open.",
            })

    in_house = list(
        Stay.objects.filter(status=Stay.Status.IN_HOUSE)
        .select_related("booking", "guest")
        .prefetch_related("stay_rooms__room")
    )
    def _stay_room(stay):
        for occupancy in stay.stay_rooms.all():
            if occupancy.released_at is None:
                return occupancy.room
        return None

    in_house_room_ids = {
        room.pk for stay in in_house if (room := _stay_room(stay)) is not None
    }
    for room_id in sorted(occupied_room_ids - in_house_room_ids):
        room = Room.objects.filter(pk=room_id).first()
        discrepancies.append({
            "type": "OCCUPIED_NOT_IN_HOUSE",
            "reference": getattr(room, "room_number", str(room_id)),
            "detail": "Room is occupied by a blocking booking tonight but has no in-house stay.",
        })

    folio_balances = {
        row["folio_id"]: (
            Decimal(row["debits"] or 0) - Decimal(row["credits"] or 0)
        ).quantize(CENT)
        for row in FolioPosting.objects.filter(folio__stay__in=[s.pk for s in in_house], folio__status=Folio.Status.OPEN)
        .values("folio_id")
        .annotate(
            debits=Sum("amount", filter=Q(effect=FolioPosting.Effect.DEBIT)),
            credits=Sum("amount", filter=Q(effect=FolioPosting.Effect.CREDIT)),
        )
    } if in_house else {}
    unbilled = []
    stay_folios = {
        row["stay_id"]: row["id"]
        for row in Folio.objects.filter(stay__in=[s.pk for s in in_house], status=Folio.Status.OPEN).values("id", "stay_id")
    } if in_house else {}
    for stay in in_house:
        folio_id = stay_folios.get(stay.pk)
        balance = folio_balances.get(folio_id, ZERO) if folio_id else ZERO
        if balance > ZERO:
            room = _stay_room(stay)
            unbilled.append({
                "stay_reference": stay.reference,
                "guest": stay.guest.full_name if stay.guest_id else "",
                "room": room.room_number if room is not None else "",
                "folio_balance": money(balance),
            })
        if folio_id is None:
            discrepancies.append({
                "type": "IN_HOUSE_NO_FOLIO",
                "reference": stay.reference,
                "detail": "In-house stay has no open folio.",
            })

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
        "in_house_count": len(in_house),
        "unbilled_in_house_folios": unbilled,
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

    as_of = as_of or hotel_today()
    outstanding = (
        Booking.objects.exclude(status=Booking.Status.CANCELLED)
        .filter(payment_status__in=[Booking.PaymentStatus.UNPAID, Booking.PaymentStatus.PARTIALLY_PAID])
        .select_related("guest")
        .order_by("check_in", "pk")
    )
    buckets = {key: {"label": label, "total": ZERO, "count": 0} for key, label, _low, _high in BUCKETS}
    items = []
    total = ZERO
    for booking in outstanding.iterator():
        due = _dec(booking.amount_due)
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
            "balance_due": "Booking total minus payments, from the booking record (refunds already reflected).",
            "age_basis": "Days since check-in; future check-ins age as current.",
        },
    }
