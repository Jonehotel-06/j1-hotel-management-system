"""Transactional immutable double-entry posting service.

No HTTP view should directly create a ``FinancialTransaction`` or line. This
service makes balance validation, idempotency, append-only event evidence, and
posting state changes occur in one database transaction.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Mapping

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.utils import hotel_today

from ..models import FinancialEvent, FinancialLine, FinancialTransaction, FolioPosting
from . import accounting
from .references import generate_finance_reference

CENT = Decimal("0.01")


class LedgerIntegrityError(ValidationError):
    """Raised when an attempted accounting entry violates a ledger invariant."""


def _money(value, *, label="amount") -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LedgerIntegrityError({label: "A valid two-decimal money value is required."}) from exc
    if amount <= Decimal("0.00"):
        raise LedgerIntegrityError({label: "Amount must be greater than zero."})
    return amount


def _sum_lines(lines: Iterable[FinancialLine], direction: str) -> Decimal:
    return sum((line.amount for line in lines if line.direction == direction), Decimal("0.00")).quantize(CENT)


def _validate_balanced(lines: list[FinancialLine]):
    if len(lines) < 2:
        raise LedgerIntegrityError("A financial transaction needs at least one debit and one credit line.")
    debit_total = _sum_lines(lines, FinancialLine.Direction.DEBIT)
    credit_total = _sum_lines(lines, FinancialLine.Direction.CREDIT)
    if debit_total <= 0 or credit_total <= 0 or debit_total != credit_total:
        raise LedgerIntegrityError(
            f"Financial transaction is not balanced: debit {debit_total} does not equal credit {credit_total}."
        )


def _validate_folio_postings(*, financial_transaction):
    """Keep statement balance effects exactly aligned to immutable GL lines."""
    postings = list(
        FolioPosting.objects.select_for_update()
        .select_related("line")
        .filter(transaction=financial_transaction)
        .order_by("pk")
    )
    for posting in postings:
        line = posting.line
        if line.transaction_id != financial_transaction.pk:
            raise LedgerIntegrityError("A folio posting points outside its financial transaction.")
        if line.folio_id != posting.folio_id:
            raise LedgerIntegrityError("A folio posting must use the same folio as its source financial line.")
        if posting.amount != line.amount:
            raise LedgerIntegrityError("A folio posting amount must equal its source financial line.")
        if line.account_code != accounting.ACCOUNTS_RECEIVABLE:
            raise LedgerIntegrityError("A folio posting must derive from a guest-receivable financial line.")
        expected_effect = (
            FolioPosting.Effect.DEBIT
            if line.direction == FinancialLine.Direction.DEBIT
            else FolioPosting.Effect.CREDIT
        )
        if posting.effect != expected_effect:
            raise LedgerIntegrityError("A folio posting effect must match its source financial-line direction.")


def _existing_idempotent_transaction(*, source_key=None, idempotency_key=None):
    if source_key:
        current = FinancialTransaction.objects.select_for_update().filter(source_key=source_key).first()
        if current:
            return current
    if idempotency_key:
        current = FinancialTransaction.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        if current:
            return current
    return None


@transaction.atomic
def post_existing_transaction(*, financial_transaction, actor=None) -> FinancialTransaction:
    """Validate and make a draft transaction immutable exactly once."""
    financial_transaction = FinancialTransaction.objects.select_for_update().get(pk=financial_transaction.pk)
    if financial_transaction.status == FinancialTransaction.Status.POSTED:
        return financial_transaction
    if financial_transaction.status != FinancialTransaction.Status.DRAFT:
        raise LedgerIntegrityError(
            f"Only DRAFT financial transactions can post; got {financial_transaction.status}."
        )

    lines = list(FinancialLine.objects.select_for_update().filter(transaction=financial_transaction).order_by("pk"))
    _validate_balanced(lines)

    _validate_folio_postings(financial_transaction=financial_transaction)

    now = timezone.now()
    financial_transaction.status = FinancialTransaction.Status.POSTED
    financial_transaction.posted_at = now
    if actor and not financial_transaction.initiated_by_id:
        financial_transaction.initiated_by = actor
        financial_transaction.save(update_fields=["status", "posted_at", "initiated_by", "updated_at"])
    else:
        financial_transaction.save(update_fields=["status", "posted_at", "updated_at"])
    FinancialEvent.objects.create(
        transaction=financial_transaction,
        type=FinancialEvent.Type.POSTED,
        actor=actor,
        details={
            "debit_total": str(_sum_lines(lines, FinancialLine.Direction.DEBIT)),
            "credit_total": str(_sum_lines(lines, FinancialLine.Direction.CREDIT)),
            "line_count": len(lines),
        },
    )
    return financial_transaction


@transaction.atomic
def create_posted_transaction(
    *,
    transaction_type: str,
    lines: Iterable[Mapping],
    actor=None,
    source_key: str | None = None,
    idempotency_key: str | None = None,
    source_reference: str = "",
    external_reference: str = "",
    narrative: str = "",
    metadata: Mapping | None = None,
    currency: str = "NGN",
    business_date: date | None = None,
    occurred_at=None,
    approval=None,
    reversal_of=None,
    postings: Iterable[Mapping] | None = None,
) -> tuple[FinancialTransaction, bool]:
    """Atomically create, balance-check, and post a journal entry.

    ``lines`` must contain ``account_code``, ``direction``, and ``amount``;
    optionally ``folio``, ``description``, and ``metadata``. ``postings`` use
    a zero-based ``line_index`` plus folio statement fields. The function
    returns ``(transaction, created)`` so webhook/worker retries stay cheap.
    """
    source_key = (source_key or "").strip() or None
    idempotency_key = (idempotency_key or "").strip() or None
    existing = _existing_idempotent_transaction(
        source_key=source_key, idempotency_key=idempotency_key
    )
    if existing:
        return existing, False

    normalized_lines = list(lines)
    if not normalized_lines:
        raise LedgerIntegrityError("At least one debit and one credit line are required.")

    financial_transaction = FinancialTransaction.objects.create(
        reference=generate_finance_reference("FIN"),
        type=transaction_type,
        status=FinancialTransaction.Status.DRAFT,
        business_date=business_date or hotel_today(),
        occurred_at=occurred_at or timezone.now(),
        currency=(currency or "NGN").upper(),
        source_key=source_key,
        idempotency_key=idempotency_key,
        source_reference=(source_reference or "")[:120],
        external_reference=(external_reference or "")[:160],
        narrative=(narrative or "")[:500],
        metadata=dict(metadata or {}),
        initiated_by=actor,
        approved_by=getattr(approval, "reviewer", None) if approval else None,
        approved_at=getattr(approval, "reviewed_at", None) if approval else None,
        reversal_of=reversal_of,
    )
    FinancialEvent.objects.create(
        transaction=financial_transaction,
        type=FinancialEvent.Type.CREATED,
        actor=actor,
        details={"source_key": source_key or "", "transaction_type": transaction_type},
    )

    created_lines: list[FinancialLine] = []
    for index, raw_line in enumerate(normalized_lines):
        if not isinstance(raw_line, Mapping):
            raise LedgerIntegrityError({"lines": f"Line {index + 1} must be an object."})
        direction = raw_line.get("direction")
        if direction not in FinancialLine.Direction.values:
            raise LedgerIntegrityError({"lines": f"Line {index + 1} has an invalid debit/credit direction."})
        account_code = str(raw_line.get("account_code") or "").strip()
        if not account_code:
            raise LedgerIntegrityError({"lines": f"Line {index + 1} needs an account code."})
        created_lines.append(
            FinancialLine.objects.create(
                transaction=financial_transaction,
                folio=raw_line.get("folio"),
                account_code=account_code[:64],
                direction=direction,
                amount=_money(raw_line.get("amount"), label=f"lines[{index}].amount"),
                description=str(raw_line.get("description") or "")[:500],
                metadata=dict(raw_line.get("metadata") or {}),
            )
        )

    _validate_balanced(created_lines)

    for raw_posting in postings or ():
        if not isinstance(raw_posting, Mapping):
            raise LedgerIntegrityError({"postings": "Each folio posting must be an object."})
        try:
            line_index = int(raw_posting["line_index"])
            line = created_lines[line_index]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise LedgerIntegrityError({"postings": "Each posting needs a valid line_index."}) from exc
        folio = raw_posting.get("folio") or line.folio
        if not folio:
            raise LedgerIntegrityError({"postings": "A folio posting needs a folio."})
        kind = raw_posting.get("kind")
        effect = raw_posting.get("effect")
        if kind not in FolioPosting.Kind.values or effect not in FolioPosting.Effect.values:
            raise LedgerIntegrityError({"postings": "Posting kind and effect must be valid."})
        posting_amount = _money(raw_posting.get("amount", line.amount), label="posting.amount")
        if posting_amount != line.amount:
            raise LedgerIntegrityError({"postings": "A folio posting amount must equal its source financial line."})
        FolioPosting.objects.create(
            folio=folio,
            transaction=financial_transaction,
            line=line,
            kind=kind,
            effect=effect,
            amount=posting_amount,
            currency=(raw_posting.get("currency") or financial_transaction.currency).upper(),
            description=str(raw_posting.get("description") or line.description or "")[:500],
            business_date=raw_posting.get("business_date") or financial_transaction.business_date,
            source_reference=(raw_posting.get("source_reference") or source_reference or "")[:120],
            reversal_of=raw_posting.get("reversal_of"),
        )

    posted = post_existing_transaction(financial_transaction=financial_transaction, actor=actor)
    return posted, True
