# apps/bookings/services/extension_service.py
"""Extend an existing stay: quote it, charge it, and keep the evidence.

An extension is deliberately NOT a new booking and NOT a silent edit of
``Booking.check_out``. It is a money event on the reservation that is already
in the system, so it has to behave like one:

* the room must genuinely be free for the extra nights (the same authoritative
  overlap rule the booking engine uses), otherwise the stay is refused rather
  than overbooked;
* the amount is computed by the BACKEND from the rate the guest already agreed,
  never from a figure the browser sent;
* the money is recorded through the existing payment service, so the payment
  lands in the ledger, the receipt and the financial reports exactly like any
  other collection;
* the previous checkout date is preserved on an append-only history row, so the
  desk can always reconstruct what the guest was originally booked for.

Nothing here trusts a client-supplied amount, nightly rate or checkout date.
"""
from decimal import ROUND_HALF_UP, Decimal
import logging

from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_action
from apps.core.exceptions import (
    BookingStateError,
    InvalidDatesError,
    PaymentError,
    RoomUnavailableError,
)
from apps.core.utils import money
from apps.hotel.models import HotelSettings
from apps.rooms.models import Room, RoomType

from ..models import Booking, BookingExtension, BookingRoom
from . import availability

logger = logging.getLogger("apps")

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")

# A stay can only be extended while it is still live: an expected/confirmed
# reservation, or a guest already in house. Anything already closed
# (checked out, cancelled, expired, no-show) is history and must not reopen.
EXTENDABLE_STATUSES = (
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
)


def _q(value) -> Decimal:
    return Decimal(value or 0).quantize(_CENT, rounding=ROUND_HALF_UP)


def can_extend(booking: Booking) -> tuple:
    """``(allowed, reason)`` — whether the desk may offer "Extend stay"."""
    if booking is None:
        return False, "This booking could not be loaded."
    status = booking.status
    if status not in EXTENDABLE_STATUSES:
        return False, (
            f"A booking that is {booking.get_status_display().lower()} cannot be extended."
        )
    if not booking.room_assignments.exists():
        return False, "This booking has no physical room assigned yet."
    return True, ""


def _resolve_target_checkout(*, booking, new_check_out=None, additional_nights=None):
    """Turn "3 more nights" (or an explicit date) into one concrete date.

    Both inputs are accepted because the desk thinks in nights ("three more
    nights") while a guest asking for a date says "the eleventh". Exactly one
    has to be supplied; supplying both is only allowed when they agree.
    """
    if new_check_out is None and additional_nights in (None, ""):
        raise InvalidDatesError(
            "Give either the new checkout date or the number of additional nights."
        )
    if new_check_out is not None:
        if additional_nights not in (None, ""):
            nights = int(additional_nights)
            if booking.check_out is None or (new_check_out - booking.check_out).days != nights:
                raise InvalidDatesError(
                    "The new checkout date and the number of additional nights do not "
                    "match. Provide one or the other."
                )
        return new_check_out, (new_check_out - booking.check_out).days
    nights = int(additional_nights)
    if nights < 1:
        raise InvalidDatesError("An extension must be for at least one additional night.")
    from datetime import timedelta

    return booking.check_out + timedelta(days=nights), nights


def _blocked_rooms_for_extension(*, booking, previous_check_out, new_check_out):
    """Physical rooms of this booking that cannot cover the extra nights.

    Uses the SAME half-open overlap rule as the booking engine
    (``existing.check_in < new.check_out AND new.check_in < existing.check_out``)
    and the SAME "which reservations block" rule, so a room that another
    confirmed booking holds for any of the added nights is reported.

    Readiness is checked separately and deliberately differently from a new
    booking: the guest is ALREADY in this room, so housekeeping cleanliness is
    irrelevant here — only a maintenance/out-of-service outage genuinely stops
    them staying.
    """
    assignments = list(booking.room_assignments.select_related("room"))
    if not assignments:
        return [], []
    conflicts = set(
        availability.blocked_room_ids(
            room_type_id=booking.room_type_id,
            check_in=previous_check_out,
            check_out=new_check_out,
            exclude_booking_id=booking.pk,
        )
    )
    blocked = []
    out_of_service = []
    for assignment in assignments:
        room = assignment.room
        if room.pk in conflicts:
            blocked.append(room)
        elif room.status in availability.OPERATIONALLY_BLOCKED or not room.is_active:
            out_of_service.append(room)
    return blocked, out_of_service


def _extension_rate(booking: Booking) -> Decimal:
    """The nightly rate an extension is charged at.

    The rate the guest already agreed (``Booking.price_per_night``) is used, not
    today's configured rate. Re-pricing a stay the guest is already sleeping in
    would change an agreed contract, and the stored snapshot is the figure the
    receipt and the original booking were both built on.
    """
    return _q(booking.price_per_night)


