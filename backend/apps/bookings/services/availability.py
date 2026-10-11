# apps/bookings/services/availability.py
"""Authoritative availability engine.

A room blocks a requested [check_in, check_out) range when it has an ACTIVE
assignment that OVERLAPS:

    existing.check_in < new.check_out  AND  new.check_in < existing.check_out

=> a booking ending 2026-10-15 does not block one starting 2026-10-15
(checkout day == next guest's check-in day).

"Active" assignments belong to bookings that are CONFIRMED/CHECKED_IN, or
PENDING with a live (unexpired) hold. Expired pending holds never block
inventory. Rooms under maintenance/out-of-service are excluded altogether.
"""
from datetime import date, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.rooms.models import Room, RoomType

from ..models import Booking, BookingRoom

BLOCKING_STATUSES = (Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN)
OPERATIONALLY_BLOCKED = (Room.Status.MAINTENANCE, Room.Status.OUT_OF_SERVICE)

# ---------------------------------------------------------------------------
# ONE authoritative room-readiness rule
#
# A physical room may be ADVERTISED as available or ASSIGNED to a guest only
# when every one of these holds:
#
#   1. is_active                     — the room exists in inventory;
#   2. status not in OPERATIONALLY_BLOCKED
#                                    — not under maintenance / out of service;
#   3. housekeeping_status == CLEAN  — cleaned AND released by housekeeping.
#
# Rule 3 is the fix for audit D3: Room 14 returned effective_status=AVAILABLE
# and public available=true while housekeeping had it DIRTY, so a room that was
# not ready to receive a guest could be sold.
#
# The housekeeping projection is deliberately meaningful:
#   * checkout sets the room DIRTY (booking_service._release_rooms_for_checkout);
#   * a cleaning task sets CLEANING while work is in progress — this is the
#     "awaiting inspection" state, and it is NOT sellable;
#   * only the dispatcher-authorized COMPLETED transition (which records
#     inspected_by/inspected_at) sets CLEAN.
# So a worker cannot make a room sellable by completing their own task, and a
# room is never auto-cleaned merely because a task was finished.
#
# Physical condition (housekeeping) stays distinct from commercial occupancy
# (live bookings), which is derived separately below.
# ---------------------------------------------------------------------------
READY_HOUSEKEEPING_STATUSES = (Room.HousekeepingStatus.CLEAN,)


def room_readiness_q():
    """The single sellable-room predicate, shared by every availability path."""
    return Q(is_active=True) & ~Q(status__in=OPERATIONALLY_BLOCKED) & Q(
        housekeeping_status__in=READY_HOUSEKEEPING_STATUSES
    )


def unready_room_reason(room) -> str:
    """Human-readable reason a specific room cannot be sold or assigned."""
    if not room.is_active:
        return f"Room {room.room_number} is inactive."
    if room.status in OPERATIONALLY_BLOCKED:
        return f"Room {room.room_number} is {room.get_status_display().lower()}."
    if room.housekeeping_status not in READY_HOUSEKEEPING_STATUSES:
        detail = {
            Room.HousekeepingStatus.DIRTY: "still needs cleaning",
            Room.HousekeepingStatus.CLEANING: "is awaiting inspection",
        }.get(room.housekeeping_status, "is not ready")
        return f"Room {room.room_number} {detail} (housekeeping: {room.housekeeping_status})."
    return ""


def room_is_sellable(room) -> bool:
    """True when the room satisfies the authoritative readiness rule."""
    return not unready_room_reason(room)


def sellable_rooms_queryset(queryset=None):
    """Apply the readiness rule to a Room queryset (the one place it lives)."""
    return (queryset if queryset is not None else Room.objects.all()).filter(room_readiness_q())


def blocking_booking_q(now=None, prefix=""):
    now = now or timezone.now()
    p = f"{prefix}__" if prefix else ""
    return Q(**{f"{p}status__in": BLOCKING_STATUSES}) | Q(
        **{f"{p}status": Booking.Status.PENDING, f"{p}expires_at__gt": now}
    )


def overlap_q(check_in: date, check_out: date, prefix=""):
    p = f"{prefix}__" if prefix else ""
    return Q(**{f"{p}check_in__lt": check_out}) & Q(**{f"{p}check_out__gt": check_in})


def blocked_room_ids(*, room_type_id, check_in, check_out, now=None, exclude_booking_id=None):
    qs = (
        BookingRoom.objects.filter(room__room_type_id=room_type_id)
        .filter(blocking_booking_q(now=now, prefix="booking"))
        .filter(overlap_q(check_in, check_out))
    )
    if exclude_booking_id:
        qs = qs.exclude(booking_id=exclude_booking_id)
    return qs.values_list("room_id", flat=True).distinct()


