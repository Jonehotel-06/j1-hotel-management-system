"""Controlled manual journal entries and cash safe drops.

Manual journals post through the same balanced, immutable ledger service as
every operational transaction — there is no direct ledger editing path. Only
accounts in the stable chart may be used, debits must equal credits, and the
client-supplied idempotency key makes retries safe.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.utils import hotel_today

from ..models import CashMovement, CashSession, FinancialEvent, FinancialLine, FinancialTransaction
from . import accounting
from .cash_session_service import _amount
from .ledger_service import create_posted_transaction

CHART_OF_ACCOUNTS = [
    (accounting.CASH_ON_HAND, "Cash on hand"),
    (accounting.BANK_CLEARING, "Bank clearing"),
    (accounting.CARD_CLEARING, "Card clearing"),
    (accounting.ACCOUNTS_RECEIVABLE, "Guest receivables"),
    (accounting.INVENTORY_ASSET, "Inventory asset"),
    (accounting.GUEST_DEPOSITS, "Guest deposits"),
    (accounting.ACCOUNTS_PAYABLE, "Accounts payable"),
    (accounting.ACCOMMODATION_REVENUE, "Accommodation revenue"),
    (accounting.FOOD_BEVERAGE_REVENUE, "Food & beverage revenue"),
    (accounting.OTHER_OPERATING_REVENUE, "Other operating revenue"),
    (accounting.SERVICE_FEE_REVENUE, "Service fee revenue"),
    (accounting.DISCOUNTS_ALLOWED, "Discounts allowed"),
    (accounting.REFUNDS_ISSUED, "Refunds issued"),
    (accounting.OPERATING_EXPENSE, "Operating expense"),
    (accounting.PAYROLL_EXPENSE, "Payroll expense"),
    (accounting.PAYROLL_PAYABLE, "Payroll payable"),
    (accounting.CASH_OVER_SHORT, "Cash over / short"),
    (accounting.CASH_FLOAT, "Cash float"),
    (accounting.STATUTORY_TAX_ABSORBED, "Statutory tax absorbed (hotel-borne)"),
    (accounting.STATUTORY_TAX_PAYABLE, "Statutory tax payable"),
]

ACCOUNT_CODES = {code for code, _label in CHART_OF_ACCOUNTS}


def chart_of_accounts() -> list[dict]:
    return [{"code": code, "label": label} for code, label in CHART_OF_ACCOUNTS]


def post_manual_journal(*, actor, lines, narrative, business_date=None, idempotency_key, source_reference=""):
    """Post one balanced ADJUSTMENT journal; returns (transaction, created)."""
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "An idempotency key is required."})
    narrative = str(narrative or "").strip()
    if not narrative:
        raise ValidationError({"narrative": "A journal narrative explaining the adjustment is required."})
    business_date = business_date or hotel_today()
    if business_date > hotel_today():
        raise ValidationError({"business_date": "A journal cannot be dated in the future."})
    normalised = []
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")
    for index, line in enumerate(lines or []):
        code = str(line.get("account_code") or "").strip()
        if code not in ACCOUNT_CODES:
            raise ValidationError({"lines": f"Line {index + 1}: unknown account code."})
        direction = str(line.get("direction") or "").strip().upper()
        if direction not in {FinancialLine.Direction.DEBIT, FinancialLine.Direction.CREDIT}:
            raise ValidationError({"lines": f"Line {index + 1}: direction must be DEBIT or CREDIT."})
        amount = _amount(line.get("amount"), allow_zero=False)
        if direction == FinancialLine.Direction.DEBIT:
            total_debits += amount
        else:
            total_credits += amount
        normalised.append({
            "account_code": code,
            "direction": direction,
            "amount": amount,
            "description": str(line.get("description") or "")[:255],
        })
    if len(normalised) < 2:
        raise ValidationError({"lines": "A journal needs at least two lines."})
    if total_debits != total_credits:
        raise ValidationError({
            "lines": f"Journal is not balanced: debits {total_debits} do not equal credits {total_credits}."
        })
    financial_transaction, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.ADJUSTMENT,
        source_key=f"manual-journal:{actor.pk}:{key}",
        idempotency_key=f"manual-journal:{actor.pk}:{key}",
        actor=actor,
        source_reference=str(source_reference or "")[:120] or f"manual-journal:{key}"[:120],
        narrative=narrative[:500],
        currency="NGN",
        business_date=business_date,
        metadata={"entry_kind": "MANUAL_JOURNAL", "posted_by": actor.pk},
        lines=normalised,
    )
    if created:
        FinancialEvent.objects.create(
            transaction=financial_transaction,
            type=FinancialEvent.Type.POSTED,
            actor=actor,
            details={"reason": "manual_journal", "narrative": narrative[:500]},
        )
    return financial_transaction, created


@transaction.atomic
def record_cash_drop(*, cash_session, actor, amount, notes=""):
    """Move cash from an open drawer to the safe, with an audit record.

    Only the session's own cashier may drop cash, and never more than the
    drawer's expected cash. The drop reduces expected cash so the closing
    count reconciles against what physically remains in the drawer.
    """
    session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
    if session.cashier_id != actor.pk:
        raise ValidationError("Only the assigned cashier can record a cash drop for this session.")
    if session.status != CashSession.Status.OPEN:
        raise ValidationError("Cash drops can only be recorded while the session is open.")
    amount = _amount(amount, allow_zero=False)
    if amount > Decimal(session.expected_cash or 0):
        raise ValidationError("Cash drop cannot exceed the drawer's expected cash.")
    movement = CashMovement.objects.create(
        cash_session=session,
        type=CashMovement.Type.SAFE_DROP,
        amount=amount,
        actor=actor,
        source_reference=session.reference,
        notes=(str(notes or "").strip() or "Cash drop to safe")[:500],
    )
    session.expected_cash = (Decimal(session.expected_cash or 0) - amount).quantize(Decimal("0.01"))
    session.save(update_fields=["expected_cash", "updated_at"])
    FinancialEvent.objects.create(
        cash_session=session,
        type=FinancialEvent.Type.CREATED,
        actor=actor,
        details={"reason": "cash_safe_drop", "amount": str(amount), "expected_cash_after": str(session.expected_cash)},
    )
    return movement