def quote_extension(*, booking, new_check_out=None, additional_nights=None) -> dict:
    """Price and validate an extension WITHOUT changing anything.

    The dashboard calls this to show the desk the extra nights and the amount
    payable before any money is taken; the confirm call re-runs every check
    inside its own transaction, so this is a preview and never the authority.
    """
    from datetime import timedelta

    settings_obj = HotelSettings.get_settings()
    allowed, reason = can_extend(booking)
    if not allowed:
        raise BookingStateError(reason)

    previous_check_out = booking.check_out
    target, nights = _resolve_target_checkout(
        booking=booking, new_check_out=new_check_out, additional_nights=additional_nights
    )
    if nights < 1:
        raise InvalidDatesError("The new checkout date must be after the current one.")
    total_nights = (target - booking.check_in).days
    if total_nights > settings_obj.max_stay_nights:
        raise InvalidDatesError(
            f"A stay may not exceed {settings_obj.max_stay_nights} night(s); "
            f"{target} would make this one {total_nights}."
        )

    rate = _extension_rate(booking)
    rooms = max(1, int(booking.number_of_rooms or 1))
    amount = _q(Decimal(rate) * nights * rooms)

    blocked, out_of_service = _blocked_rooms_for_extension(
        booking=booking, previous_check_out=previous_check_out, new_check_out=target
    )

    room_numbers = [
        a.room.room_number
        for a in booking.room_assignments.select_related("room").order_by("room__room_number")
    ]
    conflicts = []
    if blocked:
        conflicts.append(
            "Room(s) "
            + ", ".join(sorted(room.room_number for room in blocked))
            + " are already reserved by another booking for part of the added nights."
        )
    if out_of_service:
        conflicts.append(
            "Room(s) "
            + ", ".join(sorted(room.room_number for room in out_of_service))
            + " are not in service and cannot host the extra nights."
        )

    return {
        "booking_reference": booking.booking_reference,
        "guest_name": booking.guest.full_name,
        "rooms": room_numbers,
        "check_in": booking.check_in.isoformat(),
        "current_check_out": previous_check_out.isoformat(),
        "new_check_out": target.isoformat(),
        "additional_nights": nights,
        "total_nights_after_extension": total_nights,
        "nightly_rate": money(rate),
        "number_of_rooms": rooms,
        "extension_amount": money(amount),
        "currency": booking.currency,
        "amount_due_before_extension": money(_q(booking.amount_due)),
        "available": not conflicts,
        "conflicts": conflicts,
        "extension_count": booking.extensions.filter(
            status=BookingExtension.Status.CONFIRMED
        ).count(),
    }


