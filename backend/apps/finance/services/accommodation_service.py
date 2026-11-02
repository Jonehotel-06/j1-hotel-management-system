"""Nightly, idempotent accommodation-revenue recognition for operational stays."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.utils import hotel_today
from apps.stays.models import Stay

from ..models import FinancialLine, FinancialTransaction, FolioPosting
from . import accounting
from .folio_service import get_or_create_main_folio_for_stay
from .ledger_service import create_posted_transaction

CENT = Decimal("0.01")
MAX_CATCH_UP_NIGHTS = 366


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _component_for_night(total, *, nights: int, night_index: int) -> Decimal:
    """Split a booked component deterministically with its rounding residual last."""
    total = _money(total)
    if nights <= 0 or not 0 <= night_index < nights:
        raise ValidationError("Nightly revenue requires a valid booked night.")
    base = (total / Decimal(nights)).quantize(CENT, rounding=ROUND_DOWN)
    if night_index == nights - 1:
        return total - (base * (nights - 1))
    return base


def night_components(booking, service_date: date) -> tuple[Decimal, Decimal, Decimal]:
    """Public view of the nightly net/tax/fee split.

    Reporting and reconciliation read the same split the ledger postings use, so
    a detected gap always equals the amount that would have been posted.
    """
    return _night_components(booking, service_date)


def _night_components(booking, service_date: date) -> tuple[Decimal, Decimal, Decimal]:
    nights = (booking.check_out - booking.check_in).days
    night_index = (service_date - booking.check_in).days
    # Net room/extra-guest/discount amount is derived as a residual so historic
    # quote rounding cannot make daily journal entries differ from total_amount.
    tax_total = _money(booking.tax_amount)
    fee_total = _money(booking.fee_amount)
    net_total = _money(booking.total_amount) - tax_total - fee_total
    return (
        _component_for_night(net_total, nights=nights, night_index=night_index),
        _component_for_night(tax_total, nights=nights, night_index=night_index),
        _component_for_night(fee_total, nights=nights, night_index=night_index),
    )


def occupied_night_components(stay, service_date: date):
    """Return booking-night amounts for rooms actually checked in that night.

    A booking's one price/discount/fee is allocated pro rata, with cent
    residual assigned deterministically to the last booked room. Multi-room
    recognition never assumes a room arrived. Existing single-room legacy
    stays lacking per-room timestamps retain the prior compatibility path and
    must be reconciled before cutover.
    """
    booking = stay.booking
    components = _night_components(booking, service_date)
    room_count = int(booking.number_of_rooms or 1)
    assignments = list(booking.room_assignments.select_related("room").order_by("pk"))
    if room_count <= 1 and not any(a.checked_in_at for a in assignments):
        return components, []
    if len(assignments) > room_count:
        raise ValidationError("Room assignment count exceeds the booking quantity; review before recognition.")
    occupancy = {row.booking_room_id: row for row in stay.stay_rooms.filter(booking_room__isnull=False)}
    room_details = []
    totals = [Decimal("0.00"), Decimal("0.00"), Decimal("0.00")]
    for index, assignment in enumerate(assignments):
        if assignment.checked_in_at is None:
            continue
        arrival = timezone.localtime(assignment.checked_in_at).date()
        if arrival > service_date or not assignment.check_in <= service_date < assignment.check_out:
            continue
        stay_room = occupancy.get(assignment.pk)
        if stay_room and stay_room.released_at and timezone.localtime(stay_room.released_at).date() <= service_date:
            continue
        shares = [_component_for_night(value, nights=room_count, night_index=index) for value in components]
        totals = [left + right for left, right in zip(totals, shares)]
        room_details.append({"booking_room_id": assignment.pk, "room_number": assignment.room.room_number,
                             "net": str(shares[0]), "tax": str(shares[1]), "service_fee": str(shares[2])})
    return tuple(totals), room_details


@transaction.atomic
def post_accommodation_charge_for_night(*, stay, service_date: date, actor=None):
    """Post one booked night exactly once after it has elapsed.

    Accommodation is recognized nightly, not at reservation creation or when a
    prepayment clears. A stable source key makes retries from Celery, checkout,
    or operator recovery idempotent.
    """
    stay = (
        Stay.objects.select_for_update()
        .select_related("booking", "guest")
        .get(pk=stay.pk)
    )
    booking = stay.booking
    if stay.status not in {Stay.Status.IN_HOUSE, Stay.Status.CHECKED_OUT}:
        raise ValidationError("Accommodation may only be posted for an operational in-house or checked-out stay.")
    if not booking.check_in <= service_date < booking.check_out:
        raise ValidationError("Service date must be within the booked [check-in, check-out) range.")
    # Do not recognize a still-open hotel night. Automatic/manual checkout can
    # only catch up through yesterday; a future-night source key is never made.
    if service_date >= hotel_today():
        raise ValidationError("Accommodation revenue can only be posted after the service night has elapsed.")

    folio, _ = get_or_create_main_folio_for_stay(stay=stay, actor=actor)
    (net_accommodation, tax, service_fee), room_details = occupied_night_components(stay, service_date)
    total = (net_accommodation + tax + service_fee).quantize(CENT)
    if total <= Decimal("0.00"):
        # No occupied room on that night means no revenue and no receivable.
        return None, False

    lines = [
        {
            "account_code": accounting.ACCOUNTS_RECEIVABLE,
            "direction": FinancialLine.Direction.DEBIT,
            "amount": total,
            "folio": folio,
            "description": f"Accommodation for {service_date.isoformat()}",
        },
    ]
    if net_accommodation > Decimal("0.00"):
        lines.append(
            {
                "account_code": accounting.ACCOMMODATION_REVENUE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": net_accommodation,
                "description": f"Accommodation revenue for {service_date.isoformat()}",
            }
        )
    if tax > Decimal("0.00"):
        lines.append(
            {
                "account_code": accounting.TAX_PAYABLE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": tax,
                "description": f"Tax for accommodation {service_date.isoformat()}",
            }
        )
    if service_fee > Decimal("0.00"):
        lines.append(
            {
                "account_code": accounting.SERVICE_FEE_REVENUE,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": service_fee,
                "description": f"Service fee for accommodation {service_date.isoformat()}",
            }
        )
    source_key = f"accommodation:{stay.reference}:{service_date.isoformat()}"
    financial_transaction, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=booking.booking_reference,
        external_reference=stay.reference,
        narrative=f"Nightly accommodation charge for {service_date.isoformat()}",
        metadata={
            "stay_id": stay.pk,
            "stay_reference": stay.reference,
            "booking_reference": booking.booking_reference,
            "service_date": service_date.isoformat(),
            "night_index": (service_date - booking.check_in).days + 1,
            "occupied_rooms": room_details,
        },
        currency=booking.currency,
        business_date=service_date,
        lines=lines,
        postings=[
            {
                "line_index": 0,
                "folio": folio,
                "kind": FolioPosting.Kind.CHARGE,
                "effect": FolioPosting.Effect.DEBIT,
                "amount": total,
                "description": f"Accommodation — {service_date.isoformat()}",
                "source_reference": booking.booking_reference,
            }
        ],
    )
    # A2 correction: a recognised night releases the matching guest deposit to
    # the receivable (idempotent per payment). Money collected ahead of its
    # room nights stays in LIABILITY.GUEST_DEPOSITS until this point —
    # allocating it earlier credited the receivable before any charge existed.
    from .payment_collection_service import apply_unallocated_payments_for_booking

    apply_unallocated_payments_for_booking(booking=booking, folio=folio, actor=actor)
    return financial_transaction, created


@transaction.atomic
def post_due_accommodation_charges_for_stay(*, stay, through_date: date | None = None, actor=None) -> int:
    """Catch up elapsed nights for one stay, bounded and safely repeatable."""
    stay = Stay.objects.select_for_update().select_related("booking").get(pk=stay.pk)
    booking = stay.booking
    last_night = min(booking.check_out - timedelta(days=1), (through_date or (hotel_today() - timedelta(days=1))))
    if last_night < booking.check_in:
        return 0
    number_of_nights = (last_night - booking.check_in).days + 1
    if number_of_nights > MAX_CATCH_UP_NIGHTS:
        raise ValidationError("Stay exceeds the supported bounded nightly catch-up range; resolve through finance review.")
    created = 0
    for offset in range(number_of_nights):
        _, was_created = post_accommodation_charge_for_night(
            stay=stay,
            service_date=booking.check_in + timedelta(days=offset),
            actor=actor,
        )
        created += int(was_created)
    return created


def _due_stay_queryset(*, service_date: date, after_stay_id: int | None = None):
    """Return deterministic, index-friendly candidates for one hotel night."""
    queryset = Stay.objects.filter(
        status=Stay.Status.IN_HOUSE,
        expected_arrival__lte=service_date,
        expected_departure__gt=service_date,
    ).order_by("pk")
    if after_stay_id is not None:
        queryset = queryset.filter(pk__gt=after_stay_id)
    return queryset


def post_previous_night_accommodation_batch(
    *, service_date: date | None = None, batch_size=200, after_stay_id: int | None = None
) -> tuple[int, int | None]:
    """Post one bounded page and return a continuation cursor when needed.

    The continuation lets a large hotel process every in-house stay without an
    unbounded beat task. Each individual stay still has a source-keyed,
    idempotent transaction, so retrying a failed page is safe.
    """
    service_date = service_date or (hotel_today() - timedelta(days=1))
    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size must be between 1 and 500.")
    candidate_ids = list(
        _due_stay_queryset(service_date=service_date, after_stay_id=after_stay_id)
        .values_list("pk", flat=True)[: batch_size + 1]
    )
    stay_ids = candidate_ids[:batch_size]
    count = 0
    for stay_id in stay_ids:
        _, created = post_accommodation_charge_for_night(
            stay=Stay(pk=stay_id), service_date=service_date, actor=None
        )
        count += int(created)
    next_after_stay_id = stay_ids[-1] if len(candidate_ids) > batch_size else None
    return count, next_after_stay_id


def post_previous_night_accommodation(*, service_date: date | None = None, batch_size=200) -> int:
    """Run one bounded nightly page; Celery schedules continuations if needed."""
    count, _ = post_previous_night_accommodation_batch(
        service_date=service_date,
        batch_size=batch_size,
    )
    return count
