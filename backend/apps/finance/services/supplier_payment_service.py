"""Supplier payment requests: propose -> submit -> approve -> execute.

Why this exists
===============
The Accounting screen's "Record supplier payment" button called
``POST /api/admin/finance/expenses/<ref>/settle-payment/``. That endpoint *is*
the money leaving the hotel: it posts Dr accounts payable / Cr the tender used.
It required only ``expense.manage``, which the capability matrix grants to
cashiers and waiters, and it accepted no amount at all — it always settled the
whole payable. So before this module:

* the person who could record a supplier bill could also discharge it with no
  second pair of eyes, on any amount, with no record that a payment had been
  proposed, submitted, authorised or refused;
* "payment approved" and "payment executed" were the same event, so a payment
  could not be authorised ahead of time or reversed before it was made;
* nothing tied the payment back to the payable it was meant to settle beyond the
  expense row itself.

This module keeps the existing settlement posting untouched (so the ledger,
reporting and every existing balance stay exactly as they were) and puts a
controlled record in front of it:

``propose_supplier_payment``   records the intent; validates the payable,
                               amount, currency and method. Any ``expense.manage``
                               holder may propose.
``submit_supplier_payment``    hands it to a reviewer; creates the central
                               ``ApprovalRequest`` (policy threshold applies,
                               exactly like an expense submission).
``review_supplier_payment``    an ``expense.approve`` holder decides. The
                               central maker-checker rule refuses self-approval
                               for every role except an administrator override,
                               which is recorded as evidence.
``execute_supplier_payment``   posts the settlement through the existing
                               ``settle_expense_payment`` service in one database
                               transaction and stamps ``EXECUTED`` only if the
                               posting succeeded.
``cancel_supplier_payment``    withdraws a request that has not been executed.

The direct settlement endpoint keeps working for an approver (an
``expense.approve`` holder already has authority to release a payable), but a
user who lacks that capability must now come through an approved request. That
closes the cashier-pays-the-supplier hole without changing a single posted
figure or breaking the existing settlement contract.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.approvals import override_event_details, override_note, require_independent_approver
from apps.core.utils import hotel_today

from ..models import ApprovalRequest, Expense, SupplierPaymentRequest
from .approval_service import _review_approval, active_control_policy, request_approval, requires_manager_approval
from .expense_service import settle_expense_payment
from .references import generate_finance_reference

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

ACTIVE_STATUSES = (
    SupplierPaymentRequest.Status.PROPOSED,
    SupplierPaymentRequest.Status.SUBMITTED,
    SupplierPaymentRequest.Status.APPROVED,
)
TERMINAL_STATUSES = (
    SupplierPaymentRequest.Status.REJECTED,
    SupplierPaymentRequest.Status.EXECUTED,
    SupplierPaymentRequest.Status.CANCELLED,
)


def payable_balance(expense: Expense) -> Decimal:
    """The amount still owed on one payable document.

    The existing design allows exactly one settlement transaction per expense
    (``Expense.settlement_transaction`` is a OneToOne), so a payable is either
    unpaid in full or settled in full. Nothing here invents a part-paid state:
    an amount that is not the full outstanding balance is refused with an
    explanation instead of being silently rounded up to the payable.
    """
    if getattr(expense, "settlement_transaction_id", None) or expense.paid_at:
        return ZERO
    return Decimal(expense.amount).quantize(CENT)


def _dec(value, *, field="amount") -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid two-decimal amount."}) from exc
    if amount <= ZERO:
        raise ValidationError({field: "Enter a payment amount greater than zero."})
    return amount


def _settleable_expense(expense: Expense) -> Expense:
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.status != Expense.Status.POSTED:
        raise ValidationError(
            "Only a posted payable can be paid. Post the approved expense first, "
            "so the liability exists before the money moves."
        )
    if expense.payment_method != Expense.PaymentMethod.PAYABLE:
        raise ValidationError(
            "This expense was settled at posting time; only an on-account (accounts payable) "
            "expense needs a supplier payment."
        )
    if expense.settlement_transaction_id or expense.paid_at:
        raise ValidationError("This payable has already been paid. A second payment is not allowed.")
    return expense


@transaction.atomic
def propose_supplier_payment(
    *, expense: Expense, actor, method, amount=None, payment_reference="", paid_on=None,
    notes="", idempotency_key=None,
) -> tuple[SupplierPaymentRequest, bool]:
    """Record a proposed supplier payment against one posted payable."""
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot propose supplier payments.")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "A payment-request idempotency key is required."})
    existing = SupplierPaymentRequest.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        return existing, False

    expense = _settleable_expense(expense)
    outstanding = payable_balance(expense)
    method = str(method or "").upper()
    if method not in SupplierPaymentRequest.Method.values:
        raise ValidationError({"method": "Choose how the supplier will be paid: BANK_TRANSFER, CARD or CASH."})
    reference = str(payment_reference or "").strip()[:160]
    if method in {SupplierPaymentRequest.Method.BANK_TRANSFER, SupplierPaymentRequest.Method.CARD} and not reference:
        raise ValidationError({"payment_reference": "Enter the bank/card reference this payment will carry."})
    if amount is None:
        amount = outstanding
    amount = _dec(amount)
    if amount > outstanding:
        raise ValidationError({
            "amount": (
                f"The payment cannot exceed the outstanding payable balance of {outstanding} "
                f"{expense.currency}."
            )
        })
    if amount != outstanding:
        raise ValidationError({
            "amount": (
                f"This payable is settled in one payment of {outstanding} {expense.currency}. Partial "
                "settlement of an on-account expense is not supported by the current ledger design "
                "(one settlement posting per payable), so pass the full balance."
            )
        })
    paid_on = paid_on or hotel_today()
    if paid_on > hotel_today():
        raise ValidationError({"paid_on": "A supplier payment cannot be dated in the future."})

    open_request = SupplierPaymentRequest.objects.select_for_update().filter(
        expense=expense, status__in=ACTIVE_STATUSES,
    ).first()
    if open_request is not None:
        raise ValidationError({
            "expense": (
                f"Payable {expense.reference} already has an open payment request "
                f"({open_request.reference}, {open_request.status}). Cancel it or use it — a second "
                "request for the same payable would be a duplicate payment."
            )
        })

    request = SupplierPaymentRequest.objects.create(
        reference=generate_finance_reference("SPR"),
        idempotency_key=key,
        expense=expense,
        amount=amount,
        currency=expense.currency,
        method=method,
        payment_reference=reference,
        paid_on=paid_on,
        requested_by=actor,
        notes=str(notes or "")[:2000],
    )
    from ..models import ExpenseEvent

    ExpenseEvent.objects.create(
        expense=expense, type=ExpenseEvent.Type.CREATED, actor=actor,
        details={
            "supplier_payment_request": request.reference,
            "amount": str(amount),
            "method": method,
            "status": request.status,
        },
    )
    return request, True


@transaction.atomic
def submit_supplier_payment(*, request: SupplierPaymentRequest, actor) -> SupplierPaymentRequest:
    """Freeze a proposal and create the policy-bound approval evidence."""
    request = SupplierPaymentRequest.objects.select_for_update().select_related(
        "expense", "approval_request",
    ).get(pk=request.pk)
    if request.requested_by_id != actor.pk and not has_capability(actor, "expense.approve"):
        raise PermissionDenied("Only the requester may submit this payment request.")
    if request.status == SupplierPaymentRequest.Status.SUBMITTED:
        return request
    if request.status != SupplierPaymentRequest.Status.PROPOSED:
        raise ValidationError(f"A payment request cannot be submitted from {request.status}.")
    expense = _settleable_expense(request.expense)
    if payable_balance(expense) < request.amount:
        raise ValidationError("The payable balance changed. Cancel this request and propose it again.")

    policy = active_control_policy()
    requires_review = requires_manager_approval(
        request_type=ApprovalRequest.Type.SUPPLIER_PAYMENT, amount=request.amount, policy=policy,
    )
    if requires_review:
        approval, _ = request_approval(
            request_type=ApprovalRequest.Type.SUPPLIER_PAYMENT,
            amount=request.amount,
            requester=actor,
            request_key=f"supplier-payment:{request.reference}",
            source_reference=expense.reference,
            currency=request.currency,
            reason=f"Supplier payment for {expense.reference} ({expense.payee or expense.category})",
            metadata={
                "supplier_payment_request": request.reference,
                "expense_reference": expense.reference,
                "method": request.method,
                "payment_reference": request.payment_reference,
            },
            policy=policy,
        )
        request.approval_request = approval
        request.status = SupplierPaymentRequest.Status.SUBMITTED
        request.save(update_fields=["approval_request", "status", "updated_at"])
    else:
        # Below the configured threshold the hotel's policy needs no second
        # signature, exactly as it does for an expense submission. The request
        # still moves through APPROVED so the audit trail has one shape.
        request.status = SupplierPaymentRequest.Status.APPROVED
        request.reviewed_by = actor
        request.reviewed_at = timezone.now()
        request.review_note = "Auto-approved under the active finance control policy (below threshold)."
        request.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    return request


@transaction.atomic
def review_supplier_payment(
    *, request: SupplierPaymentRequest, reviewer, approved: bool, review_note="",
) -> SupplierPaymentRequest:
    """Independent approval or rejection of a submitted payment request."""
    if not has_capability(reviewer, "expense.approve"):
        raise PermissionDenied("This user cannot approve supplier payments.")
    request = SupplierPaymentRequest.objects.select_for_update().select_related(
        "expense", "approval_request", "requested_by",
    ).get(pk=request.pk)
    if request.status in TERMINAL_STATUSES:
        return request
    if request.status != SupplierPaymentRequest.Status.SUBMITTED or request.approval_request_id is None:
        raise ValidationError("This payment request has no pending controlled approval.")
    approval = _review_approval(
        approval=request.approval_request, reviewer=reviewer, approved=approved, review_note=review_note,
    )
    if approval.status not in {ApprovalRequest.Status.APPROVED, ApprovalRequest.Status.REJECTED}:
        return request
    request.status = (
        SupplierPaymentRequest.Status.APPROVED if approved else SupplierPaymentRequest.Status.REJECTED
    )
    request.reviewed_by = reviewer
    request.reviewed_at = timezone.now()
    request.review_note = approval.review_note
    request.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    return request


@transaction.atomic
def execute_supplier_payment(
    *, request: SupplierPaymentRequest, actor, cash_session=None, receipt_url="", receipt_file=None,
):
    """Post the approved payment once and stamp the request EXECUTED.

    The settlement and the status stamp share one transaction: if the posting
    fails, nothing is committed and no payment is reported as made.
    """
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot record supplier payments.")
    request = SupplierPaymentRequest.objects.select_for_update().select_related("expense").get(pk=request.pk)
    if request.status == SupplierPaymentRequest.Status.EXECUTED:
        return request, None
    if request.status != SupplierPaymentRequest.Status.APPROVED:
        raise ValidationError(
            f"Only an approved payment request can be executed; this one is {request.status}."
        )
    expense = _settleable_expense(request.expense)
    if payable_balance(expense) < request.amount:
        raise ValidationError(
            "The payable no longer carries this amount (it was settled elsewhere). "
            "Cancel this request; nothing was paid."
        )
    settled, created = settle_expense_payment(
        expense=expense,
        actor=actor,
        method=request.method,
        amount=request.amount,
        payment_reference=request.payment_reference,
        cash_session=cash_session,
        receipt_url=receipt_url,
        receipt_file=receipt_file,
        paid_on=request.paid_on,
        payment_request=request,
    )
    request.status = SupplierPaymentRequest.Status.EXECUTED
    request.executed_by = actor
    request.executed_at = timezone.now()
    request.settlement_transaction = settled.settlement_transaction
    request.save(update_fields=[
        "status", "executed_by", "executed_at", "settlement_transaction", "updated_at",
    ])
    return request, settled


@transaction.atomic
def cancel_supplier_payment(
    *, request: SupplierPaymentRequest, actor, reason,
) -> SupplierPaymentRequest:
    """Withdraw a payment request that has not been executed."""
    request = SupplierPaymentRequest.objects.select_for_update().select_related("expense").get(pk=request.pk)
    if request.requested_by_id != actor.pk and not has_capability(actor, "expense.approve"):
        raise PermissionDenied("Only the requester or an approver may cancel this payment request.")
    if request.status == SupplierPaymentRequest.Status.CANCELLED:
        return request
    if request.status == SupplierPaymentRequest.Status.EXECUTED:
        raise ValidationError(
            "This payment has already been executed. A paid supplier cannot be un-paid by "
            "cancelling its request; a reversal is a separate controlled posting."
        )
    reason = " ".join(str(reason or "").split())
    if len(reason) < 5:
        raise ValidationError({"reason": "Record why this payment request is being cancelled."})
    if request.approval_request_id and request.approval_request.status == ApprovalRequest.Status.PENDING:
        approval = ApprovalRequest.objects.select_for_update().get(pk=request.approval_request_id)
        approval.status = ApprovalRequest.Status.CANCELLED
        approval.review_note = (approval.review_note + f"\nCancelled: {reason}").strip()
        approval.save(update_fields=["status", "review_note", "updated_at"])
    request.status = SupplierPaymentRequest.Status.CANCELLED
    request.cancelled_by = actor
    request.cancelled_at = timezone.now()
    request.cancel_reason = reason[:2000]
    request.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return request


def supplier_payment_requests_for(expense: Expense) -> list[SupplierPaymentRequest]:
    return list(
        SupplierPaymentRequest.objects.filter(expense=expense)
        .select_related("requested_by", "reviewed_by", "executed_by")
        .order_by("-created_at", "-pk")
    )