@transaction.atomic
def extend_booking(*, booking, staff_user, new_check_out=None, additional_nights=None,
                   amount_paid=None, provider="", notes="", request=None):
    """Extend a stay, take the money and record the history.

    Everything happens in ONE transaction: the availability re-check, the
    checkout-date change, the room-assignment change, the price change and the
    payment. If the payment cannot be recorded nothing is written — there is no
    such thing as a half-extended stay.
    """
    # Serialize with booking creation and room assignment: the room-type row is
    # the same lock `create_booking` takes, so a new reservation for this room
    # cannot slip in between our overlap check and our write.
    RoomType.objects.select_for_update().get(pk=booking.room_type_id)
    booking = Booking.objects.select_for_update().select_related(
        "guest", "room_type"
    ).get(pk=booking.pk)

    allowed, reason = can_extend(booking)
    if not allowed:
        raise BookingStateError(reason)

    settings_obj = HotelSettings.get_settings()
    previous_check_out = booking.check_out
    target, nights = _resolve_target_checkout(
        booking=booking, new_check_out=new_check_out, additional_nights=additional_nights
    )
    if nights < 1:
        raise InvalidDatesError(
            f"The new checkout date must be after the current one ({previous_check_out})."
        )
    total_nights = (target - booking.check_in).days
    if total_nights > settings_obj.max_stay_nights:
        raise InvalidDatesError(
            f"A stay may not exceed {settings_obj.max_stay_nights} night(s); "
            f"{target} would make this one {total_nights}."
        )
    if BookingExtension.objects.filter(
        booking=booking, new_check_out=target, status=BookingExtension.Status.CONFIRMED
    ).exists():
        # Double-click protection: the stay is ALREADY extended to this date.
        raise BookingStateError(
            f"This stay has already been extended to {target}. Choose a later date."
        )

    rate = _extension_rate(booking)
    rooms = max(1, int(booking.number_of_rooms or 1))
    extension_amount = _q(Decimal(rate) * nights * rooms)

    blocked, out_of_service = _blocked_rooms_for_extension(
        booking=booking, previous_check_out=previous_check_out, new_check_out=target
    )
    if blocked:
        numbers = ", ".join(sorted(room.room_number for room in blocked))
        raise RoomUnavailableError(
            f"Room(s) {numbers} are already reserved by another booking for part of "
            "the added nights. The stay was not extended and the guest's room was not "
            "changed — release the conflicting reservation, or move this guest with the "
            "existing room-transfer action."
        )
    if out_of_service:
        numbers = ", ".join(sorted(room.room_number for room in out_of_service))
        raise RoomUnavailableError(
            f"Room(s) {numbers} are not in service and cannot host the extra nights."
        )

    # --- Money ------------------------------------------------------------
    # What the desk actually collected. Never more than the extension costs and
    # never negative; a shortfall stays on the booking balance and follows the
    # hotel's existing partial-payment rules (it is what blocks checkout).
    if amount_paid in (None, ""):
        paid = extension_amount
    else:
        paid = _q(Decimal(amount_paid))
    if paid < _ZERO:
        raise PaymentError("The amount paid cannot be negative.")
    if paid > extension_amount:
        raise PaymentError(
            f"The amount received ({money(paid)}) exceeds the "
            f"extension cost of {money(extension_amount)}."
        )
    provider = str(provider or "").strip().upper()

    # --- Apply the extension ----------------------------------------------
    previous_total = booking.total_amount
    booking.check_out = target
    booking.subtotal = _q(booking.subtotal + extension_amount)
    booking.total_amount = _q(booking.total_amount + extension_amount)
    # The balance grows by exactly what the extra nights cost. Payment status is
    # re-derived BEFORE the payment is recorded so a previously settled booking
    # is no longer "PAID" — otherwise the payment service would (correctly)
    # refuse to take money for a booking that looked fully settled.
    if booking.amount_paid >= booking.total_amount:
        booking.payment_status = Booking.PaymentStatus.PAID
    elif booking.amount_paid > _ZERO:
        booking.payment_status = Booking.PaymentStatus.PARTIALLY_PAID
    else:
        booking.payment_status = Booking.PaymentStatus.UNPAID
    booking.save(update_fields=[
        "check_out", "subtotal", "total_amount", "payment_status", "updated_at",
    ])

    # Every reserved room of this stay now runs to the new date: the room must
    # not become sellable to anybody else before the guest actually leaves.
    booking.room_assignments.update(check_out=target)

    # Keep the operational stay in step so occupancy, nightly revenue and the
    # housekeeping trigger all see the real departure date. Imported lazily:
    # the stays app depends on bookings, never the other way round.
    stay = getattr(booking, "stay", None)
    if stay is not None:
        from apps.stays.models import Stay

        if stay.status in (Stay.Status.EXPECTED, Stay.Status.IN_HOUSE):
            stay.expected_departure = target
            stay.save(update_fields=["expected_departure", "updated_at"])

    # --- Record the money through the existing payment system --------------
    payment = None
    if paid > _ZERO:
        from apps.payments.services.payment_service import record_offline_payment

        payment = record_offline_payment(
            booking=booking,
            staff_user=staff_user,
            amount=paid,
            provider=provider or "CASH",
            notes=(notes or f"Stay extension to {target}")[:255],
            request=request,
        )

    extension = BookingExtension.objects.create(
        booking=booking,
        previous_check_out=previous_check_out,
        new_check_out=target,
        additional_nights=nights,
        nightly_rate=rate,
        extension_amount=extension_amount,
        amount_paid=paid,
        currency=booking.currency,
        payment=payment,
        payment_method=payment.provider if payment else (provider if paid > _ZERO else ""),
        payment_reference=payment.reference if payment else "",
        processed_by=staff_user,
        notes=(notes or "")[:255],
        status=BookingExtension.Status.CONFIRMED,
    )

    log_action(
        actor=staff_user,
        action="BOOKING_EXTENDED",
        instance=booking,
        changes={
            "check_out": [str(previous_check_out), str(target)],
            "total_amount": [str(previous_total), str(booking.total_amount)],
        },
        metadata={
            "reference": booking.booking_reference,
            "extension_id": extension.pk,
            "additional_nights": nights,
            "extension_amount": str(extension_amount),
            "amount_paid": str(paid),
            "outstanding_balance": str(extension.outstanding_balance),
            "payment_reference": extension.payment_reference,
            "payment_method": extension.payment_method,
            "rooms": [
                a.room.room_number
                for a in booking.room_assignments.select_related("room")
            ],
        },
        request=request,
        summary=(
            f"Booking {booking.booking_reference} extended by {nights} night(s) "
            f"to {target} ({money(extension_amount)})"
        ),
    )
    logger.info(
        "Booking extended: %s +%s night(s) to %s amount=%s paid=%s by staff %s",
        booking.booking_reference, nights, target, extension_amount, paid,
        getattr(staff_user, "pk", None),
    )
    return extension


def extension_history(booking: Booking):
    """Every extension of a stay, oldest first, with the ORIGINAL date kept.

    ``original_check_out`` is the checkout date the booking was created with.
    It is NOT mutated when a stay is extended, so it is recovered here from the
    earliest extension row (whose ``previous_check_out`` is that original date)
    instead of being recomputed or guessed.
    """
    rows = list(
        booking.extensions.select_related("processed_by", "payment")
        .order_by("created_at", "pk")
    )
    original = rows[0].previous_check_out if rows else booking.check_out
    return original, rows