def available_rooms_queryset(*, room_type, check_in, check_out, now=None,
                             for_update=False, exclude_booking_id=None, exclude_room_ids=()):
    qs = (
        # The readiness rule is applied HERE, in the single allocation path, so
        # no caller can offer or assign a dirty / awaiting-inspection room.
        sellable_rooms_queryset(Room.objects.filter(room_type=room_type))
        .exclude(
            pk__in=blocked_room_ids(
                room_type_id=room_type.pk,
                check_in=check_in,
                check_out=check_out,
                now=now,
                exclude_booking_id=exclude_booking_id,
            )
        )
        .order_by("room_number")
    )
    if exclude_room_ids:
        qs = qs.exclude(pk__in=[pk for pk in exclude_room_ids if pk])
    if for_update:
        qs = qs.select_for_update()
    return qs


def _first_available_room(room_type, *, check_in, check_out, now=None, exclude_room_ids=()):
    """Cheapest possible answer to \"is anything free in this type?\" — one row."""
    return available_rooms_queryset(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        now=now,
        exclude_room_ids=exclude_room_ids,
    ).select_related("room_type").first()


def find_substitute_room(*, room_type, check_in, check_out, guests=1, now=None,
                         exclude_room_ids=()):
    """Deterministic replacement for an exact room that just became unavailable.

    The availability engine stays authoritative: nothing here trusts a
    frontend claim, and a substitute is only returned when it is genuinely
    free for the requested ``[check_in, check_out)`` window.

    Preference order
    ----------------
    1. another room of the **same room type** with the **same nightly rate**
       (within one type the rate is identical by definition; ties break on
       ``room_number`` so the choice is deterministic, never random).
    2. same room type, closest nightly rate — a no-op under the current
       per-type pricing model, kept so the ordering stays correct if
       per-room rates are ever introduced.
    3. a different **active room type** whose nightly rate is **identical**
       and whose capacity covers the party. Because the rate matches, the
       stay total the guest already saw does not change.
    4. "Closest suitable room" at a *different* price is deliberately NOT
       auto-selected: a changed total may never be applied to a guest's
       booking without their explicit consent. The caller surfaces
       ``ROOM_UNAVAILABLE`` and the availability search lists what is free.

    Returns ``None`` when no acceptable substitute exists, otherwise a dict
    describing the substitution (the caller is responsible for telling the
    guest — a substitution may never happen silently).
    """
    from decimal import Decimal

    guests = max(1, int(guests or 1))
    exclude = {pk for pk in (exclude_room_ids or ()) if pk}

    # --- Levels 1 & 2: same room type, closest rate, deterministic order ----
    candidates = list(
        available_rooms_queryset(
            room_type=room_type, check_in=check_in, check_out=check_out,
            now=now, exclude_room_ids=exclude,
        ).select_related("room_type")
    )
    if candidates:
        target = Decimal(room_type.base_price)
        candidates.sort(key=lambda r: (abs(Decimal(r.room_type.base_price) - target), r.room_number))
        room = candidates[0]
        return {
            "room": room,
            "room_type": room.room_type,
            "level": 1 if room.room_type.base_price == target else 2,
            "price_changed": room.room_type.base_price != target,
            "reason": (
                f"Room {room.room_number} is available in the same room type "
                f"({room.room_type.name}) for the same nightly rate."
            ),
        }

    # --- Level 3: identical rate in another type that can hold the party ----
    target = Decimal(room_type.base_price)
    alt_types = (
        RoomType.objects.filter(is_active=True, max_guests__gte=guests)
        .exclude(pk=room_type.pk)
        .order_by("display_order", "name")
    )
    best = None
    for alt in alt_types:
        room = _first_available_room(
            alt, check_in=check_in, check_out=check_out, now=now, exclude_room_ids=exclude
        )
        if room is None:
            continue
        diff = abs(Decimal(alt.base_price) - target)
        key = (diff, alt.max_guests, alt.name)
        if best is None or key < best[0]:
            best = (key, room, alt, diff)
    if best is not None and best[3] == 0:
        _, room, alt, _diff = best
        return {
            "room": room,
            "room_type": alt,
            "level": 3,
            "price_changed": False,
            "reason": (
                f"Room {room.room_number} ({alt.name}) has the same nightly rate "
                f"and sleeps up to {alt.max_guests} guests."
            ),
        }
    return None


