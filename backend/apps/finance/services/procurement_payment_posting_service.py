"""Finance-owned reconciliation of a Procurement supplier payment.

A ``PurchaseOrderPayment`` is operational evidence that money left the hotel.
It is NOT a financial posting. This service links that evidence to exactly one
controlled Finance record:

* an already-executed ``SupplierPaymentRequest`` → MIRRORED;
* an existing payable/expense for the purchase order → POSTED or
  HANDED_TO_ACCOUNTING, depending on whether money has actually been posted;
* otherwise ONE idempotent DRAFT ``Expense`` handover, which an Accountant
  must prepare and submit and an authorised approver must decide before the
  Accountant can post it;
* if the actor has neither ``expense.request`` nor ``expense.manage``, a
  REQUIRES_REVIEW exception, never a silent fabricated posting.

The purchase-order payment is append-only; only its three traceability
references are written through a controlled QuerySet.update. Historical
posted financial rows are never edited or deleted.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.accounts.capabilities import has_capability

from ..models import Expense, FinancialTransaction, SupplierPaymentRequest
from .expense_service import create_expense_handover


def _settlement_link(reference: str) -> tuple[str, str]:
    request = (
        SupplierPaymentRequest.objects.select_related("expense", "settlement_transaction")
        .filter(reference=reference)
        .first()
    )
    if not request:
        return "", ""
    expense_reference = request.expense.reference if request.expense_id else ""
    transaction_reference = (
        request.settlement_transaction.reference if request.settlement_transaction_id else ""
    )
    return expense_reference, transaction_reference


def _expense_posted_money(expense: Expense) -> tuple[bool, str]:
    """Return whether the linked expense has actually moved money out.

    A posted ``PAYABLE`` expense creates a LIABILITY only; its later settlement
    is the money-out transaction. A posted CASH/BANK/CARD expense credits a
    tender account at posting, so no second settlement is expected.
    """
    if expense.status != Expense.Status.POSTED:
        return False, ""
    if (expense.settlement_transaction_id
            and expense.settlement_transaction.status == FinancialTransaction.Status.POSTED):
        return True, expense.settlement_transaction.reference
    if (expense.financial_transaction_id and expense.payment_method != Expense.PaymentMethod.PAYABLE
            and expense.financial_transaction.status == FinancialTransaction.Status.POSTED):
        return True, expense.financial_transaction.reference
    return False, ""


@transaction.atomic
def reconcile_procurement_payment_with_accounting(*, payment, actor=None) -> dict:
    """Link one persisted Procurement payment to one controlled Finance record."""
    from apps.inventory.models import PurchaseOrderPayment

    payment = (
        PurchaseOrderPayment.objects.select_for_update()
        .select_related("purchase_order", "purchase_order__supplier")
        .get(pk=payment.pk)
    )
    po = payment.purchase_order
    expense_reference = ""
    transaction_reference = ""
    created = False

    if payment.accounting_settlement_reference:
        expense_reference, transaction_reference = _settlement_link(
            payment.accounting_settlement_reference
        )
        status = PurchaseOrderPayment.AccountingPostingStatus.MIRRORED
    else:
        existing = (
            Expense.objects.select_for_update()
            .select_related("financial_transaction", "settlement_transaction")
            .filter(purchase_order=po)
            .first()
        )
        if existing is not None:
            expense_reference = existing.reference
            if existing.status in {Expense.Status.REJECTED, Expense.Status.VOIDED}:
                status = PurchaseOrderPayment.AccountingPostingStatus.REQUIRES_REVIEW
            else:
                posted, transaction_reference = _expense_posted_money(existing)
                source_id = (existing.metadata or {}).get("procurement_payment_id")
                if source_id is not None and str(source_id) != str(payment.pk):
                    # One payable per PO is not proof that a later instalment
                    # was represented by the first handover's journal.
                    transaction_reference = ""
                    status = PurchaseOrderPayment.AccountingPostingStatus.REQUIRES_REVIEW
                elif posted and not (
                    str(source_id) == str(payment.pk)
                    and existing.workflow_origin == Expense.WorkflowOrigin.PROCUREMENT_PAYMENT
                    and existing.payment_method != Expense.PaymentMethod.PAYABLE
                    and existing.amount == payment.amount
                    and existing.currency == po.currency
                    and existing.supplier_reference == (payment.reference or po.reference)[:120]
                    and {"BANK_TRANSFER": Expense.PaymentMethod.BANK_TRANSFER,
                         "CHEQUE": Expense.PaymentMethod.BANK_TRANSFER,
                         "CARD": Expense.PaymentMethod.CARD}.get(payment.method) == existing.payment_method
                ):
                    # An unrelated payable/settlement on the same order is not
                    # evidence that THIS disbursement was posted. A verified
                    # explicit supplier-payment-request link uses MIRRORED.
                    transaction_reference = ""
                    status = PurchaseOrderPayment.AccountingPostingStatus.REQUIRES_REVIEW
                else:
                    status = (
                        PurchaseOrderPayment.AccountingPostingStatus.POSTED
                        if posted
                        else PurchaseOrderPayment.AccountingPostingStatus.HANDED_TO_ACCOUNTING
                    )
        elif has_capability(actor, "expense.request") or has_capability(actor, "expense.manage"):
            method_map = {
                PurchaseOrderPayment.PaymentMethod.BANK_TRANSFER: Expense.PaymentMethod.BANK_TRANSFER,
                PurchaseOrderPayment.PaymentMethod.CHEQUE: Expense.PaymentMethod.BANK_TRANSFER,
                PurchaseOrderPayment.PaymentMethod.CARD: Expense.PaymentMethod.CARD,
                # Cash procurement payments need a cashier-controlled cash
                # session at settlement. Keep the handover as a PAYABLE draft
                # so posting cannot invent an unsupported CASH_ON_HAND credit.
                PurchaseOrderPayment.PaymentMethod.CASH: Expense.PaymentMethod.PAYABLE,
            }
            expense = None
            try:
                with transaction.atomic():
                    expense, created = create_expense_handover(
                        actor=actor,
                        category="Supplier inventory payment",
                        amount=payment.amount,
                        incurred_on=payment.paid_on,
                        payment_method=method_map.get(
                            payment.method, Expense.PaymentMethod.BANK_TRANSFER
                        ),
                        accounting_class=Expense.AccountingClass.INVENTORY,
                        currency=po.currency,
                        payee=po.supplier.name,
                        supplier_reference=payment.reference or po.reference,
                        description=f"Procurement payment {payment.reference or payment.pk} for {po.reference}",
                        notes=payment.notes or "",
                        purchase_order=po,
                        workflow_origin=Expense.WorkflowOrigin.PROCUREMENT_PAYMENT,
                        idempotency_key=f"procurement-payment:{payment.pk}",
                        metadata={
                            "source": "PROCUREMENT_PAYMENT",
                            "procurement_payment_id": payment.pk,
                            "purchase_order_reference": po.reference,
                            "procurement_payment_method": payment.method,
                            "bank_reference": payment.reference,
                            "evidence_only": True,
                        },
                    )
            except IntegrityError:
                # A concurrent handover won the one-payable-per-PO race. Use it.
                expense = (
                    Expense.objects.select_for_update()
                    .filter(purchase_order=po)
                    .first()
                )
                created = False
            if expense is not None:
                expense_reference = expense.reference
                posted, transaction_reference = _expense_posted_money(expense)
                status = (
                    PurchaseOrderPayment.AccountingPostingStatus.POSTED
                    if posted
                    else PurchaseOrderPayment.AccountingPostingStatus.HANDED_TO_ACCOUNTING
                )
            else:
                status = PurchaseOrderPayment.AccountingPostingStatus.REQUIRES_REVIEW
        else:
            status = PurchaseOrderPayment.AccountingPostingStatus.REQUIRES_REVIEW

    PurchaseOrderPayment.objects.filter(pk=payment.pk).update(
        finance_expense_reference=expense_reference,
        finance_transaction_reference=transaction_reference,
        accounting_posting_status=status,
    )
    payment.finance_expense_reference = expense_reference
    payment.finance_transaction_reference = transaction_reference
    payment.accounting_posting_status = status
    return {
        "status": status,
        "finance_expense_reference": expense_reference,
        "finance_transaction_reference": transaction_reference,
        "created_handover": bool(created),
        "ledger_posting_created": False,
    }


__all__ = ["reconcile_procurement_payment_with_accounting"]
