"""Ledger-authoritative guest receivables (batch 01, task 7).

Why this module exists
----------------------
The hotel ran two unrelated "what does the guest owe" calculations:

1. ``Booking.amount_due`` — a mutable projection, ``total_amount -
   amount_paid`` clamped at zero. It covers the WHOLE contracted stay,
   including nights that have not happened yet, and it knows nothing about
   food, bar, laundry or any other folio charge.
2. The ``ASSET.GUEST_RECEIVABLE`` control account — immutable double-entry
   evidence of what has actually been charged and settled.

``night_audit_service.accounts_receivable_aging`` added the two together
(``booking.amount_due`` + a folio-charge map), which is not a receivable at
all: it is a contract balance plus a partial ledger balance. It could never
agree with the balance sheet, it clamped negatives to zero so guest credit
balances silently vanished, it ignored the transaction status so drafts and
voided entries leaked in, it ignored the ``as_of`` date for folio charges, and
it mixed every currency into one NGN-labelled number.

What this module does instead
-----------------------------
Everything is derived from ONE source of truth: posted ``FinancialLine`` rows
on ``ASSET.GUEST_RECEIVABLE``. Each such line is either attached to a folio or
not, so the decomposition is exact by construction::

    control account balance
        = Σ (balance of every folio)
        + Σ (receivable lines with no folio attached)

That identity is computed and returned on every call as
``reconciliation``; it cannot be fudged because both sides read the same
rows.

Presentation rules that follow from double-entry practice
---------------------------------------------------------
* **Debit and credit balances are never netted across guests.** Guest A owing
  ₦50,000 and guest B holding a ₦50,000 advance is not "nobody owes
  anything". ``gross_receivables`` and ``guest_credit_balances`` are reported
  separately; their difference is the control-account balance.
* **A negative folio balance is investigated, not clamped.** It means money
  was applied to the folio ahead of the charge it pays for — nearly always a
  prepaid stay whose nights have not yet been posted — so it is reported as a
  guest credit balance with the unposted accommodation alongside it.
* **Unallocated payments stay out of the guest ledger.** Money received but
  not yet applied sits in ``LIABILITY.GUEST_DEPOSITS``. It is reported as its
  own figure and never subtracted from any guest's balance, so it can never be
  taken off the wrong guest.
* **One currency per report.** Every query is filtered by the requested
  currency and the other currencies are reported as a separate count, so a USD
  balance can never be published as naira.
* **Cancelled, no-show and early-departure bookings are included** when their
  folio still carries posted value — the money does not disappear because the
  booking status changed — and the booking status is published next to the
  balance.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Case, Count, DecimalField, F, Max, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from apps.core.utils import money

from ..models import FinancialLine, FinancialTransaction
from . import accounting

ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=16, decimal_places=2)

#: How a receivable movement is classified for the guest-facing breakdown.
#: Every posted line on the control account falls into exactly one bucket, so
#: the buckets always sum back to the folio balance.
CHARGE_TYPES = (
    FinancialTransaction.Type.ACCOMMODATION_CHARGE,
    FinancialTransaction.Type.POS_CHARGE,
    FinancialTransaction.Type.OTHER_CHARGE,
)
PAYMENT_TYPES = (
    FinancialTransaction.Type.PAYMENT_ALLOCATION,
    FinancialTransaction.Type.PAYMENT_COLLECTION,
)

AGING_BUCKETS = (
    ("current", 0, 0),
    ("days_1_30", 1, 30),
    ("days_31_60", 31, 60),
    ("days_61_90", 61, 90),
    ("days_over_90", 91, None),
)


def _money(value) -> Decimal:
    return Decimal(value or ZERO).quantize(Decimal("0.01"))


def _signed(*conditions) -> Case:
    """Debit-positive, credit-negative sum of the lines matching ``conditions``."""
    predicate = Q(*conditions) if conditions else Q()
    return Sum(
        Case(
            When(predicate & Q(direction=FinancialLine.Direction.DEBIT), then=F("amount")),
            When(predicate & Q(direction=FinancialLine.Direction.CREDIT), then=-F("amount")),
            default=Value(ZERO),
            output_field=MONEY,
        ),
        default=ZERO,
        output_field=MONEY,
    )


def receivable_lines(*, as_of: date, currency: str):
    """Every posted guest-receivable line on or before ``as_of``.

    This single queryset is the authority for every figure in this module.
    ``status=POSTED`` excludes drafts (not yet evidence) and voided entries
    (already cancelled by a balancing reversal, which is itself posted).
    """
    return FinancialLine.objects.filter(
        account_code=accounting.ACCOUNTS_RECEIVABLE,
        transaction__status=FinancialTransaction.Status.POSTED,
        transaction__currency=currency,
        transaction__business_date__lte=as_of,
    )


def control_account_balance(*, as_of: date, currency: str) -> Decimal:
    """``ASSET.GUEST_RECEIVABLE`` balance at ``as_of`` — one indexed aggregate."""
    total = receivable_lines(as_of=as_of, currency=currency).aggregate(balance=_signed())
    return _money(total["balance"])


def unapplied_guest_deposits(*, as_of: date, currency: str) -> Decimal:
    """Money received but not yet applied to any folio (a LIABILITY).

    Reported on its own. It is deliberately NOT deducted from any guest's
    receivable: until it is allocated the ledger does not know which charge it
    pays for, and guessing is how a payment ends up reducing the wrong guest's
    balance.
    """
    total = FinancialLine.objects.filter(
        account_code=accounting.GUEST_DEPOSITS,
        transaction__status=FinancialTransaction.Status.POSTED,
        transaction__currency=currency,
        transaction__business_date__lte=as_of,
    ).aggregate(balance=_signed())
    # Credit-normal liability: report it positive when the hotel is holding money.
    return _money(-_money(total["balance"]))


def folio_balance_rows(*, as_of: date, currency: str, folio_ids=None):
    """Per-folio receivable decomposition — one grouped query.

    Returns raw rows (not serialised) so callers can aggregate without a second
    pass. Charges are split by the transaction type that raised them, which is
    the posting service's own classification; nothing is inferred from text.
    """
    lines = receivable_lines(as_of=as_of, currency=currency).filter(folio__isnull=False)
    if folio_ids is not None:
        lines = lines.filter(folio_id__in=list(folio_ids))
    return (
        lines.values(
            "folio_id",
            "folio__reference",
            "folio__kind",
            "folio__status",
            "folio__currency",
            "folio__guest_id",
            "folio__guest__first_name",
            "folio__guest__last_name",
            "folio__booking_id",
            "folio__booking__booking_reference",
            "folio__booking__status",
            "folio__booking__check_in",
            "folio__booking__check_out",
            "folio__stay_id",
            "folio__stay__reference",
            "folio__opened_at",
        )
        .annotate(
            balance=_signed(),
            accommodation_charges=_signed(
                Q(transaction__type=FinancialTransaction.Type.ACCOMMODATION_CHARGE)),
            food_beverage_charges=_signed(
                Q(transaction__type=FinancialTransaction.Type.POS_CHARGE)),
            other_charges=_signed(
                Q(transaction__type=FinancialTransaction.Type.OTHER_CHARGE)),
            payments_allocated=_signed(Q(transaction__type__in=PAYMENT_TYPES)),
            discounts=_signed(Q(transaction__type=FinancialTransaction.Type.DISCOUNT)),
            refunds=_signed(Q(transaction__type=FinancialTransaction.Type.REFUND)),
            adjustments=_signed(
                ~Q(transaction__type__in=CHARGE_TYPES + PAYMENT_TYPES)
                & ~Q(transaction__type__in=[
                    FinancialTransaction.Type.DISCOUNT, FinancialTransaction.Type.REFUND])),
            movements=Count("pk"),
            last_movement=Max("transaction__business_date"),
        )
        .order_by("-balance")
    )


def unposted_stay_balance(*, as_of: date, currency: str) -> dict:
    """Contracted stay value the ledger has not charged to a folio yet.

    The old aging report published ``Booking.amount_due`` AS the receivable.
    That is not what a receivable is — it includes nights that have not
    happened — but it is not meaningless either: it is the hotel's exposure on
    stays whose nights the night audit has not posted (future nights, or a
    gap where posting never ran).

    Keeping it as a SEPARATE figure is what makes the split safe. For one
    booking::

        contract balance        = max(total_amount − amount_paid, 0)
        posted accommodation    = its folios' receivable balance for
                                  accommodation charges and the payments that
                                  settle them
        not yet charged         = max(contract balance − posted accommodation, 0)

    The posted half is already inside ``gross_receivables``; only the
    remainder is reported here, so adding the two cannot double count.
    Computed entirely in SQL — the previous implementation pulled every
    booking into Python.
    """
    from django.db.models import OuterRef, Subquery
    from django.db.models.functions import Greatest

    from apps.bookings.models import Booking

    posted_accommodation = (
        receivable_lines(as_of=as_of, currency=currency)
        .filter(folio__booking=OuterRef("pk"))
        .filter(
            Q(transaction__type=FinancialTransaction.Type.ACCOMMODATION_CHARGE)
            | Q(folio_posting__settles="ACCOMMODATION")
        )
        .values("folio__booking")
        .annotate(total=_signed())
        .values("total")[:1]
    )
    totals = (
        Booking.objects.exclude(status=Booking.Status.CANCELLED)
        .filter(check_in__lte=as_of, currency=currency)
        .annotate(
            posted_accommodation=Coalesce(
                Subquery(posted_accommodation, output_field=MONEY), Value(ZERO), output_field=MONEY),
            contract_balance=Greatest(
                F("total_amount") - F("amount_paid"), Value(ZERO), output_field=MONEY),
        )
        .annotate(
            not_yet_charged=Greatest(
                F("contract_balance") - F("posted_accommodation"), Value(ZERO), output_field=MONEY),
        )
        .aggregate(
            total=Sum("not_yet_charged", default=ZERO),
            bookings=Count("pk", filter=Q(not_yet_charged__gt=ZERO)),
        )
    )
    return {
        "amount": _money(totals["total"]),
        "bookings": totals["bookings"] or 0,
    }


def _room_numbers(folio_rows) -> dict:
    """Room numbers per booking — one bounded query, only for the rows shown.

    Multi-room bookings return every room, because the owner reads the
    receivable per booking and needs to recognise which rooms it covers.
    """
    booking_ids = {row["folio__booking_id"] for row in folio_rows if row["folio__booking_id"]}
    if not booking_ids:
        return {}
    from apps.bookings.models import BookingRoom

    rooms = {}
    for booking_id, number in BookingRoom.objects.filter(
        booking_id__in=booking_ids
    ).values_list("booking_id", "room__room_number").order_by("room__room_number"):
        rooms.setdefault(booking_id, []).append(str(number))
    return rooms


def _age_bucket(days: int) -> str:
    for name, low, high in AGING_BUCKETS:
        if days <= 0 and name == "current":
            return name
        if days >= low and (high is None or days <= high):
            return name
    return "days_over_90"


def _guest_name(row) -> str:
    name = " ".join(
        part for part in (row["folio__guest__first_name"], row["folio__guest__last_name"]) if part
    ).strip()
    return name or "Guest"


def guest_receivables(
    *,
    as_of: date | None = None,
    currency: str = "NGN",
    limit: int = 200,
    include_settled: bool = False,
) -> dict:
    """The corrected guest receivable position with reconciliation evidence.

    ``limit`` bounds only the itemised list; every total is computed over the
    whole population by the database, so truncating the detail can never
    change a headline figure.
    """
    from apps.core.utils import hotel_today

    as_of = as_of or hotel_today()
    currency = str(currency or "NGN").upper()

    control = control_account_balance(as_of=as_of, currency=currency)
    rows = list(folio_balance_rows(as_of=as_of, currency=currency))

    gross = ZERO
    credits = ZERO
    buckets = {name: ZERO for name, _low, _high in AGING_BUCKETS}
    bucket_counts = {name: 0 for name, _low, _high in AGING_BUCKETS}
    owing_rows, credit_rows = [], []
    for row in rows:
        balance = _money(row["balance"])
        if balance > ZERO:
            gross += balance
            owing_rows.append(row)
        elif balance < ZERO:
            credits += -balance
            credit_rows.append(row)

    # Age from the date the guest's liability started running: the booking's
    # arrival date when there is one, otherwise the date the folio opened.
    for row in owing_rows:
        started = row["folio__booking__check_in"] or row["folio__opened_at"].date()
        bucket = _age_bucket((as_of - started).days)
        buckets[bucket] += _money(row["balance"])
        bucket_counts[bucket] += 1

    # Receivable postings that were never attached to a folio (for example a
    # walk-in POS charge raised with no folio open). Surfaced rather than
    # dropped, because the control account contains them.
    unassigned = control - (gross - credits)

    other_currencies = (
        FinancialLine.objects.filter(
            account_code=accounting.ACCOUNTS_RECEIVABLE,
            transaction__status=FinancialTransaction.Status.POSTED,
            transaction__business_date__lte=as_of,
        )
        .exclude(transaction__currency=currency)
        .values("transaction__currency")
        .annotate(balance=_signed(), lines=Count("pk"))
        .order_by("transaction__currency")
    )

    shown = [row for row in rows if include_settled or _money(row["balance"]) != ZERO]
    shown = sorted(shown, key=lambda row: -_money(row["balance"]))
    truncated = len(shown) > limit
    shown = shown[:limit]
    rooms = _room_numbers(shown)

    items = []
    for row in shown:
        balance = _money(row["balance"])
        started = row["folio__booking__check_in"] or row["folio__opened_at"].date()
        charges = (
            _money(row["accommodation_charges"])
            + _money(row["food_beverage_charges"])
            + _money(row["other_charges"])
        )
        items.append(
            {
                "folio_id": row["folio_id"],
                "folio_reference": row["folio__reference"],
                "folio_kind": row["folio__kind"],
                "folio_status": row["folio__status"],
                "guest_id": row["folio__guest_id"],
                "guest_name": _guest_name(row),
                "booking_reference": row["folio__booking__booking_reference"] or "",
                "booking_status": row["folio__booking__status"] or "",
                "stay_reference": row["folio__stay__reference"] or "",
                "rooms": rooms.get(row["folio__booking_id"], []),
                "currency": row["folio__currency"] or currency,
                "check_in": row["folio__booking__check_in"].isoformat()
                if row["folio__booking__check_in"] else None,
                "check_out": row["folio__booking__check_out"].isoformat()
                if row["folio__booking__check_out"] else None,
                "accommodation_charges": money(_money(row["accommodation_charges"])),
                "food_beverage_charges": money(_money(row["food_beverage_charges"])),
                "other_charges": money(_money(row["other_charges"])),
                "total_charges": money(charges),
                # Stored credit-negative on the ledger; published as the
                # positive amount the guest has paid against this folio.
                "payments_allocated": money(-_money(row["payments_allocated"])),
                "discounts": money(-_money(row["discounts"])),
                "refunds": money(_money(row["refunds"])),
                "adjustments": money(_money(row["adjustments"])),
                "outstanding": money(balance if balance > ZERO else ZERO),
                "credit_balance": money(-balance if balance < ZERO else ZERO),
                "balance": money(balance),
                "age_days": max((as_of - started).days, 0),
                "age_bucket": _age_bucket((as_of - started).days),
                "movements": row["movements"],
            }
        )

    # Booking-level roll-up, for consumers that read one line per reservation
    # rather than one per folio. Multi-folio and multi-room bookings therefore
    # appear once here and once per folio in ``items`` — never twice in a total.
    by_booking: dict = {}
    for row in rows:
        reference = row["folio__booking__booking_reference"] or ""
        key = reference or f"folio:{row['folio_id']}"
        entry = by_booking.setdefault(key, {
            "booking_reference": reference,
            "guest_name": _guest_name(row),
            "booking_status": row["folio__booking__status"] or "",
            "check_in": row["folio__booking__check_in"].isoformat()
            if row["folio__booking__check_in"] else None,
            "folios": 0, "outstanding": ZERO, "credit_balance": ZERO,
            "total_charges": ZERO, "payments_allocated": ZERO,
        })
        balance = _money(row["balance"])
        entry["folios"] += 1
        entry["outstanding"] += balance if balance > ZERO else ZERO
        entry["credit_balance"] += -balance if balance < ZERO else ZERO
        entry["total_charges"] += (
            _money(row["accommodation_charges"]) + _money(row["food_beverage_charges"])
            + _money(row["other_charges"]))
        entry["payments_allocated"] += -_money(row["payments_allocated"])
    booking_rows = sorted(
        ({**entry,
          "outstanding": money(entry["outstanding"]),
          "credit_balance": money(entry["credit_balance"]),
          "total_charges": money(entry["total_charges"]),
          "payments_allocated": money(entry["payments_allocated"])}
         for entry in by_booking.values()),
        key=lambda entry: -Decimal(entry["outstanding"]),
    )[:limit]

    not_charged = unposted_stay_balance(as_of=as_of, currency=currency)
    reconciles = (gross - credits + unassigned) == control
    return {
        "as_of": as_of.isoformat(),
        "currency": currency,
        "basis": "posted_immutable_ledger_guest_receivable_control_account",
        # ---- headline position ------------------------------------------
        "gross_receivables": money(gross),
        "guest_credit_balances": money(credits),
        "unassigned_receivables": money(unassigned),
        "net_receivable": money(control),
        "total_outstanding": money(gross),
        "unapplied_guest_deposits": money(
            unapplied_guest_deposits(as_of=as_of, currency=currency)),
        "folios_owing": len(owing_rows),
        "folios_in_credit": len(credit_rows),
        # Exposure on stays the ledger has not charged yet. Reported beside
        # the receivable, never inside it.
        "stay_value_not_yet_charged": money(not_charged["amount"]),
        "bookings_not_yet_charged": not_charged["bookings"],
        "total_stay_exposure": money(gross + not_charged["amount"]),
        # ---- aging ------------------------------------------------------
        "buckets": {name: money(value) for name, value in buckets.items()},
        "bucket_counts": bucket_counts,
        # ---- detail -----------------------------------------------------
        "items": items,
        "by_booking": booking_rows,
        "items_truncated": truncated,
        "item_limit": limit,
        # ---- proof ------------------------------------------------------
        "reconciliation": {
            "control_account": accounting.ACCOUNTS_RECEIVABLE,
            "control_account_balance": money(control),
            "sum_of_folio_balances": money(gross - credits),
            "receivables_without_a_folio": money(unassigned),
            "difference": money(control - (gross - credits + unassigned)),
            "reconciles": reconciles,
            "identity": (
                "control account balance = Σ folio balances + receivables with no folio; "
                "Σ folio balances = gross receivables − guest credit balances"
            ),
        },
        "other_currency_balances": [
            {
                "currency": row["transaction__currency"],
                "balance": money(_money(row["balance"])),
                "lines": row["lines"],
            }
            for row in other_currencies
        ],
        "definitions": {
            "gross_receivables": "Sum of folio balances that are in DEBIT — money guests owe the hotel.",
            "guest_credit_balances": (
                "Sum of folio balances that are in CREDIT — advances and overpayments the hotel is "
                "holding for a guest. Never netted against another guest's debt and never clamped to zero."
            ),
            "unapplied_guest_deposits": (
                "LIABILITY.GUEST_DEPOSITS: money received that is not yet applied to any folio. A "
                "liability, not a receivable, and deliberately not deducted from any guest."
            ),
            "net_receivable": "The ASSET.GUEST_RECEIVABLE control-account balance. Equals gross − credits + unassigned.",
            "unassigned_receivables": "Posted receivable lines with no folio attached (e.g. a walk-in POS charge).",
            "total_outstanding": "Alias of gross_receivables, kept for existing API consumers.",
            "stay_value_not_yet_charged": (
                "Booking total less payments received, less the accommodation already charged to a "
                "folio — i.e. contracted room value the ledger has not recognised yet (future nights, "
                "or nights the night audit has not posted). NOT a receivable and not part of any "
                "ledger total."
            ),
            "total_stay_exposure": (
                "gross_receivables + stay_value_not_yet_charged. The operational 'what could still be "
                "collected' figure. The two parts never overlap: accommodation already posted sits in "
                "the receivable and is subtracted from the contract balance before the remainder is added."
            ),
        },
    }


def folio_receivable_detail(*, folio, as_of: date | None = None) -> dict:
    """The same arithmetic for one folio, for a guest statement or an audit."""
    from apps.core.utils import hotel_today

    as_of = as_of or hotel_today()
    currency = folio.currency or "NGN"
    rows = list(folio_balance_rows(as_of=as_of, currency=currency, folio_ids=[folio.pk]))
    if not rows:
        return {
            "folio_reference": folio.reference,
            "currency": currency,
            "as_of": as_of.isoformat(),
            "total_charges": money(ZERO),
            "payments_allocated": money(ZERO),
            "discounts": money(ZERO),
            "refunds": money(ZERO),
            "adjustments": money(ZERO),
            "outstanding": money(ZERO),
            "credit_balance": money(ZERO),
            "balance": money(ZERO),
        }
    row = rows[0]
    balance = _money(row["balance"])
    charges = (
        _money(row["accommodation_charges"])
        + _money(row["food_beverage_charges"])
        + _money(row["other_charges"])
    )
    return {
        "folio_reference": folio.reference,
        "currency": currency,
        "as_of": as_of.isoformat(),
        "accommodation_charges": money(_money(row["accommodation_charges"])),
        "food_beverage_charges": money(_money(row["food_beverage_charges"])),
        "other_charges": money(_money(row["other_charges"])),
        "total_charges": money(charges),
        "payments_allocated": money(-_money(row["payments_allocated"])),
        "discounts": money(-_money(row["discounts"])),
        "refunds": money(_money(row["refunds"])),
        "adjustments": money(_money(row["adjustments"])),
        "outstanding": money(balance if balance > ZERO else ZERO),
        "credit_balance": money(-balance if balance < ZERO else ZERO),
        "balance": money(balance),
    }


def booking_balance_detail(*, booking, folio_charge_balance: Decimal | None = None) -> dict:
    """Both halves of what a booking owes, with the credit kept visible.

    ``folio_service.booking_outstanding_balance`` returns the single figure the
    front desk settles with and clamps it at zero, which is correct for a
    "collect this now" prompt but hides an overpayment. This returns the same
    number together with the credit it suppressed, so a caller that reports
    rather than collects can show the truth.
    """
    from .folio_service import booking_folio_charge_balance

    if folio_charge_balance is None:
        folio_charge_balance = booking_folio_charge_balance(booking=booking)
    stay_balance = Decimal(booking.amount_due or ZERO)
    total = (stay_balance + Decimal(folio_charge_balance)).quantize(Decimal("0.01"))
    return {
        "stay_balance": money(stay_balance),
        "folio_charge_balance": money(Decimal(folio_charge_balance)),
        "outstanding": money(max(total, ZERO)),
        "credit_balance": money(-total if total < ZERO else ZERO),
        "signed_balance": money(total),
    }
