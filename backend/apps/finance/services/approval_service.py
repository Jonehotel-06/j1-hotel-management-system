"""Effective-dated maker-checker controls for sensitive money actions."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_today

from ..models import ApprovalRequest, FinancialControlPolicy, FinancialEvent
from .references import generate_finance_reference

_THRESHOLD_FIELD = {
    ApprovalRequest.Type.REFUND: "refund_manager_approval_threshold",
    ApprovalRequest.Type.DISCOUNT: "discount_manager_approval_threshold",
    ApprovalRequest.Type.VOID: "refund_manager_approval_threshold",
    ApprovalRequest.Type.ADJUSTMENT: "refund_manager_approval_threshold",
    ApprovalRequest.Type.CASH_VARIANCE: "cash_variance_manager_approval_threshold",
    ApprovalRequest.Type.EXPENSE: "expense_manager_approval_threshold",
}


def active_control_policy(*, on_date: date | None = None) -> FinancialControlPolicy:
    """Return the most recent active policy effective at the hotel date."""
    on_date = on_date or hotel_today()
    policy = (
        FinancialControlPolicy.objects.filter(is_active=True, effective_from__lte=on_date)
        .order_by("-effective_from", "-pk")
        .first()
    )
    if policy is None:
        raise ValidationError("No active financial control policy is configured.")
    return policy


def approval_threshold(policy: FinancialControlPolicy, request_type: str) -> Decimal:
    try:
        field = _THRESHOLD_FIELD[request_type]
    except KeyError as exc:
        raise ValidationError("This action type does not have a configured manager-approval threshold.") from exc
    return Decimal(getattr(policy, field)).quantize(Decimal("0.01"))


def requires_manager_approval(*, request_type: str, amount, policy=None) -> bool:
    """Return true when a positive sensitive amount meets its policy threshold."""
    policy = policy or active_control_policy()
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount <= Decimal("0.00"):
        raise ValidationError("A sensitive monetary action must have a positive amount.")
    return amount >= approval_threshold(policy, request_type)


def _policy_snapshot(policy: FinancialControlPolicy) -> dict:
    return {
        "policy_id": policy.pk,
        "policy_name": policy.name,
        "effective_from": policy.effective_from.isoformat(),
        "require_separate_approver": policy.require_separate_approver,
        "refund_manager_approval_threshold": str(policy.refund_manager_approval_threshold),
        "discount_manager_approval_threshold": str(policy.discount_manager_approval_threshold),
        "cash_variance_manager_approval_threshold": str(policy.cash_variance_manager_approval_threshold),
        "expense_manager_approval_threshold": str(policy.expense_manager_approval_threshold),
    }


@transaction.atomic
def request_approval(
    *,
    request_type: str,
    amount,
    requester,
    request_key: str,
    source_reference="",
    currency="NGN",
    reason="",
    metadata=None,
    expires_at=None,
    policy: FinancialControlPolicy | None = None,
):
    """Create or return a source-key-idempotent approval request with a policy snapshot."""
    if request_type not in ApprovalRequest.Type.values:
        raise ValidationError("Invalid finance approval type.")
    if not requester or not getattr(requester, "is_authenticated", False):
        raise PermissionDenied("An authenticated staff requester is required.")
    request_key = str(request_key or "").strip()
    if not request_key:
        raise ValidationError("A stable approval request key is required.")
    current = ApprovalRequest.objects.select_for_update().filter(request_key=request_key).first()
    if current:
        return current, False

    policy = policy or active_control_policy()
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if not requires_manager_approval(request_type=request_type, amount=amount, policy=policy):
        raise ValidationError("This action is below the configured manager-approval threshold.")
    approval = ApprovalRequest.objects.create(
        reference=generate_finance_reference("APR"),
        request_key=request_key,
        type=request_type,
        amount=amount,
        currency=(currency or "NGN").upper(),
        policy=policy,
        policy_snapshot=_policy_snapshot(policy),
        requester=requester,
        source_reference=(source_reference or "")[:120],
        reason=(reason or ""),
        metadata=dict(metadata or {}),
        expires_at=expires_at,
    )
    FinancialEvent.objects.create(
        approval_request=approval,
        type=FinancialEvent.Type.CREATED,
        actor=requester,
        details={"request_type": request_type, "amount": str(amount), "request_key": request_key},
    )
    return approval, True


@transaction.atomic
def _review_approval(*, approval, reviewer, approved: bool, review_note="") -> ApprovalRequest:
    """Apply a maker-checker decision and mandatory immutable review evidence.

    Callers handling a cash variance must also transition its CashSession, so
    they use this internal primitive through ``review_cash_variance``.
    """
    approval = ApprovalRequest.objects.select_for_update().select_related("policy", "requester").get(pk=approval.pk)
    if approval.status != ApprovalRequest.Status.PENDING:
        return approval
    approval_capability = "expense.approve" if approval.type == ApprovalRequest.Type.EXPENSE else "payment.refund.approve"
    if not reviewer or not has_capability(reviewer, approval_capability):
        raise PermissionDenied("This user cannot approve this sensitive monetary action.")
    if approval.expires_at and approval.expires_at <= timezone.now():
        approval.status = ApprovalRequest.Status.EXPIRED
        approval.save(update_fields=["status", "updated_at"])
        raise ValidationError("This approval request has expired.")
    policy = approval.policy or active_control_policy()
    require_separate_approver = approval.policy_snapshot.get(
        "require_separate_approver", policy.require_separate_approver
    )
    if require_separate_approver and approval.requester_id == reviewer.pk:
        raise PermissionDenied("A requester cannot approve their own sensitive monetary action.")
    approval.status = ApprovalRequest.Status.APPROVED if approved else ApprovalRequest.Status.REJECTED
    approval.reviewer = reviewer
    approval.reviewed_at = timezone.now()
    approval.review_note = review_note or ""
    approval.save(update_fields=["status", "reviewer", "reviewed_at", "review_note", "updated_at"])
    FinancialEvent.objects.create(
        approval_request=approval,
        type=FinancialEvent.Type.REVIEWED,
        actor=reviewer,
        details={"approved": bool(approved), "review_note": approval.review_note},
    )
    return approval


def review_approval(*, approval, reviewer, approved: bool, review_note="") -> ApprovalRequest:
    """Review non-cash approvals without bypassing cash-session controls."""
    if approval.type == ApprovalRequest.Type.CASH_VARIANCE:
        raise ValidationError("Cash variance approvals must be reviewed through the controlled cash-session workflow.")
    if approval.type == ApprovalRequest.Type.EXPENSE:
        raise ValidationError("Expense approvals must be reviewed through the controlled expense workflow.")
    return _review_approval(
        approval=approval,
        reviewer=reviewer,
        approved=approved,
        review_note=review_note,
    )
