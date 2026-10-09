"""Transactional bridge from existing booking check-in/out to Stay history."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.utils import hotel_today

from .models import Stay, StayEvent, StayRoom


def _stay_reference(booking) -> str:
    # Booking references are already unique and max 40 characters, leaving a
    # stable human-readable prefix well within Stay.reference's 56-character cap.
    return f"STY-{booking.booking_reference}"


def _create_stay(*, booking, assignments, actor, checked_in_at, bridge_reason=""):
    stay = Stay.objects.create(
        reference=_stay_reference(booking),
        booking=booking,
        guest=booking.guest,
        status=Stay.Status.IN_HOUSE,
        expected_arrival=booking.check_in,
        expected_departure=booking.check_out,
        business_date=hotel_today(),
        actual_check_in_at=checked_in_at,
        checked_in_by=actor,
        notes=bridge_reason,
    )
    StayRoom.objects.bulk_create([
        StayRoom(
            stay=stay,
            room=assignment.room,
            booking_room=assignment,
            assigned_at=checked_in_at,
            actual_check_in_at=checked_in_at,
            assigned_by=actor,
            assignment_reason=bridge_reason or "Booking check-in",
        )
        for assignment in assignments
    ])
    StayEvent.objects.create(
        stay=stay,
        type=StayEvent.Type.CHECKED_IN,
        occurred_at=checked_in_at,
        actor=actor,
        details={
            "booking_reference": booking.booking_reference,
            "rooms": [assignment.room.room_number for assignment in assignments],
            "bridged": bool(bridge_reason),
        },
    )
    _ensure_main_folio(stay=stay, actor=actor)
    return stay


def _ensure_main_folio(*, stay, actor):
    """Open the additive guest folio with the operational stay.

    This import stays local so the foundational Stay app remains free of an
    import-time dependency cycle with Finance. Any failure rolls back the
    encompassing check-in/check-out transaction; a stay must not silently exist
    without its operational folio once Finance is installed.
    """
    from apps.finance.services.folio_service import get_or_create_main_folio_for_stay
    from apps.finance.services.payment_collection_service import apply_unallocated_payments_for_booking

    folio, created = get_or_create_main_folio_for_stay(stay=stay, actor=actor)
    # A collection may predate operational arrival (online deposits) or the
    # finance cutover itself. Apply it only once a real guest folio exists.
    apply_unallocated_payments_for_booking(booking=stay.booking, folio=folio, actor=actor)
    return folio, created


def _post_elapsed_accommodation_for_checkout(*, stay, actor):
    """Catch up completed nights before finalizing operational checkout."""
    from apps.finance.services.accommodation_service import post_due_accommodation_charges_for_stay

    return post_due_accommodation_charges_for_stay(
        stay=stay,
        through_date=hotel_today() - timedelta(days=1),
        actor=actor,
    )


@transaction.atomic
def ensure_stay_for_check_in(*, booking, assignments, actor):
    """Create the one operational stay for a confirmed arrival, idempotently.

    After the stay is in house, the guest-portal invitation is scheduled for
    commit. Scheduling is idempotent and never affects the check-in itself.
    """
    stay = _ensure_stay_for_check_in(booking=booking, assignments=assignments, actor=actor)
    from apps.portal.invitations import schedule_portal_invitation

    schedule_portal_invitation(stay.pk)
    return stay


def _ensure_stay_for_check_in(*, booking, assignments, actor):
    checked_in_at = booking.checked_in_at or timezone.now()
    stay = Stay.objects.select_for_update().filter(booking=booking).first()
    if stay is None:
        return _create_stay(
            booking=booking,
            assignments=assignments,
            actor=actor,
            checked_in_at=checked_in_at,
        )
    if stay.status == Stay.Status.IN_HOUSE:
        _ensure_main_folio(stay=stay, actor=actor)
        return stay
    # A booking cannot legally transition from CHECKED_OUT back to CHECKED_IN in
    # the legacy state machine. Surface a consistency issue rather than silently
    # rewriting operational history.
    raise ValueError(f"Stay {stay.reference} cannot be checked in from {stay.status}.")


@transaction.atomic
def close_stay_for_checkout(*, booking, assignments, actor=None, automatic=False):
    """Close/bridge an in-house Stay and its active room occupancy entries."""
    checked_out_at = booking.checked_out_at or timezone.now()
    stay = Stay.objects.select_for_update().filter(booking=booking).first()
    if stay is None:
        # Deployment may occur while a legacy booking is already IN_HOUSE. A
        # lazy bridge preserves a truthful event trail instead of refusing a
        # valid checkout or fabricating a pre-arrival stay during migration.
        stay = _create_stay(
            booking=booking,
            assignments=assignments,
            actor=None,
            checked_in_at=booking.checked_in_at or checked_out_at,
            bridge_reason="Bridged from legacy in-house booking during checkout",
        )
    if stay.status == Stay.Status.CHECKED_OUT:
        _ensure_main_folio(stay=stay, actor=actor)
        _post_elapsed_accommodation_for_checkout(stay=stay, actor=actor)
        return stay
    if stay.status != Stay.Status.IN_HOUSE:
        raise ValueError(f"Stay {stay.reference} cannot be checked out from {stay.status}.")

    _ensure_main_folio(stay=stay, actor=actor)
    _post_elapsed_accommodation_for_checkout(stay=stay, actor=actor)
    active_rooms = list(StayRoom.objects.select_for_update().filter(stay=stay, released_at__isnull=True))
    for stay_room in active_rooms:
        stay_room.actual_check_out_at = checked_out_at
        stay_room.released_at = checked_out_at
        stay_room.save(update_fields=["actual_check_out_at", "released_at", "updated_at"])
        StayEvent.objects.create(
            stay=stay,
            type=StayEvent.Type.ROOM_RELEASED,
            occurred_at=checked_out_at,
            actor=actor,
            details={"room_number": stay_room.room.room_number},
        )
        # Checkout already marks the room DIRTY through the legacy-compatible
        # booking projection. Add one source-keyed operational task so that
        # readiness returns to CLEAN only via the controlled housekeeping flow.
        from apps.housekeeping.services.task_service import ensure_checkout_cleaning_task
        ensure_checkout_cleaning_task(stay=stay, room=stay_room.room, actor=actor)

    stay.status = Stay.Status.CHECKED_OUT
    stay.actual_check_out_at = checked_out_at
    stay.checked_out_by = actor
    stay.save(update_fields=["status", "actual_check_out_at", "checked_out_by", "updated_at"])
    StayEvent.objects.create(
        stay=stay,
        type=StayEvent.Type.CHECKED_OUT,
        occurred_at=checked_out_at,
        actor=actor,
        details={"automatic": bool(automatic), "booking_reference": booking.booking_reference},
    )
    return stay
