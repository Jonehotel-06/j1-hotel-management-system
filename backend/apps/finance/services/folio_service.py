"""Folio lifecycle and read-model helpers."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, DecimalField, F, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.stays.models import Stay

from ..models import Folio, FolioPosting
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
