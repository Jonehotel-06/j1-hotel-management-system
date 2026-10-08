"""Controlled expense request, approval, and immutable posting workflow."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_today

from ..models import ApprovalRequest, CashMovement, CashSession, Expense, ExpenseEvent, FinancialLine, FinancialTransaction
from . import accounting
from .approval_service import _review_approval, active_control_policy, request_approval, requires_manager_approval
from .ledger_service import create_posted_transaction
from .references import generate_finance_reference

CENT = Decimal("0.01")


def _money(value, *, field="amount") -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid two-decimal amount."}) from exc
    if amount <= Decimal("0.00"):
        raise ValidationError({field: "Amount must be greater than zero."})
    return amount


def _event(*, expense, event_type, actor=None, details=None):
    return ExpenseEvent.objects.create(expense=expense, type=event_type, actor=actor, details=dict(details or {}))


@transaction.atomic
def create_expense(
    *, actor, category, amount, incurred_on=None, payment_method=Expense.PaymentMethod.BANK_TRANSFER,
    accounting_class=Expense.AccountingClass.OPERATING, currency="NGN", payee="", supplier_reference="",
    description="", receipt_url="", notes="", idempotency_key=None, cash_session=None,
) -> tuple[Expense, bool]:
    """Create or return a source-keyed draft expense without posting money."""
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot create expense records.")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "An expense idempotency key is required."})
    existing = Expense.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        return existing, False
    category = str(category or "").strip()
    if not category:
        raise ValidationError({"category": "An expense category is required."})
    if payment_method not in Expense.PaymentMethod.values:
        raise ValidationError({"payment_method": "Invalid expense payment method."})
    if accounting_class not in Expense.AccountingClass.values:
        raise ValidationError({"accounting_class": "Invalid expense accounting class."})
    if payment_method == Expense.PaymentMethod.CASH:
        if cash_session is None:
            raise ValidationError({"cash_session": "Cash expenses require an open cash session."})
        cash_session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
        if cash_session.status != CashSession.Status.OPEN:
            raise ValidationError({"cash_session": "Cash expenses require an open cash session."})
        if cash_session.cashier_id != actor.pk:
            raise PermissionDenied("Cash expenses must use the requester's assigned cash session.")
    elif cash_session is not None:
        raise ValidationError({"cash_session": "A cash session is only valid for CASH expenses."})
    amount = _money(amount)
    currency = str(currency or "NGN").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError({"currency": "Use a three-letter ISO currency code."})
    if (incurred_on or hotel_today()) > hotel_today():
        raise ValidationError({"incurred_on": "An expense cannot be incurred in the future."})
    for _ in range(8):
        try:
            with transaction.atomic():
                expense = Expense.objects.create(
                    reference=generate_finance_reference("EXP"), idempotency_key=key, category=category[:120],
                    amount=amount, currency=currency, incurred_on=incurred_on or hotel_today(),
                    payment_method=payment_method, accounting_class=accounting_class, payee=(payee or "")[:255],
                    supplier_reference=(supplier_reference or "")[:120], description=description or "", receipt_url=(receipt_url or "")[:200],
                    requested_by=actor, notes=notes or "", cash_session=cash_session,
                )
            break
        except IntegrityError:
            existing = Expense.objects.select_for_update().filter(idempotency_key=key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate an expense reference; please retry.")
    _event(expense=expense, event_type=ExpenseEvent.Type.CREATED, actor=actor,
           details={"amount": str(amount), "payment_method": payment_method, "accounting_class": accounting_class})
    return expense, True


@transaction.atomic
def submit_expense(*, expense: Expense, actor) -> Expense:
    """Freeze a draft and create policy-bound approval evidence when required."""
    expense = Expense.objects.select_for_update().select_related("requested_by", "approval_request").get(pk=expense.pk)
    if expense.requested_by_id != actor.pk:
        raise PermissionDenied("Only the expense requester may submit this draft.")
    if expense.status in {Expense.Status.SUBMITTED, Expense.Status.APPROVED, Expense.Status.POSTED}:
        return expense
    if expense.status != Expense.Status.DRAFT:
        raise ValidationError(f"Expense cannot submit from {expense.status}.")
    policy = active_control_policy()
    requires_review = requires_manager_approval(request_type=ApprovalRequest.Type.EXPENSE, amount=expense.amount, policy=policy)
    if requires_review:
        approval, _ = request_approval(
            request_type=ApprovalRequest.Type.EXPENSE, amount=expense.amount, requester=actor,
            request_key=f"expense-approval:{expense.reference}", source_reference=expense.reference,
            currency=expense.currency, reason=expense.description or expense.category,
            metadata={"expense_id": expense.pk, "expense_reference": expense.reference, "payment_method": expense.payment_method},
            policy=policy,
        )
        expense.approval_request = approval
        expense.status = Expense.Status.SUBMITTED
        expense.save(update_fields=["approval_request", "status", "updated_at"])
        _event(expense=expense, event_type=ExpenseEvent.Type.SUBMITTED, actor=actor,
               details={"approval_reference": approval.reference, "policy_id": policy.pk})
    else:
        expense.status = Expense.Status.APPROVED
        expense.approved_by = actor
        expense.approved_at = timezone.now()
        expense.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        _event(expense=expense, event_type=ExpenseEvent.Type.APPROVED, actor=actor,
               details={"auto_approved": True, "policy_id": policy.pk})
    return expense


@transaction.atomic
def review_expense(*, expense: Expense, reviewer, approved: bool, review_note="") -> Expense:
    """Apply independent expense approval and synchronize its workflow state."""
    if not has_capability(reviewer, "expense.approve"):
        raise PermissionDenied("This user cannot review expense requests.")
    expense = Expense.objects.select_for_update().select_related("approval_request", "requested_by").get(pk=expense.pk)
    if expense.status in {Expense.Status.APPROVED, Expense.Status.REJECTED, Expense.Status.POSTED}:
        return expense
    if expense.status != Expense.Status.SUBMITTED or expense.approval_request_id is None:
        raise ValidationError("This expense has no pending controlled approval request.")
    approval = _review_approval(approval=expense.approval_request, reviewer=reviewer, approved=approved, review_note=review_note)
    if approval.status not in {ApprovalRequest.Status.APPROVED, ApprovalRequest.Status.REJECTED}:
        raise ValidationError("Expense approval did not reach a final decision.")
    expense.approval_request = approval
    expense.status = Expense.Status.APPROVED if approval.status == ApprovalRequest.Status.APPROVED else Expense.Status.REJECTED
    expense.approved_by = approval.reviewer if approval.status == ApprovalRequest.Status.APPROVED else None
    expense.approved_at = approval.reviewed_at if approval.status == ApprovalRequest.Status.APPROVED else None
    expense.save(update_fields=["approval_request", "status", "approved_by", "approved_at", "updated_at"])
    _event(expense=expense,
           event_type=ExpenseEvent.Type.APPROVED if expense.status == Expense.Status.APPROVED else ExpenseEvent.Type.REJECTED,
           actor=reviewer, details={"approval_reference": approval.reference, "review_note": approval.review_note})
    return expense


def _settlement_account(expense: Expense) -> str:
    if expense.payment_method == Expense.PaymentMethod.CASH:
        return accounting.CASH_ON_HAND
    if expense.payment_method == Expense.PaymentMethod.BANK_TRANSFER:
        return accounting.BANK_CLEARING
    if expense.payment_method == Expense.PaymentMethod.CARD:
        return accounting.CARD_CLEARING
    return accounting.ACCOUNTS_PAYABLE


@transaction.atomic
def post_expense(*, expense: Expense, actor) -> tuple[Expense, bool]:
    """Post one approved expense and, for cash, its immutable paid-out evidence."""
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot post expenses.")
    expense = Expense.objects.select_for_update().select_related("approval_request", "cash_session").get(pk=expense.pk)
    if expense.status == Expense.Status.POSTED:
        return expense, False
    if expense.status != Expense.Status.APPROVED:
        raise ValidationError("Only an approved expense can be posted.")
    cash_session = None
    if expense.payment_method == Expense.PaymentMethod.CASH:
        if expense.currency != "NGN":
            raise ValidationError("Cash-session expenses currently require NGN currency.")
        if expense.cash_session_id is None:
            raise ValidationError("Cash expense is missing its cash session.")
        cash_session = CashSession.objects.select_for_update().get(pk=expense.cash_session_id)
        if cash_session.status != CashSession.Status.OPEN:
            raise ValidationError("Cash expense requires an open cash session.")
        if cash_session.cashier_id != actor.pk:
            raise PermissionDenied("Cash expense must be posted by the assigned cashier.")
        if Decimal(cash_session.expected_cash) < Decimal(expense.amount):
            raise ValidationError("Cash expense exceeds the drawer's expected cash.")
    debit_account = accounting.INVENTORY_ASSET if expense.accounting_class == Expense.AccountingClass.INVENTORY else accounting.OPERATING_EXPENSE
    transaction, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.EXPENSE,
        source_key=f"expense:{expense.reference}", idempotency_key=f"expense:{expense.reference}", actor=actor,
        source_reference=expense.reference, external_reference=expense.supplier_reference,
        narrative=f"Expense {expense.category}: {expense.payee or expense.reference}", currency=expense.currency,
        business_date=expense.incurred_on, approval=expense.approval_request,
        metadata={"expense_id": expense.pk, "expense_reference": expense.reference, "payment_method": expense.payment_method,
                  "accounting_class": expense.accounting_class, "cash_session": cash_session.reference if cash_session else ""},
        lines=[
            {"account_code": debit_account, "direction": FinancialLine.Direction.DEBIT, "amount": expense.amount,
             "description": expense.description or expense.category},
            {"account_code": _settlement_account(expense), "direction": FinancialLine.Direction.CREDIT, "amount": expense.amount,
             "description": f"Expense settlement {expense.reference}"},
        ],
    )
    if created and cash_session:
        CashMovement.objects.create(
            cash_session=cash_session, transaction=transaction, type=CashMovement.Type.PAID_OUT,
            amount=expense.amount, currency=expense.currency, actor=actor, source_reference=expense.reference,
            notes=(expense.description or expense.category)[:500], metadata={"expense_id": expense.pk, "expense_reference": expense.reference},
        )
        cash_session.expected_cash = (Decimal(cash_session.expected_cash) - Decimal(expense.amount)).quantize(CENT)
        cash_session.save(update_fields=["expected_cash", "updated_at"])
    if expense.approval_request and expense.approval_request.financial_transaction_id != transaction.pk:
        expense.approval_request.financial_transaction = transaction
        expense.approval_request.save(update_fields=["financial_transaction", "updated_at"])
    expense.financial_transaction = transaction
    expense.posted_by = actor
    expense.posted_at = timezone.now()
    expense.status = Expense.Status.POSTED
    expense.save(update_fields=["financial_transaction", "posted_by", "posted_at", "status", "updated_at"])
    _event(expense=expense, event_type=ExpenseEvent.Type.POSTED, actor=actor,
           details={"financial_transaction": transaction.reference, "cash_paid_out": bool(cash_session)})
    return expense, created
