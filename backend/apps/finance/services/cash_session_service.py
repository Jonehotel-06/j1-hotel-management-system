"""Controlled cashier sessions and safe cash-drawer projections."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.utils import hotel_today

from ..models import CashMovement, CashSession, FinancialEvent, FinancialLine, FinancialTransaction
from . import accounting
from .approval_service import active_control_policy, request_approval
from .ledger_service import create_posted_transaction
from .references import generate_finance_reference

CENT = Decimal("0.01")


def _amount(value, *, allow_zero=True):
    amount = Decimal(str(value or 0)).quantize(CENT)
    if amount < Decimal("0.00") or (not allow_zero and amount <= Decimal("0.00")):
        raise ValidationError("Cash amount must be non-negative." if allow_zero else "Cash amount must be positive.")
    return amount


def _post_cash_variance_adjustment(*, session, actor, approval=None):
    """Bring Cash on Hand to the counted drawer total exactly once.

    A shortage debits the cash-over/short expense; an overage credits it. The
    actual physical-count record stays immutable in ``CashMovement`` while the
    balanced journal provides the authoritative accounting result.
    """
    variance = Decimal(session.variance or 0).quantize(CENT)
    if variance == Decimal("0.00"):
        return None
    amount = abs(variance)
    if variance < Decimal("0.00"):
        lines = [
            {
                "account_code": accounting.CASH_OVER_SHORT,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "description": f"Cash shortage for {session.reference}",
            },
            {
                "account_code": accounting.CASH_ON_HAND,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": f"Cash shortage for {session.reference}",
            },
        ]
    else:
        lines = [
            {
                "account_code": accounting.CASH_ON_HAND,
                "direction": FinancialLine.Direction.DEBIT,
                "amount": amount,
                "description": f"Cash overage for {session.reference}",
            },
            {
                "account_code": accounting.CASH_OVER_SHORT,
                "direction": FinancialLine.Direction.CREDIT,
                "amount": amount,
                "description": f"Cash overage for {session.reference}",
            },
        ]
    source_key = f"cash-close-variance:{session.reference}"
    financial_transaction, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.CASH_CLOSE,
        source_key=source_key,
        idempotency_key=source_key,
        actor=actor,
        source_reference=session.reference,
        narrative=f"Cash-close variance adjustment for {session.reference}",
        currency="NGN",
        business_date=session.business_date,
        metadata={
            "cash_session_id": session.pk,
            "cash_session_reference": session.reference,
            "expected_cash": str(session.expected_cash),
            "counted_cash": str(session.counted_cash),
            "variance": str(variance),
            "approval_reference": approval.reference if approval else "",
        },
        lines=lines,
    )
    if approval and approval.financial_transaction_id != financial_transaction.pk:
        approval.financial_transaction = financial_transaction
        approval.save(update_fields=["financial_transaction", "updated_at"])
    if created:
        FinancialEvent.objects.create(
            transaction=financial_transaction,
            cash_session=session,
            approval_request=approval,
            type=FinancialEvent.Type.POSTED,
            actor=actor,
            details={"reason": "cash_variance", "variance": str(variance)},
        )
    return financial_transaction


@transaction.atomic
def open_cash_session(*, cashier, actor=None, opening_float="0.00", location="", terminal="", notes="") -> CashSession:
    """Open one cashier drawer session, with opening float as a finance event."""
    cashier = User.objects.select_for_update().get(pk=cashier.pk)
    if actor and cashier.pk != actor.pk:
        raise ValidationError("A cashier session can only be opened by its assigned cashier.")
    existing = CashSession.objects.select_for_update().filter(
        cashier=cashier, status__in=[CashSession.Status.OPEN, CashSession.Status.PENDING_REVIEW]
    ).first()
    if existing:
        return existing
    opening_float = _amount(opening_float)
    session = CashSession.objects.create(
        reference=generate_finance_reference("CSH"),
        cashier=cashier,
        business_date=hotel_today(),
        location=(location or "")[:120],
        terminal=(terminal or "")[:120],
        opening_float=opening_float,
        expected_cash=opening_float,
        opened_by=actor or cashier,
        notes=notes or "",
    )
    if opening_float > Decimal("0.00"):
        source_key = f"cash-open:{session.reference}"
        financial_transaction, _ = create_posted_transaction(
            transaction_type=FinancialTransaction.Type.CASH_OPEN,
            source_key=source_key,
            idempotency_key=source_key,
            actor=actor or cashier,
            source_reference=session.reference,
            narrative=f"Cash drawer opening float {session.reference}",
            currency="NGN",
            lines=[
                {
                    "account_code": accounting.CASH_ON_HAND,
                    "direction": FinancialLine.Direction.DEBIT,
                    "amount": opening_float,
                    "description": "Cash drawer opening float",
                },
                {
                    "account_code": accounting.CASH_FLOAT,
                    "direction": FinancialLine.Direction.CREDIT,
                    "amount": opening_float,
                    "description": "Cash drawer opening float source",
                },
            ],
        )
        CashMovement.objects.create(
            cash_session=session,
            transaction=financial_transaction,
            type=CashMovement.Type.OPENING_FLOAT,
            amount=opening_float,
            actor=actor or cashier,
            source_reference=session.reference,
            notes="Opening cash float",
        )
    FinancialEvent.objects.create(
        cash_session=session,
        type=FinancialEvent.Type.CASH_SESSION_OPENED,
        actor=actor or cashier,
        details={"opening_float": str(opening_float), "location": session.location, "terminal": session.terminal},
    )
    return session


@transaction.atomic
def close_cash_session(*, cash_session, cashier, counted_cash, notes="") -> CashSession:
    """Record physical count; material variance remains pending independent review."""
    session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
    if session.cashier_id != cashier.pk:
        raise ValidationError("Only the assigned cashier can close this cash session.")
    if session.status == CashSession.Status.CLOSED:
        return session
    if session.status != CashSession.Status.OPEN:
        raise ValidationError(f"Cash session cannot close from {session.status}.")
    counted_cash = _amount(counted_cash)
    variance = (counted_cash - session.expected_cash).quantize(CENT)
    policy = active_control_policy()
    threshold = Decimal(policy.cash_variance_manager_approval_threshold).quantize(CENT)
    needs_review = abs(variance) >= threshold if threshold > Decimal("0.00") else variance != Decimal("0.00")
    session.counted_cash = counted_cash
    session.variance = variance
    session.closed_at = timezone.now()
    session.closed_by = cashier
    session.notes = notes or session.notes
    session.status = CashSession.Status.PENDING_REVIEW if needs_review else CashSession.Status.CLOSED
    session.save(update_fields=[
        "counted_cash", "variance", "closed_at", "closed_by", "notes", "status", "updated_at",
    ])
    if counted_cash > Decimal("0.00"):
        CashMovement.objects.create(
            cash_session=session,
            type=CashMovement.Type.CLOSING_COUNT,
            amount=counted_cash,
            actor=cashier,
            source_reference=session.reference,
            notes="Closing physical count",
            metadata={"expected_cash": str(session.expected_cash), "variance": str(variance)},
        )
    # Low variances are permitted by policy and are journaled immediately;
    # material variances wait for a distinct manager's approval below.
    if not needs_review:
        _post_cash_variance_adjustment(session=session, actor=cashier)

    approval_reference = None
    if needs_review:
        approval, _ = request_approval(
            request_type="CASH_VARIANCE",
            amount=abs(variance),
            requester=cashier,
            request_key=f"cash-variance:{session.reference}",
            source_reference=session.reference,
            reason=f"Cash variance {variance} on session {session.reference}",
            metadata={"cash_session_id": session.pk, "variance": str(variance)},
        )
        approval_reference = approval.reference
    FinancialEvent.objects.create(
        cash_session=session,
        type=FinancialEvent.Type.CASH_SESSION_CLOSED,
        actor=cashier,
        details={
            "expected_cash": str(session.expected_cash),
            "counted_cash": str(counted_cash),
            "variance": str(variance),
            "approval_reference": approval_reference,
        },
    )
    return session

@transaction.atomic
def review_cash_variance(*, cash_session, reviewer, approved: bool, review_note="") -> CashSession:
    """Complete the manager-review branch of a material cash-close variance.

    A physical count is immutable evidence, regardless of whether management
    accepts its variance. Both decisions therefore finish the drawer session;
    a rejected decision remains discoverable from the linked approval and the
    immutable review event rather than leaving a cashier unable to open another
    controlled drawer indefinitely.
    """
    from ..models import ApprovalRequest
    from .approval_service import _review_approval

    session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
    if session.status == CashSession.Status.CLOSED:
        return session
    if session.status != CashSession.Status.PENDING_REVIEW:
        raise ValidationError(f"Cash session cannot be variance-reviewed from {session.status}.")
    approval = (
        ApprovalRequest.objects.select_for_update()
        .filter(
            type=ApprovalRequest.Type.CASH_VARIANCE,
            request_key=f"cash-variance:{session.reference}",
            source_reference=session.reference,
        )
        .first()
    )
    if approval is None:
        raise ValidationError("Cash session has no pending variance approval request.")
    approval = _review_approval(
        approval=approval,
        reviewer=reviewer,
        approved=approved,
        review_note=review_note,
    )
    # `review_approval` returns a prior decision idempotently. Do not overwrite
    # its author/outcome on duplicate API retries.
    if approval.status not in {ApprovalRequest.Status.APPROVED, ApprovalRequest.Status.REJECTED}:
        raise ValidationError("Cash variance approval did not reach a final decision.")
    if approval.status == ApprovalRequest.Status.APPROVED:
        _post_cash_variance_adjustment(session=session, actor=approval.reviewer, approval=approval)
    if session.status != CashSession.Status.CLOSED:
        session.status = CashSession.Status.CLOSED
        if approval.status == ApprovalRequest.Status.APPROVED:
            session.review_approved_by = approval.reviewer
            session.review_approved_at = approval.reviewed_at
        session.save(update_fields=[
            "status", "review_approved_by", "review_approved_at", "updated_at",
        ])
        FinancialEvent.objects.create(
            cash_session=session,
            approval_request=approval,
            type=FinancialEvent.Type.CASH_VARIANCE_REVIEWED,
            actor=approval.reviewer,
            details={
                "approved": approval.status == ApprovalRequest.Status.APPROVED,
                "review_note": approval.review_note,
                "variance": str(session.variance),
            },
        )
    return session