def available_room_counts(*, room_type_ids, check_in, check_out, now=None):
    """Return sellable free-room counts for many room types in two queries.

    This is the range-search counterpart to :func:`available_room_count`.
    It applies the same operational and live-booking rules, but aggregates
    sellable rooms and distinct blocked rooms by type instead of issuing one
    subquery/count pair per type. It is intentionally read-only and is used
    only for public search fan-out; booking allocation continues to call the
    authoritative per-room queryset under its transaction/lock path.
    """
    type_ids = list(dict.fromkeys(pk for pk in room_type_ids if pk))
    counts = {pk: 0 for pk in type_ids}
    if not type_ids:
        return counts

    now = now or timezone.now()
    for row in (
        sellable_rooms_queryset(Room.objects.filter(room_type_id__in=type_ids))
        .values("room_type_id")
        .annotate(total=Count("pk"))
    ):
        counts[row["room_type_id"]] = row["total"]

    # A blocked maintenance/inactive room must not be subtracted twice: it was
    # never included in the sellable total. DISTINCT keeps corrupted/overlapping
    # assignment rows from making one physical room count as several blocks.
    blocked_by_type = (
        BookingRoom.objects.filter(
            room__room_type_id__in=type_ids,
            room__is_active=True,
        )
        .exclude(room__status__in=OPERATIONALLY_BLOCKED)
        .filter(blocking_booking_q(now=now, prefix="booking"))
        .filter(overlap_q(check_in, check_out))
        .values("room__room_type_id")
        .annotate(blocked=Count("room_id", distinct=True))
    )
    for row in blocked_by_type:
        room_type_id = row["room__room_type_id"]
        counts[room_type_id] = max(0, counts.get(room_type_id, 0) - row["blocked"])
    return counts


def available_room_count(*, room_type, check_in, check_out, now=None):
    return available_rooms_queryset(
        room_type=room_type, check_in=check_in, check_out=check_out, now=now
    ).count()


# ---------------------------------------------------------------------------
# Per-night inventory (calendar)
# ---------------------------------------------------------------------------
MAX_CALENDAR_WINDOW_DAYS = 366


def nightly_inventory(*, room_type, start, end, now=None):
    """Per-night sellable inventory for one room type across ``[start, end)``.

    This is the SAME authoritative answer the range search and booking
    creation rely on (the blocking rules above), resolved one night at a
    time so a calendar can disable genuinely sold-out dates:

    * CONFIRMED / CHECKED_IN bookings and PENDING bookings with a live hold
      block their ``[check_in, check_out)`` nights;
    * cancelled, expired, checked-out and no-show reservations never block;
    * maintenance / out-of-service rooms are not sellable at all.

    A date is unavailable only when EVERY sellable physical room of the type
    is blocked on that night — a date some booking merely touches is still
    available while another room of the same type is free, and a checkout day
    never blocks the next guest's check-in.

    Returns::

        {
            "total_sellable": 3,               # sellable physical rooms
            "dates": {                         # one entry PER date in window
                "2026-09-18": {"available_rooms": 2, "available": True},
                "2026-09-19": {"available_rooms": 0, "available": False},
            },
        }

    Cost: exactly two queries (sellable ids + live assignments) plus an
    O(assignments log assignments + days) in-memory sweep — never one query
    per date.
    """
    from collections import defaultdict

    if end <= start:
        raise ValueError("end must be after start")
    span = min((end - start).days, MAX_CALENDAR_WINDOW_DAYS)
    end = start + timedelta(days=span)

    now = now or timezone.now()
    sellable_ids = list(
        sellable_rooms_queryset(Room.objects.filter(room_type=room_type))
        .values_list("pk", flat=True)
    )

    by_room = defaultdict(list)
    if sellable_ids:
        for room_id, ci, co in (
            BookingRoom.objects.filter(room_id__in=sellable_ids)
            .filter(blocking_booking_q(now=now, prefix="booking"))
            .filter(check_in__lt=end, check_out__gt=start)
            .values_list("room_id", "check_in", "check_out")
        ):
            by_room[room_id].append((ci, co))

    # Difference array over the window: each room's live assignments are
    # merged into non-overlapping intervals, then +1/-1 events are applied at
    # the interval edges. The running sum is the number of DISTINCT rooms
    # blocked on each night, so overlapping data can never double-count.
    delta = [0] * (span + 1)

    def _apply(ci, co):
        lo = max(0, (ci - start).days)
        hi = min(span, (co - start).days)
        if lo < hi:
            delta[lo] += 1
            delta[hi] -= 1

    for spans in by_room.values():
        spans.sort()
        merged_ci, merged_co = spans[0]
        for ci, co in spans[1:]:
            if ci <= merged_co:  # overlap (or back-to-back) for the same room
                merged_co = max(merged_co, co)
            else:
                _apply(merged_ci, merged_co)
                merged_ci, merged_co = ci, co
        _apply(merged_ci, merged_co)

    dates = {}
    blocked = 0
    for offset in range(span):
        blocked += delta[offset]
        free = len(sellable_ids) - blocked
        dates[(start + timedelta(days=offset)).isoformat()] = {
            "available_rooms": max(free, 0),
            "available": free >= 1,
        }
    return {"total_sellable": len(sellable_ids), "dates": dates}


def count_active_blocking(booking):
    """How many physical rooms a booking currently blocks (usually == number_of_rooms)."""
    return BookingRoom.objects.filter(booking=booking).count()
