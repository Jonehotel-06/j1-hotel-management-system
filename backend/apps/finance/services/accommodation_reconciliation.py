"""Detect and repair gaps in nightly accommodation recognition (audit D4/N1).

Room revenue and its tax are recognized one night at a time by
``post_accommodation_charge_for_night`` under the stable source key
``accommodation:{stay}:{night}``. Two things drive that: checkout catch-up and
the nightly Celery beat task. A stay that reached ``CHECKED_OUT`` without either
path — a bridged/legacy checkout, an interrupted deploy, a worker that was not
running, or a stay that never went through the operational checkout at all —
leaves the room's revenue and tax off the ledger even though the booking, the
folio and the guest still carry the money.

This module reports those gaps and posts the missing nights through the same
canonical, idempotent service. Amounts are always derived from the booking's own
``total_amount`` / ``tax_amount`` / ``fee_amount`` split: nothing is invented,
nothing is posted twice, and a second run is a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q

from apps.core.utils import hotel_today
from apps.stays.models import Stay

from ..models import FinancialTransaction
from .accommodation_service import night_components, post_due_accommodation_charges_for_stay

ZERO = Decimal("0.00")
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


@dataclass(frozen=True)
class StayNightGap:
    """One operational stay whose elapsed nights are not fully on the ledger."""

    stay_id: int
    stay_reference: str
    booking_reference: str
    stay_status: str
    nights_expected: int
    missing_nights: tuple[date, ...]
    net_amount: Decimal
    tax_amount: Decimal
    fee_amount: Decimal

    @property
    def total_amount(self) -> Decimal:
        return self.net_amount + self.tax_amount + self.fee_amount

    @property
    def nights_missing(self) -> int:
        return len(self.missing_nights)

    def as_dict(self) -> dict:
        """JSON-safe, PII-free description for command output and evidence."""
        return {
            "stay_reference": self.stay_reference,
            "booking_reference": self.booking_reference,
            "stay_status": self.stay_status,
            "nights_expected": self.nights_expected,
            "nights_missing": self.nights_missing,
            "first_missing_night": self.missing_nights[0].isoformat(),
            "last_missing_night": self.missing_nights[-1].isoformat(),
            "net_amount": str(self.net_amount.quantize(Decimal("0.01"))),
            "tax_amount": str(self.tax_amount.quantize(Decimal("0.01"))),
            "fee_amount": str(self.fee_amount.quantize(Decimal("0.01"))),
            "total_amount": str(self.total_amount.quantize(Decimal("0.01"))),
        }


def expected_nights(*, booking, through_date: date) -> list[date]:
    """Booked nights that have already elapsed and may be recognized."""
    last_night = min(booking.check_out - timedelta(days=1), through_date)
    if last_night < booking.check_in:
        return []
    return [booking.check_in + timedelta(days=offset)
            for offset in range((last_night - booking.check_in).days + 1)]


def accommodation_night_gaps(*, through_date: date | None = None, stay_reference: str | None = None,
                             since: date | None = None, limit: int = DEFAULT_LIMIT) -> list[StayNightGap]:
    """Stays with elapsed nights missing from the ledger, oldest stay first.

    Two queries regardless of stay count: one for the candidate stays and one
    for every accommodation source key already posted against them.
    """
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}.")
    through_date = through_date or (hotel_today() - timedelta(days=1))
    queryset = (
        Stay.objects.filter(
            status__in=(Stay.Status.IN_HOUSE, Stay.Status.CHECKED_OUT),
            expected_arrival__lte=through_date,
        )
        .select_related("booking")
        .order_by("pk")
    )
    if stay_reference:
        queryset = queryset.filter(reference=stay_reference)
    if since:
        queryset = queryset.filter(expected_arrival__gte=since)
    stays = list(queryset[:limit])
    if not stays:
        return []

    stay_refs = [stay.reference for stay in stays]
    booking_refs = [stay.booking.booking_reference for stay in stays]
    posted_keys = set(
        FinancialTransaction.objects.filter(
            type=FinancialTransaction.Type.ACCOMMODATION_CHARGE,
        )
        .filter(Q(external_reference__in=stay_refs) | Q(source_reference__in=booking_refs))
        .values_list("source_key", flat=True)
    )

    gaps: list[StayNightGap] = []
    for stay in stays:
        booking = stay.booking
        nights = expected_nights(booking=booking, through_date=through_date)
        missing: list[date] = []
        net = tax = fee = ZERO
        for night in nights:
            if f"accommodation:{stay.reference}:{night.isoformat()}" in posted_keys:
                continue
            night_net, night_tax, night_fee = night_components(booking, night)
            net += night_net
            tax += night_tax
            fee += night_fee
            missing.append(night)
        if not missing:
            continue
        gaps.append(
            StayNightGap(
                stay_id=stay.pk,
                stay_reference=stay.reference,
                booking_reference=booking.booking_reference,
                stay_status=stay.status,
                nights_expected=len(nights),
                missing_nights=tuple(missing),
                net_amount=net,
                tax_amount=tax,
                fee_amount=fee,
            )
        )
    return gaps


def post_missing_accommodation_charges(gaps, *, through_date: date | None = None,
                                       actor=None) -> tuple[int, list[str]]:
    """Post the missing nights for each gap; returns (created_count, failures).

    Each stay is repaired through the canonical service, which locks the stay,
    re-checks eligibility and relies on the per-night source key for
    idempotency. One failing stay never aborts the rest of the run.
    """
    created = 0
    failures: list[str] = []
    for gap in gaps:
        try:
            created += post_due_accommodation_charges_for_stay(
                stay=Stay(pk=gap.stay_id), through_date=through_date, actor=actor
            )
        except Exception as exc:  # noqa: BLE001 - report and continue; run is supervised
            failures.append(f"{gap.stay_reference}: {exc}")
    return created, failures
