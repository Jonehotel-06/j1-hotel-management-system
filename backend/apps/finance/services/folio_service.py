"""Folio lifecycle and read-model helpers."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
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
    """Annotate a folio queryset with database-computed balances.

    ``DEBIT`` statement items increase what the guest owes; ``CREDIT`` items
    reduce it. This intentionally avoids a mutable cached balance column.

    BATCH 01 (task 7). Two things changed here.

    Only POSTED transactions count. Previously every posting was summed
    whatever the state of the transaction behind it, so a DRAFT charge that
    had not been approved, or a VOIDED one that had already been reversed by
    a balancing entry, still moved the guest's balance. A voided transaction
    was in fact counted twice: once as the original posting and once as the
    reversal.

    ``charges_total`` and ``credits_total`` are exposed next to the net
    figure. The net balance alone cannot distinguish a quiet folio from one
    carrying a large charge and an equally large credit, and the hotel needs
    to see both sides before it writes anything off.
    """
    queryset = queryset if queryset is not None else Folio.objects.all()
    money_field = DecimalField(max_digits=14, decimal_places=2)
    posted = Q(postings__transaction__status=FinancialTransaction.Status.POSTED)

    def _sum(condition):
        return Coalesce(
            Sum(Case(When(condition, then=F("postings__amount")), output_field=money_field)),
            Value(Decimal("0.00")),
            output_field=money_field,
        )

    charges = _sum(posted & Q(postings__effect=FolioPosting.Effect.DEBIT))
    credits = _sum(posted & Q(postings__effect=FolioPosting.Effect.CREDIT))
    return queryset.annotate(
        charges_total=charges,
        credits_total=credits,
    ).annotate(
        balance=Coalesce(
            F("charges_total") - F("credits_total"),
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
        # BATCH 01 (task 7). A VOIDED folio has been cancelled outright; its
        # postings are not a debt and must not be added to the booking.
        .exclude(folio__status=Folio.Status.VOIDED)
        # BATCH 01 (task 7). Only money in the booking's own currency may be
        # added to the booking's own balance. A foreign-currency posting used
        # to be summed as though the two were interchangeable; it is now left
        # out of this figure and reported separately by the receivables
        # service, which keeps each currency apart.
        .filter(currency=F("folio__booking__currency"))
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

    Never negative — an overpayment or a credit balance is not a debt. This
    clamp is correct for the desk, which uses the figure to decide how much
    to collect, and it is deliberately unchanged.

    It is the wrong figure for a report, because the clamp silently discards
    a credit the hotel owes back. Reporting callers should use
    ``guest_receivables_service.booking_balance_detail``, which returns this
    same number alongside the credit it suppressed.
    """
    stay_balance = Decimal(booking.amount_due or Decimal("0.00"))
    if folio_charge_balance is None:
        folio_charge_balance = booking_folio_charge_balance(booking=booking)
    total = (stay_balance + Decimal(folio_charge_balance)).quantize(Decimal("0.01"))
    return max(total, Decimal("0.00"))


# ---------------------------------------------------------------------------
# Controlled correction of a posted folio entry (repair 2026-11-26)
#
# The hotel could not correct a folio entry at all: a mistyped charge stayed on
# the guest's bill, because FolioPosting/FinancialTransaction are immutable and
# nothing offered the approved reversal the immutability requires. Deleting or
# editing the row is not an option (it would silently rewrite financial
# history), so this service posts an offsetting VOID journal and an offsetting
# statement item, keeps the original in place, and records who corrected what,
# why, and when.
# ---------------------------------------------------------------------------
VOIDABLE_POSTING_KINDS = frozenset({
    FolioPosting.Kind.CHARGE,
    FolioPosting.Kind.DISCOUNT,
    FolioPosting.Kind.ADJUSTMENT,
})


