"""Controlled expense request, approval, and immutable posting workflow."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_today

from ..models import ApprovalRequest, CashMovement, CashSession, Expense, ExpenseEvent, FinancialLine, FinancialTransaction, SupplierSettlement
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
    if not amount.is_finite() or amount <= Decimal("0.00"):
        raise ValidationError({field: "Amount must be finite and greater than zero."})
    return amount


def _event(*, expense, event_type, actor=None, details=None):
    return ExpenseEvent.objects.create(expense=expense, type=event_type, actor=actor, details=dict(details or {}))


@transaction.atomic
def create_expense(
    *, actor, category, amount, incurred_on=None, payment_method=Expense.PaymentMethod.BANK_TRANSFER,
    accounting_class=Expense.AccountingClass.OPERATING, currency="NGN", payee="", supplier_reference="",
    description="", receipt_url="", receipt_file=None, notes="", idempotency_key=None, cash_session=None,
    department="", purchase_order=None, metadata=None,
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
    if receipt_file is not None:
        # Validate BEFORE the row exists: a rejected attachment must never leave
        # an expense that claims a document it does not have.
        from apps.core.validators import validate_document_upload

        validate_document_upload(receipt_file)
    for _ in range(8):
        try:
            with transaction.atomic():
                expense = Expense.objects.create(
                    reference=generate_finance_reference("EXP"), idempotency_key=key, category=category[:120],
                    amount=amount, currency=currency, incurred_on=incurred_on or hotel_today(),
                    payment_method=payment_method, accounting_class=accounting_class, payee=(payee or "")[:255],
                    supplier_reference=(supplier_reference or "")[:120], department=(department or "").strip()[:80], description=description or "", receipt_url=(receipt_url or "")[:200],
                    requested_by=actor, notes=notes or "", cash_session=cash_session,
                    purchase_order=purchase_order, metadata=dict(metadata or {}),
                )
            break
        except IntegrityError:
            existing = Expense.objects.select_for_update().filter(idempotency_key=key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate an expense reference; please retry.")
    if receipt_file is not None:
        from apps.core.storage import save_upload

        def _write():
            expense.receipt_file.save(receipt_file.name, receipt_file, save=False)

        save_upload(_write, context=f"expense {expense.reference} receipt")
        expense.save(update_fields=["receipt_file", "updated_at"])
    _event(expense=expense, event_type=ExpenseEvent.Type.CREATED, actor=actor,
           details={
               "amount": str(amount), "payment_method": payment_method,
               "accounting_class": accounting_class, "has_receipt_file": bool(expense.receipt_file),
               "purchase_order": expense.purchase_order.reference if expense.purchase_order_id else "",
           })
    return expense, True


@transaction.atomic
def submit_expense(*, expense: Expense, actor) -> Expense:
    """Freeze a draft and create policy-bound approval evidence when required.

    A Store Keeper / Procurement handover is DRAFT-only authority. Only an
    Accountant with ``expense.manage`` may submit it, and it ALWAYS goes through
    independent approval regardless of the monetary threshold.
    """
    expense = Expense.objects.select_for_update().select_related("requested_by", "approval_request").get(pk=expense.pk)
    is_handover = expense.workflow_origin != Expense.WorkflowOrigin.DIRECT
    if expense.requested_by_id != actor.pk and not (is_handover and has_capability(actor, "expense.manage")):
        raise PermissionDenied("Only the expense requester may submit this draft.")
    if expense.status in {Expense.Status.SUBMITTED, Expense.Status.APPROVED, Expense.Status.POSTED}:
        return expense
    if expense.status != Expense.Status.DRAFT:
        raise ValidationError(f"Expense cannot submit from {expense.status}.")
    if is_handover and not has_capability(actor, "expense.manage"):
        raise PermissionDenied("A Store Keeper handover must be prepared and submitted by an Accountant.")
    policy = active_control_policy()
    requires_review = is_handover or requires_manager_approval(request_type=ApprovalRequest.Type.EXPENSE, amount=expense.amount, policy=policy)
    if requires_review:
        approval, _ = request_approval(
            request_type=ApprovalRequest.Type.EXPENSE, amount=expense.amount, requester=actor,
            request_key=f"expense-approval:{expense.reference}", source_reference=expense.reference,
            currency=expense.currency, reason=expense.description or expense.category,
            metadata={"expense_id": expense.pk, "expense_reference": expense.reference, "payment_method": expense.payment_method},
            policy=policy, force_review=is_handover,
        )
        expense.approval_request = approval
        expense.status = Expense.Status.SUBMITTED
        update_fields = ["approval_request", "status", "updated_at"]
        if is_handover and not expense.prepared_by_id:
            expense.prepared_by = actor
            expense.prepared_at = timezone.now()
            update_fields.extend(["prepared_by", "prepared_at"])
        expense.save(update_fields=update_fields)
        _event(expense=expense, event_type=ExpenseEvent.Type.SUBMITTED, actor=actor,
               details={"approval_reference": approval.reference, "policy_id": policy.pk,
                        "workflow_origin": expense.workflow_origin, "forced_review": is_handover})
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


@transaction.atomic
def attach_expense_payment_evidence(
    *, expense: Expense, actor, payment_reference="", receipt_url="", receipt_file=None, paid_on=None,
) -> tuple[Expense, bool]:
    """Store the proof of a supplier payment that has ALREADY left the money.

    Posting a CASH / BANK_TRANSFER / CARD expense credits the tender account, so
    the money is treated as having gone out at posting and there is no later
    settlement to post. Until now the transfer slip for such an expense had
    nowhere to live, and only an on-account settlement could carry receipt
    evidence. This records that evidence on the expense itself:

      * the transfer receipt file or link and the bank/card reference,
      * the date the desk says the money was paid,
      * a PAID event, so the attachment is on the append-only expense history.

    It posts **no ledger line**: no financial figure moves, and
    ``outstanding_supplier_payables`` / ``total_cash_disbursements`` (both read
    from posted ledger lines) are unchanged by this call. Existing evidence is
    never overwritten — a second attachment is refused instead of replacing the
    first, so the record stays honest.
    """
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot record supplier payment evidence.")
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.status != Expense.Status.POSTED:
        raise ValidationError("Only a posted expense can carry supplier payment evidence.")
    if expense.payment_method == Expense.PaymentMethod.PAYABLE and not (
        expense.paid_at or expense.settlement_transaction_id
    ):
        raise ValidationError(
            "This expense is on account and has not been paid yet. Record the payment first "
            "(Record supplier payment), then attach the receipt if you still need to."
        )

    payment_reference = str(payment_reference or "").strip()[:160]
    receipt_url = str(receipt_url or "").strip()[:200]
    if receipt_file is None and not payment_reference and not receipt_url:
        raise ValidationError({
            "receipt": "Attach the receipt file, paste the receipt link, or enter the transfer reference."
        })
    if receipt_file is not None and expense.payment_receipt_file:
        raise ValidationError({"receipt": "A payment receipt file is already recorded for this expense."})
    if receipt_url and expense.payment_receipt_url:
        raise ValidationError({"receipt_url": "A payment receipt link is already recorded for this expense."})
    if payment_reference and expense.payment_reference:
        raise ValidationError({"payment_reference": "A payment reference is already recorded for this expense."})

    paid_on = paid_on or hotel_today()
    if paid_on > hotel_today():
        raise ValidationError({"paid_on": "A supplier payment cannot be dated in the future."})

    # Only the fields the caller actually supplied are written: attaching a
    # receipt file to an expense that was settled with a bank reference must
    # never blank that reference (or the other evidence) out.
    updates = {}
    if receipt_file is not None:
        updates["payment_receipt_file"] = receipt_file
    if payment_reference:
        updates["payment_reference"] = payment_reference
    if receipt_url:
        updates["payment_receipt_url"] = receipt_url
    stamps_paid = not expense.paid_at
    if stamps_paid:
        # Stamped once, and only when posting did not already record the money
        # as out: the expense list and the payment key stay truthful.
        updates["paid_at"] = timezone.now()
        updates["paid_by"] = actor
    Expense.objects.filter(pk=expense.pk).update(**updates)
    expense.refresh_from_db()
    _event(
        expense=expense, event_type=ExpenseEvent.Type.PAID, actor=actor,
        details={
            "evidence_only": True,
            "payment_method": expense.payment_method,
            "payment_reference": payment_reference,
            "receipt_url": receipt_url,
            "receipt_file": bool(receipt_file),
            "paid_on": paid_on.isoformat(),
            "stamped_paid": stamps_paid,
            "ledger_effect": "none",
        },
    )
    return expense, True


@transaction.atomic
def settle_expense_payment(
    *, expense, actor, method, payment_reference="", cash_session=None,
    receipt_url="", receipt_file=None, paid_on=None, amount=None,
    payment_request=None, idempotency_key=None,
) -> tuple[Expense, bool]:
    """Settle one real instalment of a posted payable without duplicating it.

    Legacy full settlements remain readable. New instalments each have an
    immutable source-keyed settlement and journal. Only the final instalment
    sets the legacy paid/settlement fields. Both the expense and any approved
    request are locked while capacity and authority are checked.
    """
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot record supplier payments.")
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    approved_request = None
    if payment_request is not None:
        from ..models import SupplierPaymentRequest
        approved_request = SupplierPaymentRequest.objects.select_for_update().get(pk=payment_request.pk)
        if approved_request.expense_id != expense.pk or approved_request.status != SupplierPaymentRequest.Status.APPROVED:
            raise ValidationError("The approved payment request must belong to this payable.")
    if not has_capability(actor, "expense.approve") and approved_request is None:
        raise PermissionDenied("Recording a supplier payment needs an approved payment request.")
    if expense.status != Expense.Status.POSTED or expense.payment_method != Expense.PaymentMethod.PAYABLE:
        raise ValidationError("Only a posted on-account expense can be settled.")

    method = str(method or "").strip().upper()
    if method not in {Expense.PaymentMethod.CASH, Expense.PaymentMethod.BANK_TRANSFER, Expense.PaymentMethod.CARD}:
        raise ValidationError({"method": "Choose CASH, BANK_TRANSFER or CARD."})
    reference = str(payment_reference or "").strip()[:160]
    if method != Expense.PaymentMethod.CASH and not reference:
        raise ValidationError({"payment_reference": "Enter the bank/card payment reference."})
    supplied_key = str(idempotency_key or "").strip()
    if len(supplied_key) > 120:
        raise ValidationError({"idempotency_key": "Use at most 120 characters."})
    ref_key = hashlib.sha256(reference.casefold().encode("utf-8")).hexdigest() if reference and method != Expense.PaymentMethod.CASH else None
    if approved_request:
        key = f"supplier-request:{approved_request.reference}"
    elif supplied_key:
        digest = hashlib.sha256(f"{expense.reference}:{supplied_key}".encode("utf-8")).hexdigest()
        key = f"expense-settlement:{digest}"
    elif ref_key:
        key = f"supplier-reference:{ref_key}"
    else:
        key = f"expense-settlement:{expense.reference}"

    existing = SupplierSettlement.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        requested_amount = existing.amount if amount is None else _money(amount)
        if (existing.expense_id != expense.pk or existing.method != method or
                existing.payment_reference != reference or existing.amount != requested_amount or
                (paid_on is not None and existing.paid_on != paid_on) or
                (receipt_url and existing.receipt_url != str(receipt_url)[:200]) or
                (method == Expense.PaymentMethod.CASH and cash_session is not None and not
                 existing.transaction.cash_movements.filter(cash_session=cash_session, type=CashMovement.Type.PAID_OUT).exists()) or
                (approved_request and existing.payment_request_id != approved_request.pk)):
            raise ValidationError({"idempotency_key": "This payment key was already used for different details."})
        return expense, False
    if ref_key and SupplierSettlement.objects.filter(external_reference_key=ref_key).exists():
        raise ValidationError({"payment_reference": "This payment reference has already been recorded."})
    if reference and Expense.objects.filter(payment_reference__iexact=reference).exclude(pk=expense.pk).exists():
        raise ValidationError({"payment_reference": "This reference is already used by a historical expense."})

    from .supplier_payment_service import payable_balance
    outstanding = payable_balance(expense)
    if outstanding <= Decimal("0.00"):
        # Preserve the existing endpoint's harmless repeat response for
        # historical fully paid rows, without posting a second journal.
        if expense.paid_at or expense.settlement_transaction_id:
            return expense, False
        raise ValidationError("This payable has no outstanding balance.")
    amount = _money(outstanding if amount is None else amount)
    if amount > outstanding:
        raise ValidationError({"amount": f"Payment exceeds the outstanding balance of {outstanding} {expense.currency}."})
    if method == Expense.PaymentMethod.CASH and amount < outstanding and not (supplied_key or approved_request):
        raise ValidationError({"idempotency_key": "A unique key is required for a partial cash payment."})
    if approved_request and Decimal(approved_request.amount).quantize(CENT) != amount:
        raise ValidationError({"amount": "The approved payment request covers a different amount."})
    paid_on = paid_on or hotel_today()
    if paid_on > hotel_today():
        raise ValidationError({"paid_on": "A supplier payment cannot be dated in the future."})
    if receipt_file is not None:
        from apps.core.validators import validate_document_upload
        validate_document_upload(receipt_file)

    session = None
    if method == Expense.PaymentMethod.CASH:
        if expense.currency != "NGN" or cash_session is None:
            raise ValidationError({"cash_session_reference": "An NGN cash payment requires an open cash session."})
        session = CashSession.objects.select_for_update().get(pk=cash_session.pk)
        if session.status != CashSession.Status.OPEN or session.cashier_id != actor.pk:
            raise PermissionDenied("Cash payment requires the acting cashier's open session.")
        if Decimal(session.expected_cash) < amount:
            raise ValidationError("Cash payment exceeds the drawer's expected cash.")
    elif cash_session is not None:
        raise ValidationError({"cash_session_reference": "A cash session is only valid for cash."})

    settlement_account = {
        Expense.PaymentMethod.CASH: accounting.CASH_ON_HAND,
        Expense.PaymentMethod.BANK_TRANSFER: accounting.BANK_CLEARING,
        Expense.PaymentMethod.CARD: accounting.CARD_CLEARING,
    }[method]
    journal_key = f"supplier-settlement:{hashlib.sha256(key.encode()).hexdigest()}"
    journal, created = create_posted_transaction(
        transaction_type=FinancialTransaction.Type.EXPENSE,
        source_key=journal_key, idempotency_key=journal_key, actor=actor,
        source_reference=expense.reference, external_reference=reference or expense.supplier_reference,
        narrative=f"Supplier payment for expense {expense.reference}: {expense.payee or expense.category}",
        currency=expense.currency, business_date=paid_on,
        metadata={"expense_settlement": True, "expense_id": expense.pk,
                  "expense_reference": expense.reference, "accounting_class": expense.accounting_class,
                  "settlement_method": method, "supplier_payment_request": getattr(approved_request, "reference", ""),
                  "client_idempotency_key": supplied_key},
        lines=[
            {"account_code": accounting.ACCOUNTS_PAYABLE, "direction": FinancialLine.Direction.DEBIT,
             "amount": amount, "description": f"Settle payable {expense.reference}"},
            {"account_code": settlement_account, "direction": FinancialLine.Direction.CREDIT,
             "amount": amount, "description": f"Paid supplier for {expense.reference}"},
        ],
    )
    if not created:
        raise ValidationError("A journal exists without matching settlement evidence; investigate before retrying.")
    try:
        with transaction.atomic():
            SupplierSettlement.objects.create(
                expense=expense, transaction=journal, payment_request=approved_request,
                idempotency_key=key, external_reference_key=ref_key, payment_reference=reference,
                method=method, amount=amount, currency=expense.currency, paid_on=paid_on,
                paid_by=actor, receipt_url=str(receipt_url or "")[:200], receipt_file=receipt_file,
            )
    except IntegrityError as exc:
        raise ValidationError({"payment_reference": "This payment or reference was recorded concurrently; review before retrying."}) from exc
    if session is not None:
        CashMovement.objects.create(
            cash_session=session, transaction=journal, type=CashMovement.Type.PAID_OUT,
            amount=amount, currency=expense.currency, actor=actor, source_reference=expense.reference,
            notes=f"Supplier payment {expense.reference}"[:500],
            metadata={"expense_id": expense.pk, "expense_reference": expense.reference, "settlement": True},
        )
        session.expected_cash = (Decimal(session.expected_cash) - amount).quantize(CENT)
        session.save(update_fields=["expected_cash", "updated_at"])
    remaining = (outstanding - amount).quantize(CENT)
    if remaining == Decimal("0.00"):
        updates = {"paid_at": timezone.now(), "paid_by": actor, "payment_reference": reference,
                   "payment_receipt_url": str(receipt_url or "")[:200], "settlement_transaction": journal}
        if receipt_file is not None:
            # The uploaded file was saved on the immutable instalment above;
            # reuse its storage name, rather than saving it twice.
            updates["payment_receipt_file"] = SupplierSettlement.objects.get(transaction=journal).receipt_file.name
        Expense.objects.filter(pk=expense.pk, settlement_transaction__isnull=True).update(**updates)
    expense.refresh_from_db()
    _event(expense=expense, event_type=ExpenseEvent.Type.PAID, actor=actor,
           details={"settlement_transaction": journal.reference, "method": method,
                    "payment_reference": reference, "amount": str(amount), "remaining": str(remaining),
                    "supplier_payment_request": getattr(approved_request, "reference", ""),
                    "authority": "APPROVED_PAYMENT_REQUEST" if approved_request else "APPROVER_DIRECT"})
    return expense, True


@transaction.atomic
def create_expense_handover(
    *, actor, category, amount, incurred_on=None,
    payment_method=Expense.PaymentMethod.PAYABLE,
    accounting_class=Expense.AccountingClass.OPERATING,
    currency="NGN", payee="", supplier_reference="", description="",
    notes="", purchase_order=None, workflow_origin=Expense.WorkflowOrigin.STOREKEEPER,
    idempotency_key=None, metadata=None,
) -> tuple[Expense, bool]:
    """Create ONE idempotent DRAFT handover, with no ledger posting.

    ``expense.request`` is deliberately narrower than ``expense.manage``: this
    function cannot set status, approve, post, settle, or attach a cash drawer.
    The Accountant must prepare/submit, an authorised approver must decide, and
    ``post_expense`` remains the only route to a financial transaction.
    """
    if not (has_capability(actor, "expense.request") or has_capability(actor, "expense.manage")):
        raise PermissionDenied("This user cannot create an expense handover.")
    if workflow_origin not in Expense.WorkflowOrigin.values or workflow_origin == Expense.WorkflowOrigin.DIRECT:
        raise ValidationError({"workflow_origin": "A controlled handover origin is required."})
    if payment_method not in Expense.PaymentMethod.values:
        raise ValidationError({"payment_method": "Invalid expense payment method."})
    if accounting_class not in Expense.AccountingClass.values:
        raise ValidationError({"accounting_class": "Invalid expense accounting class."})
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "An expense handover idempotency key is required."})
    existing = Expense.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        return existing, False

    category = str(category or "").strip()
    if not category:
        raise ValidationError({"category": "An expense category is required."})
    amount = _money(amount)
    currency = str(currency or "NGN").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError({"currency": "Use a three-letter ISO currency code."})
    incurred_on = incurred_on or hotel_today()
    if incurred_on > hotel_today():
        raise ValidationError({"incurred_on": "An expense cannot be incurred in the future."})

    expense = None
    for _ in range(8):
        try:
            with transaction.atomic():
                expense = Expense.objects.create(
                    reference=generate_finance_reference("EXP"), idempotency_key=key,
                    status=Expense.Status.DRAFT, category=category[:120], amount=amount,
                    currency=currency, incurred_on=incurred_on, payment_method=payment_method,
                    accounting_class=accounting_class, payee=str(payee or "")[:255],
                    supplier_reference=str(supplier_reference or "")[:120],
                    description=description or "", notes=notes or "", requested_by=actor,
                    purchase_order=purchase_order, workflow_origin=workflow_origin,
                    metadata=dict(metadata or {}),
                )
            break
        except IntegrityError:
            existing = Expense.objects.select_for_update().filter(idempotency_key=key).first()
            if existing:
                return existing, False
    else:
        raise ValidationError("Could not allocate a unique expense reference; please retry.")
    _event(
        expense=expense, event_type=ExpenseEvent.Type.CREATED, actor=actor,
        details={
            "workflow_origin": workflow_origin, "amount": str(amount),
            "payment_method": payment_method, "accounting_class": accounting_class,
            "purchase_order": expense.purchase_order.reference if expense.purchase_order_id else "",
            "ledger_effect": "none", "status": Expense.Status.DRAFT,
        },
    )
    return expense, True


@transaction.atomic
def prepare_expense_for_approval(*, expense: Expense, actor, **fields) -> Expense:
    """Accountant-only preparation of a DRAFT handover before submission.

    Amount, status and posting evidence are intentionally not writable from
    this endpoint. The preparation identity and event are append-only evidence.
    """
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("Only Finance staff can prepare an expense handover.")
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.workflow_origin == Expense.WorkflowOrigin.DIRECT:
        raise ValidationError("Only Store Keeper / Procurement handovers can be prepared through this endpoint.")
    if expense.status != Expense.Status.DRAFT:
        raise ValidationError("Only a DRAFT handover can be prepared.")
    allowed = {
        "category", "payee", "department", "supplier_reference", "description",
        "accounting_class", "payment_method", "incurred_on", "notes",
        "receipt_url",
    }
    changed = []
    for name, value in fields.items():
        if name not in allowed or value is None:
            continue
        if name == "accounting_class" and value not in Expense.AccountingClass.values:
            raise ValidationError({"accounting_class": "Invalid expense accounting class."})
        if name == "payment_method" and value not in Expense.PaymentMethod.values:
            raise ValidationError({"payment_method": "Invalid expense payment method."})
        if name == "incurred_on" and value > hotel_today():
            raise ValidationError({"incurred_on": "An expense cannot be incurred in the future."})
        setattr(expense, name, value)
        changed.append(name)
    expense.prepared_by = actor
    expense.prepared_at = timezone.now()
    expense.save(update_fields=changed + ["prepared_by", "prepared_at", "updated_at"])
    _event(
        expense=expense, event_type=ExpenseEvent.Type.PREPARED, actor=actor,
        details={"changed_fields": changed, "ledger_effect": "none"},
    )
    return expense
