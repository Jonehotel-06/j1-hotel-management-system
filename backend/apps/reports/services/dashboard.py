# apps/reports/services/dashboard.py
"""Staff dashboard aggregation.

Everything is computed with database-level aggregates — the endpoint does a
handful of queries regardless of table size, and financial figures are only
included for roles allowed to see them.
"""
from datetime import timedelta

from django.db.models import Count, F, Q, Sum, Prefetch
from django.utils import timezone

from apps.bookings.models import Booking, BookingRoom
from apps.bookings.services import availability
from apps.core.utils import hotel_date_bounds, hotel_today, money
from apps.notifications.services import unread_count
from apps.payments.models import Payment
from apps.hotel.models import HotelSettings
from apps.rooms.models import Room


def _money_or_zero(value):
    return money(value or 0)


def build_dashboard(user):
    today = hotel_today()
    now = timezone.now()
    month_start = today.replace(day=1)
    today_start, tomorrow_start = hotel_date_bounds(today, today)
    month_start_at, _ = hotel_date_bounds(month_start, month_start)

    # --- Physical room state (operational truth) -----------------------------
    rooms_qs = Room.objects.filter(is_active=True)
    room_stats = rooms_qs.aggregate(
        total=Count("id"),
        occupied=Count("id", filter=Q(status=Room.Status.OCCUPIED)),
        maintenance=Count("id", filter=Q(status=Room.Status.MAINTENANCE)),
        out_of_service=Count("id", filter=Q(status=Room.Status.OUT_OF_SERVICE)),
    )
    total_rooms = room_stats["total"] or 0
    occupied_rooms = room_stats["occupied"] or 0
    unavailable_ops = (room_stats["maintenance"] or 0) + (room_stats["out_of_service"] or 0)

    # "Available tonight" uses the SAME inventory truth as booking allocation:
    # a room is available only if it is operationally usable AND has no active
    # (confirmed / checked-in / live-pending) reservation overlapping tonight.
    tomorrow = today + timedelta(days=1)
    blocked_tonight = set(
        BookingRoom.objects.filter(room__is_active=True)
        .filter(availability.blocking_booking_q(now=now, prefix="booking"))
        .filter(availability.overlap_q(today, tomorrow))
        .values_list("room_id", flat=True)
        .distinct()
    )
    operationally_free = set(
        rooms_qs.exclude(status__in=availability.OPERATIONALLY_BLOCKED)
        .values_list("id", flat=True)
    )
    available_rooms = len(operationally_free - blocked_tonight)
    occupancy_rate = round((occupied_rooms / total_rooms) * 100, 1) if total_rooms else 0.0

    # --- Booking pipeline stats ----------------------------------------------
    bookings = Booking.objects.all()
    booking_stats = bookings.aggregate(
        arrivals_today=Count("id", filter=Q(check_in=today, status=Booking.Status.CONFIRMED)),
        departures_today=Count("id", filter=Q(check_out=today, status=Booking.Status.CHECKED_IN)),
        in_house=Count("id", filter=Q(status=Booking.Status.CHECKED_IN)),
        pending=Count("id", filter=Q(status=Booking.Status.PENDING, expires_at__gt=now)),
        confirmed=Count("id", filter=Q(status=Booking.Status.CONFIRMED, check_in__gte=today)),
        created_today=Count("id", filter=Q(created_at__gte=today_start, created_at__lt=tomorrow_start)),
        cancelled_today=Count("id", filter=Q(cancelled_at__gte=today_start, cancelled_at__lt=tomorrow_start)),
        no_shows=Count("id", filter=Q(status=Booking.Status.NO_SHOW, check_in=today)),
    )

    operational_bookings = bookings.select_related("guest", "room_type").prefetch_related(
        Prefetch("room_assignments", queryset=BookingRoom.objects.select_related("room"), to_attr="_assignments")
    )
    upcoming_arrivals = operational_bookings.filter(
        status=Booking.Status.CONFIRMED, check_in__gte=today
    ).order_by("check_in", "created_at")[:7]
    in_house_bookings = operational_bookings.filter(
        status=Booking.Status.CHECKED_IN
    ).order_by("check_out", "checked_in_at")[:7]
    # The KPI and list intentionally share the same authoritative predicate.
    # This is a presentation of the existing departures_today aggregation, not
    # a second/conflicting definition of what constitutes a departure.
    departures_today = operational_bookings.filter(
        status=Booking.Status.CHECKED_IN, check_out=today
    ).order_by("checked_in_at", "created_at")
    checkout_time = HotelSettings.get_settings().check_out_time.strftime("%H:%M")

    recent_bookings = (
        bookings.select_related("guest", "room_type")
        .only("booking_reference", "status", "payment_status", "check_in", "check_out",
              "total_amount", "currency", "created_at",
              "guest__first_name", "guest__last_name", "room_type__name")
        .order_by("-created_at")[:7]
    )

    data = {
        "today": {
            "date": today.isoformat(),
            "arrivals": booking_stats["arrivals_today"],
            "departures": booking_stats["departures_today"],
            "in_house": booking_stats["in_house"],
            "no_shows": booking_stats["no_shows"],
        },
        "rooms": {
            "total": total_rooms,
            "occupied": occupied_rooms,
            "available": available_rooms,
            "out_of_order": unavailable_ops,
            "occupancy_rate_percent": occupancy_rate,
        },
        "bookings": {
            "created_today": booking_stats["created_today"],
            "pending": booking_stats["pending"],
            "confirmed_upcoming": booking_stats["confirmed"],
            "cancelled_today": booking_stats["cancelled_today"],
        },
        "upcoming_arrivals": [
            {"booking_reference": b.booking_reference, "guest_name": b.guest.full_name,
             "room_type_name": b.room_type.name, "check_in": b.check_in.isoformat(),
             "check_out": b.check_out.isoformat(), "status": b.status,
             "room_numbers": [a.room.room_number for a in getattr(b, "_assignments", [])]}
            for b in upcoming_arrivals
        ],
        "in_house_guests": [
            {"booking_reference": b.booking_reference, "guest_name": b.guest.full_name,
             "room_type_name": b.room_type.name, "check_out": b.check_out.isoformat(),
             "status": b.status, "checked_in_at": b.checked_in_at.isoformat() if b.checked_in_at else None,
             "room_numbers": [a.room.room_number for a in getattr(b, "_assignments", [])]}
            for b in in_house_bookings
        ],
        "departures_today": [
            {"booking_reference": b.booking_reference, "guest_name": b.guest.full_name,
             "room_type_name": b.room_type.name, "check_out": b.check_out.isoformat(),
             "check_out_time": checkout_time, "status": b.status,
             "room_numbers": [a.room.room_number for a in getattr(b, "_assignments", [])]}
            for b in departures_today
        ],
        "recent_bookings": [
            {
                "booking_reference": b.booking_reference,
                "guest_name": b.guest.full_name,
                "room_type_name": b.room_type.name,
                "check_in": b.check_in.isoformat(),
                "check_out": b.check_out.isoformat(),
                "status": b.status,
                "payment_status": b.payment_status,
                "total_amount": money(b.total_amount),
                "currency": b.currency,
                "created_at": b.created_at.isoformat(),
            }
            for b in recent_bookings
        ],
        "notifications": {"unread_count": unread_count(user)},
    }

    # Financial visibility: managers & admins only (receptionists see operations).
    if user.is_manager_or_admin:
        from apps.finance.services.reporting_service import financial_summary as ledger_financial_summary

        try:
            data["ledger"] = ledger_financial_summary(start_date=today, end_date=today, currency="NGN")
        except Exception:
            import logging
            logging.getLogger("apps").exception("Owner dashboard ledger summary failed")
            data["ledger"] = None
            data["ledger_error"] = "Ledger financial summary could not be loaded."

        today_revenue = (
            Payment.objects.filter(
                status=Payment.Status.SUCCESS, paid_at__gte=today_start, paid_at__lt=tomorrow_start
            ).aggregate(total=Sum("amount"))["total"]
        )
        month_revenue = (
            Payment.objects.filter(status=Payment.Status.SUCCESS, paid_at__gte=month_start_at)
            .aggregate(total=Sum("amount"))["total"]
        )
        outstanding = (
            Booking.objects.filter(
                status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN)
            )
            .annotate(balance=F("total_amount") - F("amount_paid"))
            .filter(balance__gt=0)
            .aggregate(total=Sum("balance"), count=Count("id"))
        )
        recent_payments = (
            Payment.objects.select_related("booking")
            .filter(status=Payment.Status.SUCCESS)
            .order_by("-paid_at")[:7]
        )
        data["revenue"] = {
            "today": _money_or_zero(today_revenue),
            "this_month": _money_or_zero(month_revenue),
            "outstanding_total": _money_or_zero(outstanding["total"]),
            "outstanding_bookings": outstanding["count"],
        }
        data["recent_payments"] = [
            {
                "reference": p.reference,
                "booking_reference": p.booking.booking_reference,
                "amount": money(p.amount),
                "currency": p.currency,
                "provider": p.provider,
                "channel": p.channel,
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            }
            for p in recent_payments
        ]
        data["alerts"] = _operational_alerts(today, now)
    else:
        data["alerts"] = _operational_alerts(today, now, include_financial=False)

    return data


