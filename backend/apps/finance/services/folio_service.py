"""Folio lifecycle and read-model helpers."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, DecimalField, F, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.stays.models import Stay

from ..models import FinancialTransaction, Folio, FolioPosting
from .references import generate_finance_reference


@transaction.atomic
def get_or_create_main_folio_for_stay(*, stay, actor=None) -> tuple[Folio, bool]:
    """Open the one main guest folio for a stay, safely and idempotently.

    Locking the stay serializes repeated check-in requests and protects the
    database's one-(stay, kind) invariant on engines where a concurrent absent
    row cannot be locked directly.
    """
    stay = Stay.objects.select_for_update().select_related("booking", "guest").get(pk=stay.pk)
    existing = (
        Folio.objects.select_for_update()
        .filter(stay=stay, kind=Folio.Kind.MAIN)
        .first()
    )
    if existing:
        if existing.status == Folio.Status.VOIDED:
            raise ValidationError("The main folio for this stay was voided and cannot be reopened automatically.")
        return existing, False

    folio = Folio.objects.create(
        reference=generate_finance_reference("FOL"),
        stay=stay,
        booking=stay.booking,
        guest=stay.guest,
        kind=Folio.Kind.MAIN,
        status=Folio.Status.OPEN,
        currency=stay.booking.currency,
        opened_by=actor,
    )
    return folio, True


def folio_balance_queryset(queryset=None):
    """Annotate a folio queryset with a database-computed signed balance.

    ``DEBIT`` statement items increase what the guest owes; ``CREDIT`` items
    reduce it. This intentionally avoids a mutable cached balance column.
    """
    queryset = queryset if queryset is not None else Folio.objects.all()
    money_field = DecimalField(max_digits=14, decimal_places=2)
    return queryset.annotate(
        balance=Coalesce(
            Sum(
                Case(
                    When(postings__effect=FolioPosting.Effect.DEBIT, then=F("postings__amount")),
                    When(postings__effect=FolioPosting.Effect.CREDIT, then=-F("postings__amount")),
                    default=Value(Decimal("0.00")),
                    output_field=money_field,
                )
            ),
            Value(Decimal("0.00")),
            output_field=money_field,
        )
    )


@transaction.atomic
def close_folio(*, folio, actor, allow_nonzero_balance=False):
    """Close a folio only when operationally appropriate.

    Checkout callers should pass ``allow_nonzero_balance=False`` so unpaid
    balances cannot be silently hidden. A supervised settlement workflow can
    deliberately opt in later.
    """
    folio = Folio.objects.select_for_update().get(pk=folio.pk)
    if folio.status == Folio.Status.CLOSED:
        return folio
    if folio.status != Folio.Status.OPEN:
        raise ValidationError(f"Folio {folio.reference} cannot close from {folio.status}.")
    balance = folio_balance_queryset(Folio.objects.filter(pk=folio.pk)).values_list("balance", flat=True).get()
    if not allow_nonzero_balance and balance != Decimal("0.00"):
        raise ValidationError("A non-zero folio cannot be closed without a controlled settlement decision.")
    folio.status = Folio.Status.CLOSED
    folio.closed_at = timezone.now()
    folio.closed_by = actor
    folio.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    return folio


# ---------------------------------------------------------------------------
# Authoritative guest balance (audit D4)
#
# The booking projection (`total_amount - amount_paid`) prices the STAY. It does
# not include the restaurant, bar, room-service and other authorised charges the
# guest actually ran up: those are posted to the guest's folio as
# POS_CHARGE / OTHER_CHARGE ledger transactions numbered in one place — the
# immutable ledger. A folio with ₦25,500 of such charges therefore left the
# booking's displayed balance ₦25,500 short.
#
# ACCOMMODATION_CHARGE postings are DELIBERATELY EXCLUDED here: they are the
# nightly split of the booking's own `total_amount` (see
# accommodation_service._night_components), so including them would count the
# room twice.
#
# MONEY THAT SETTLES THE ACCOMMODATION IS EXCLUDED FOR THE SAME REASON. A
# payment for the room is posted to the folio as an AR credit (so the folio's
# own statement is complete) *and* counted in `booking.amount_paid`/`amount_due`
# — the projection the desk settles the room with. Netting that credit against
# the guest's purchases as well counted the same money twice and produced the
# reported defect: a fully paid ₦420,000 room with an unpaid ₦3,200 laundry
# charge showed "purchases and charges −₦416,800" and "amount to settle ₦0",
# and the gate pass printed as cleared. Such postings carry
# ``settles=FolioPosting.Settles.ACCOMMODATION``. Money the guest handed over
# FOR their folio charges (the desk settlement) is classified
# ``FOLIO_CHARGES`` and still reduces them; so do discounts, adjustments,
# reversals and refunds, which is why this is not "ignore all payment credits".
#
# Only POSTED (and not reversed) transactions contribute: drafts are not
# liabilities and a VOIDED transaction has been reversed by a balancing entry.
# ---------------------------------------------------------------------------
def booking_folio_charge_map(*, booking_ids):
    """Signed extra-charge balance per booking id — exactly one query.

    Debits (posted guest charges) increase the balance; credits (controlled
    discounts, adjustments, reversals, and payments applied to the folio
    charges) reduce it. Postings that settle the ACCOMMODATION are excluded:
    that money is already reflected in the booking's own paid-to-date figure.
    Returns only bookings that actually have such postings; callers treat a
    missing key as zero.
    """
    ids = [pk for pk in dict.fromkeys(booking_ids or []) if pk]
    if not ids:
        return {}
    money_field = DecimalField(max_digits=14, decimal_places=2)
    rows = (
        FolioPosting.objects.filter(folio__booking_id__in=ids)
        .exclude(transaction__type=FinancialTransaction.Type.ACCOMMODATION_CHARGE)
        # Accommodation money (a paid room) must not be netted against the
        # guest's unrelated purchases and service charges.
        .exclude(settles=FolioPosting.Settles.ACCOMMODATION)
        .filter(transaction__status=FinancialTransaction.Status.POSTED)
        .values("folio__booking_id")
        .annotate(
            balance=Coalesce(
                Sum(
                    Case(
                        When(effect=FolioPosting.Effect.DEBIT, then=F("amount")),
                        When(effect=FolioPosting.Effect.CREDIT, then=-F("amount")),
                        default=Value(Decimal("0.00")),
                        output_field=money_field,
                    )
                ),
                Value(Decimal("0.00")),
                output_field=money_field,
            )
        )
    )
    return {row["folio__booking_id"]: row["balance"] for row in rows}


def booking_folio_charge_balance(*, booking) -> Decimal:
    """Convenience wrapper for a single booking (used outside list rendering)."""
    if booking is None or booking.pk is None:
        return Decimal("0.00")
    return booking_folio_charge_map(booking_ids=[booking.pk]).get(booking.pk, Decimal("0.00"))


def booking_outstanding_balance(*, booking, folio_charge_balance: Decimal | None = None) -> Decimal:
    """What the guest still owes: stay balance + posted folio charges.

    Never negative — an overpayment or a credit balance is not a debt.
    """
    stay_balance = Decimal(booking.amount_due or Decimal("0.00"))
    if folio_charge_balance is None:
        folio_charge_balance = booking_folio_charge_balance(booking=booking)
    total = (stay_balance + Decimal(folio_charge_balance)).quantize(Decimal("0.01"))
    return max(total, Decimal("0.00"))