@transaction.atomic
def void_folio_posting(*, posting, actor, reason, allow_closed_folio=False, request=None):
    """Reverse one posted folio charge/credit with an approved void entry.

    Returns ``(reversal_transaction, created)``.

    Rules (all of them enforced here, not in the view):

    * only a ``CHARGE``, ``DISCOUNT`` or ``ADJUSTMENT`` statement item may be
      voided — payments and refunds have their own refund/void workflows and
      must not be reversed by this shortcut;
    * the folio must be OPEN, unless the caller has explicitly unlocked the
      closed-folio path (a capability decision made by the caller);
    * a reason of at least five characters is mandatory;
    * the original transaction must belong to this one folio posting, so the
      reversal cannot unbalance a journal shared with other postings;
    * the reversal is idempotent on the posting, so a double click or a retried
      request returns the reversal it already posted.

    Nothing is deleted and no posted row is edited: the ledger keeps both the
    original and its reversal, and ``folio_balance_queryset`` nets them.
    """
    from apps.audit.services import log_action
    from apps.core.approvals import override_event_details, require_independent_approver

    from ..models import FinancialEvent, FinancialLine
    from .ledger_service import create_posted_transaction

    reason = str(reason or "").strip()
    if len(reason) < 5:
        raise ValidationError({"reason": "Give the reason for voiding this entry (at least 5 characters)."})

    posting = (
        FolioPosting.objects.select_for_update()
        .select_related("folio", "transaction", "line")
        .get(pk=posting.pk)
    )
    if posting.reversal_of_id:
        raise ValidationError({"posting": "This entry is already a reversal and cannot be voided."})
    if posting.kind not in VOIDABLE_POSTING_KINDS:
        raise ValidationError({
            "posting": (
                f"A {posting.get_kind_display().lower()} entry cannot be voided here. "
                "Payments and refunds are corrected through their own refund/void workflow."
            )
        })
    folio = Folio.objects.select_for_update().get(pk=posting.folio_id)
    if folio.status != Folio.Status.OPEN and not allow_closed_folio:
        raise ValidationError({
            "folio": (
                f"Folio {folio.reference} is {folio.get_status_display().lower()}. Reopen it, or have an "
                "administrator perform the authorised correction."
            )
        })

    source_key = f"folio-void:{posting.pk}"
    existing = FinancialTransaction.objects.filter(source_key=source_key).first()
    if existing is not None:
        return existing, False

    original = posting.transaction
    if original.status != FinancialTransaction.Status.POSTED:
        raise ValidationError({"posting": "Only a posted entry can be voided."})
    siblings = list(FolioPosting.objects.filter(transaction=original).values_list("pk", flat=True))
    if siblings != [posting.pk]:
        raise ValidationError({
            "posting": (
                "This entry shares its journal with other folio entries. Use the source workflow "
                "(for example the POS order void or the accommodation correction) so the whole entry is "
                "reversed together and the journal stays balanced."
            )
        })

    evidence = require_independent_approver(
        requester=original.initiated_by, actor=actor, workflow="folio entry void",
    )
    if request is not None:
        from apps.audit.services import log_action as _log  # noqa: F401  (kept for parity with callers)

    lines = list(original.lines.order_by("pk"))
    mirror = [
        {
            "account_code": line.account_code,
            "direction": (
                FinancialLine.Direction.CREDIT if line.direction == FinancialLine.Direction.DEBIT
                else FinancialLine.Direction.DEBIT
            ),
            "amount": line.amount,
            # The mirrored line keeps its folio so the reversal's statement item
            # belongs to the same folio as its source line (the ledger refuses a
            # posting whose folio disagrees with its line).
            "folio": line.folio,
            "description": f"Void of {original.reference}: {reason}"[:500],
        }
        for line in lines
    ]
    line_index = next(
        (index for index, line in enumerate(lines) if line.pk == posting.line_id), 0,
    )
    reversal, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.VOID,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=folio.reference,
        external_reference=original.reference,
        narrative=f"Void of folio entry {posting.pk} ({posting.get_kind_display()}): {reason}"[:500],
        currency=posting.currency or folio.currency,
        reversal_of=original,
        metadata=override_event_details({
            "entry_kind": "FOLIO_POSTING_VOID",
            "folio_reference": folio.reference,
            "voided_posting_id": posting.pk,
            "voided_transaction": original.reference,
            "reason": reason,
        }, evidence),
        lines=mirror,
        postings=[{
            "line_index": line_index,
            "folio": folio,
            "kind": posting.kind,
            "effect": (
                FolioPosting.Effect.CREDIT if posting.effect == FolioPosting.Effect.DEBIT
                else FolioPosting.Effect.DEBIT
            ),
            "amount": posting.amount,
            "currency": posting.currency,
            "description": f"Void of {posting.description or original.reference}: {reason}"[:500],
            "source_reference": folio.reference,
            "reversal_of": posting,
        }],
    )
    if not created:
        return reversal, False

    FinancialEvent.objects.create(
        transaction=original, type=FinancialEvent.Type.REVERSED, actor=actor,
        details={
            "reversal_reference": reversal.reference, "reason": reason,
            "voided_posting_id": posting.pk, "folio_reference": folio.reference,
        },
    )
    log_action(
        actor=actor, action="FOLIO_POSTING_VOIDED", instance=folio, request=request,
        changes={"posting": [str(posting.pk), f"reversed by {reversal.reference}"]},
        metadata={
            "folio_reference": folio.reference, "voided_posting_id": posting.pk,
            "voided_transaction": original.reference, "reversal_transaction": reversal.reference,
            "amount": str(posting.amount), "kind": posting.kind, "effect": posting.effect,
            "reason": reason,
        },
        summary=f"Folio {folio.reference} entry {posting.pk} voided ({posting.kind} {posting.amount})",
    )
    return reversal, True