def _operational_alerts(today, now, include_financial=True):
    alerts = []
    overdue_checkins = Booking.objects.filter(
        status=Booking.Status.CONFIRMED, check_in__lt=today
    ).count()
    if overdue_checkins:
        alerts.append({
            "type": "OVERDUE_CHECKIN",
            "message": f"{overdue_checkins} confirmed booking(s) have not checked in yet.",
        })
    late_checkouts = Booking.objects.filter(
        status=Booking.Status.CHECKED_IN, check_out__lt=today
    ).count()
    if late_checkouts:
        alerts.append({
            "type": "LATE_CHECKOUT",
            "message": f"{late_checkouts} in-house booking(s) are past their check-out date.",
        })
    dirty_rooms = Room.objects.filter(
        is_active=True, housekeeping_status=Room.HousekeepingStatus.DIRTY
    ).count()
    if dirty_rooms:
        alerts.append({
            "type": "HOUSEKEEPING",
            "message": f"{dirty_rooms} room(s) are marked dirty and need housekeeping.",
        })
    if include_financial:
        from apps.finance.models import ApprovalRequest, CashSession, Expense

        pending_variance = CashSession.objects.filter(status=CashSession.Status.PENDING_REVIEW).count()
        if pending_variance:
            alerts.append({
                "type": "CASH_VARIANCE",
                "message": f"{pending_variance} cash drawer(s) have unresolved counted-cash differences. This is a prompt to investigate, not proof of theft.",
            })
        pending_expenses = Expense.objects.filter(status=Expense.Status.SUBMITTED).count()
        if pending_expenses:
            alerts.append({
                "type": "EXPENSE_APPROVAL",
                "message": f"{pending_expenses} expense(s) are waiting for independent approval.",
            })
        pending_approvals = ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING).count()
        if pending_approvals:
            alerts.append({
                "type": "FINANCIAL_APPROVAL",
                "message": f"{pending_approvals} financially sensitive request(s) await review.",
            })
    return alerts
