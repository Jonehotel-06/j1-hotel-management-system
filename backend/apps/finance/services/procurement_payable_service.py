"""Accounting's side of the purchase-order hand-off: the supplier payable.

Procurement owns the order, its approval and the goods receipts. Accounting owns
whether the hotel owes the supplier money and how much of it is on the books.
Before this module those two sides touched nowhere: a submitted purchase order
could not be turned into a payable *from the order*, so an accountant re-typed
the supplier, the amount and the reference into a free-standing expense. The
purchase order, the receipt, the payable and the payment were then four
unconnected records, and the same delivery could be billed twice.

What this module does
=====================
``purchase_order_payable_summary``
    One read-only projection per order: what arrived (valued at the received
    unit costs), what has been billed, what is left, and what has been paid.
    Derived from the records every time it is asked for; nothing is stored.

``raise_payable_from_purchase_order``
    Creates the supplier payable as an ordinary ``Expense`` on account
    (``payment_method=PAYABLE``) linked to the order, so it travels through the
    *existing* expense submit -> approve -> post controls and lands in the same
    liabilities the reports already read. This module posts nothing itself.

The rules it applies (all of them verified against the current ledger design)
=============================================================================
* A draft or a merely submitted order is not a liability. Only goods that have
  been receipted are payable, and only up to the value actually received
  (``GoodsReceiptLine.quantity_received * unit_cost``), never the ordered total.
  A partially received order therefore raises a payable for the part delivered.
* One payable per purchase order, enforced by a database constraint as well as
  this check, so a retry or a double click cannot bill the same delivery twice.
* The payable is raised, never approved: the accountant who raises it cannot
  post it, and the expense workflow's own maker-checker decides whether it hits
  the ledger.

Open business decision (documented, not invented here)
=====================================================
A second delivery that arrives *after* the payable for an order has been raised
cannot be added to that payable: posted expenses are immutable and the one-payable-
per-order constraint is deliberate. The reconciliation pack reports the unbilled
remainder every day (``PO_RECEIVED_NOT_BILLED``) so the money is never silently
lost, but the hotel must decide whether the remainder becomes a supplementary
payable (needs a payable-adjustment model) or a separately keyed supplier
invoice. This agent does not invent that model.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.capabilities import has_capability

from ..models import Expense, FinancialTransaction, SupplierSettlement
from . import accounting
from .expense_service import create_expense

TENDER_CREDIT_ACCOUNTS = (
    accounting.CASH_ON_HAND, accounting.BANK_CLEARING, accounting.CARD_CLEARING,
)


def _posted_and_not_reversed(transaction) -> bool:
    """A journal is evidence only while POSTED and not reversed by a POSTED VOID."""
    if transaction is None or getattr(transaction, "status", None) != FinancialTransaction.Status.POSTED:
        return False
    return not transaction.reversals.filter(
        status=FinancialTransaction.Status.POSTED
    ).exists()


def _active_settlements_total(expense) -> Decimal:
    """Posted supplier instalments, excluding payments later reversed."""
    rows = SupplierSettlement.objects.filter(
        expense=expense,
        transaction__status=FinancialTransaction.Status.POSTED,
    ).exclude(
        transaction__reversals__status=FinancialTransaction.Status.POSTED
    )
    return sum((Decimal(row.amount) for row in rows), ZERO).quantize(CENT)


def posted_expense_paid_amount(expense) -> Decimal:
    """Money ACTUALLY paid against one expense, from posted evidence only.

    Three legitimate shapes are handled:

    * an expense paid at posting (CASH / BANK_TRANSFER / CARD) — its posted
      journal credits a tender account;
    * a PAYABLE settled in instalments — each active ``SupplierSettlement``;
    * a legacy fully settled PAYABLE — ``paid_at`` / ``settlement_transaction``.

    A reversed payment never counts. Draft, submitted, approved and voided
    expenses have no posted money and therefore pay nothing.
    """
    if expense.status != Expense.Status.POSTED:
        return ZERO
    amount = Decimal(expense.amount).quantize(CENT)
    invoice = expense.financial_transaction
    if not _posted_and_not_reversed(invoice):
        return ZERO
    marker = expense.settlement_transaction
    if expense.paid_at and _posted_and_not_reversed(marker):
        return amount
    if expense.payment_method != Expense.PaymentMethod.PAYABLE:
        if invoice.lines.filter(
            account_code__in=TENDER_CREDIT_ACCOUNTS,
            direction="CREDIT",
        ).exists():
            return amount
        return ZERO
    active = _active_settlements_total(expense)
    # A legacy final-settlement marker without a linked journal is historical
    # evidence of full payment; keep it readable without inventing a posting.
    if expense.paid_at and marker is None:
        return amount
    return min(active, amount)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Purchase-order statuses that could carry a payable. A DRAFT is the
#: requester's unfinished work and a SUBMITTED order has not been approved, so
#: neither can be billed. CANCELLED never can.
PAYABLE_ORDER_STATUSES = (
    "APPROVED",
    "ORDERED",
    "PARTIALLY_RECEIVED",
    "RECEIVED",
    "PAID",
)

#: Expenses in these states do not count as a live payable for duplicate
#: detection: a rejected document never became an obligation, and a voided one
#: was withdrawn through the approved route.
DEAD_PAYABLE_STATUSES = (Expense.Status.REJECTED, Expense.Status.VOIDED)


def _received_value(purchase_order) -> Decimal:
    total = sum(
        (Decimal(line.quantity_received) * Decimal(line.unit_cost)
         for receipt in purchase_order.goods_receipts.all() for line in receipt.lines.all()),
        ZERO,
    )
    return Decimal(total).quantize(CENT, rounding=ROUND_HALF_UP)


def _live_payables(purchase_order):
    return list(
        Expense.objects.filter(purchase_order=purchase_order)
        .exclude(status__in=DEAD_PAYABLE_STATUSES)
        .select_related("financial_transaction", "settlement_transaction")
        .prefetch_related("supplier_settlements__transaction")
        .order_by("created_at", "pk")
    )


def purchase_order_payable_summary(purchase_order) -> dict:
    """Read-only payable position of one purchase order."""
    received = _received_value(purchase_order)
    payables = _live_payables(purchase_order)
    # ``payable_raised`` is the document total (kept for the existing API
    # contract): a DRAFT/SUBMITTED/APPROVED document is NOT a posted liability.
    billed = sum((Decimal(expense.amount) for expense in payables), ZERO).quantize(CENT)
    posted = [expense for expense in payables if _posted_and_not_reversed(expense.financial_transaction)]
    posted_liability = sum((Decimal(expense.amount) for expense in posted), ZERO).quantize(CENT)
    unposted_documents = sum(
        (Decimal(expense.amount) for expense in payables if expense not in posted), ZERO
    ).quantize(CENT)
    paid = sum((posted_expense_paid_amount(expense) for expense in posted), ZERO).quantize(CENT)
    return {
        "purchase_order": purchase_order.reference,
        "status": purchase_order.status,
        "currency": purchase_order.currency,
        "ordered_total": str(purchase_order.total_amount),
        "received_value": str(received),
        "payable_raised": str(billed),
        # Accounting-audit repair: outstanding is a POSTED liability less
        # VALID payments. Draft payables are no liability at all; reversed
        # payments do not discharge an invoice.
        "payable_outstanding": str(max(posted_liability - paid, ZERO).quantize(CENT)),
        "posted_payable_liability": str(posted_liability),
        "unposted_payable_documents": str(unposted_documents),
        "actual_supplier_payments": str(paid),
        "unbilled_received_value": str(max(received - billed, ZERO).quantize(CENT)),
        "payables": [
            {
                "reference": expense.reference,
                "status": expense.status,
                "amount": str(Decimal(expense.amount).quantize(CENT)),
                "paid_amount": str(posted_expense_paid_amount(expense).quantize(CENT)),
                "outstanding_amount": str(
                    (Decimal(expense.amount) - posted_expense_paid_amount(expense)).quantize(CENT)
                    if expense.status == Expense.Status.POSTED else ZERO
                ),
                "posted_liability": str(
                    Decimal(expense.amount).quantize(CENT)
                    if _posted_and_not_reversed(expense.financial_transaction) else ZERO
                ),
                "payment_method": expense.payment_method,
                "paid_at": expense.paid_at.isoformat() if expense.paid_at else None,
                "payment_reference": expense.payment_reference,
                "settlement_reference": (
                    expense.settlement_transaction.reference if expense.settlement_transaction_id else None
                ),
                "approval_reference": expense.approval_request.reference if expense.approval_request_id else None,
            }
            for expense in payables
        ],
        "can_raise_payable": (
            purchase_order.status in PAYABLE_ORDER_STATUSES
            and received > ZERO
            and not payables
        ),
    }


def supplier_invoice_status_summary(*, end_date, currency="NGN") -> dict:
    """Unpaid / partially paid / paid POSTED supplier invoices (as-of a date).

    Only posted expenses with a posted, unreversed journal are invoices. The
    classification uses the same payment evidence as the purchase-order
    payable position, so the Accounting page cannot disagree with its own
    drill-down. Reversed payments reopen the liability.
    """
    rows = (
        Expense.objects.filter(
            status=Expense.Status.POSTED,
            currency=currency,
            financial_transaction__business_date__lte=end_date,
        )
        .select_related("financial_transaction", "settlement_transaction")
        .prefetch_related("supplier_settlements__transaction")
    )
    buckets = {
        "UNPAID": {"invoices": 0, "amount": ZERO},
        "PARTIALLY_PAID": {"invoices": 0, "amount": ZERO},
        "PAID": {"invoices": 0, "amount": ZERO},
        "CANCELLED": {"invoices": 0, "amount": ZERO},
    }
    for expense in rows:
        if not _posted_and_not_reversed(expense.financial_transaction):
            buckets["CANCELLED"]["invoices"] += 1
            continue
        amount = Decimal(expense.amount).quantize(CENT)
        paid = min(posted_expense_paid_amount(expense), amount)
        outstanding = max(amount - paid, ZERO).quantize(CENT)
        if expense.payment_method != Expense.PaymentMethod.PAYABLE and paid >= amount:
            state = "PAID"
        elif paid <= ZERO:
            state = "UNPAID"
        elif outstanding > ZERO:
            state = "PARTIALLY_PAID"
        else:
            state = "PAID"
        buckets[state]["invoices"] += 1
        buckets[state]["amount"] += outstanding
    return {
        state: {"invoices": entry["invoices"],
                "outstanding_amount": str(entry["amount"].quantize(CENT))}
        for state, entry in buckets.items()
    }


def purchase_order_payables_index(*, references=None) -> dict:
    """Payable positions keyed by purchase-order reference, for the Accounting list."""
    from apps.inventory.models import PurchaseOrder

    queryset = PurchaseOrder.objects.filter(status__in=PAYABLE_ORDER_STATUSES)
    if references:
        queryset = queryset.filter(reference__in=list(references))
    index = {}
    for purchase_order in queryset.select_related("supplier").prefetch_related(
        "lines", "goods_receipts__lines", "payables__settlement_transaction", "payables__approval_request",
    ):
        index[purchase_order.reference] = purchase_order_payable_summary(purchase_order)
    return index


def purchase_order_payable_position(index: dict, *, as_of) -> dict:
    """Accounting-page summary for an already-built payable index.

    Kept OUT of ``financial_summary`` so the shared summary keeps its exact
    query budget; this is called by the Accounting purchase-order endpoint.
    """
    from .payment_visibility import approved_unpaid_procurement_visibility

    return {
        "as_of": as_of.isoformat(),
        "purchase_orders": len(index),
        "approved_unpaid_procurement": approved_unpaid_procurement_visibility(
            end_date=as_of, currency="NGN"
        ),
        "posted_liability": str(sum(
            (Decimal(row["posted_payable_liability"]) for row in index.values()), ZERO
        ).quantize(CENT)),
        "outstanding_payable": str(sum(
            (Decimal(row["payable_outstanding"]) for row in index.values()), ZERO
        ).quantize(CENT)),
        "actual_supplier_payments": str(sum(
            (Decimal(row["actual_supplier_payments"]) for row in index.values()), ZERO
        ).quantize(CENT)),
        "unposted_payable_documents": str(sum(
            (Decimal(row["unposted_payable_documents"]) for row in index.values()), ZERO
        ).quantize(CENT)),
        "invoice_status": supplier_invoice_status_summary(end_date=as_of, currency="NGN"),
        "approval_is_not_payment": (
            "Approved purchase orders and goods receipts post no cash. Only a posted expense/payable "
            "and an executed, unreversed supplier settlement are accounting money. Draft payables "
            "are documents, not liabilities."
        ),
    }


@transaction.atomic
def raise_payable_from_purchase_order(
    *, purchase_order, actor, amount=None, category="", notes="",
    accounting_class=Expense.AccountingClass.INVENTORY, idempotency_key=None,
) -> tuple[Expense, bool]:
    """Raise the supplier payable for the value actually received on one order."""
    if not has_capability(actor, "expense.manage"):
        raise PermissionDenied("This user cannot raise supplier payables.")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "A payable idempotency key is required."})

    # Lock the order so two accountants cannot both pass the "no payable yet"
    # check and then race the unique constraint.
    from apps.inventory.models import PurchaseOrder

    purchase_order = (
        PurchaseOrder.objects.select_for_update()
        .select_related("supplier")
        .prefetch_related("lines", "goods_receipts__lines", "payables")
        .get(pk=purchase_order.pk)
    )
    if purchase_order.status in {"DRAFT", "SUBMITTED"}:
        raise ValidationError(
            "This purchase order has not been approved yet. Accounting raises a payable only after "
            "the order is approved and its goods are received."
        )
    if purchase_order.status == "CANCELLED":
        raise ValidationError("A cancelled purchase order cannot be billed.")
    if purchase_order.status not in PAYABLE_ORDER_STATUSES:
        raise ValidationError(f"A payable cannot be raised from a {purchase_order.status} order.")

    received = _received_value(purchase_order)
    if received <= ZERO:
        raise ValidationError(
            "No goods have been received against this order yet, so nothing is payable. "
            "A submitted purchase order is not proof of delivery."
        )

    existing = _live_payables(purchase_order)
    if existing:
        raise ValidationError({
            "purchase_order": (
                f"Payable {existing[0].reference} already exists for {purchase_order.reference} "
                f"({existing[0].status}). Raising a second one would bill the same delivery twice."
            )
        })
    if amount is None:
        amount = received
    try:
        amount = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception as exc:  # noqa: BLE001 - surfaced as a field error
        raise ValidationError({"amount": "Enter a valid two-decimal amount."}) from exc
    if amount <= ZERO:
        raise ValidationError({"amount": "Enter a payable amount greater than zero."})
    if amount > received:
        raise ValidationError({
            "amount": (
                f"The payable cannot exceed the value received ({received} {purchase_order.currency}). "
                "Receipt the remaining delivery first if the supplier invoiced more."
            )
        })

    category = (category or "").strip() or "Inventory purchase"
    try:
        expense, created = create_expense(
            actor=actor,
            category=category,
            amount=amount,
            payment_method=Expense.PaymentMethod.PAYABLE,
            accounting_class=accounting_class,
            currency=purchase_order.currency,
            payee=purchase_order.supplier.name if purchase_order.supplier_id else "",
            supplier_reference=purchase_order.supplier_reference or purchase_order.reference,
            description=(
                f"Supplier payable for purchase order {purchase_order.reference} "
                f"({purchase_order.supplier.name if purchase_order.supplier_id else 'supplier'}), "
                f"goods received to the value of {received} {purchase_order.currency}."
            ),
            notes=notes,
            idempotency_key=key,
            purchase_order=purchase_order,
            metadata={
                "raised_from_purchase_order": purchase_order.reference,
                "received_value": str(received),
                "receipt_references": [receipt.reference for receipt in purchase_order.goods_receipts.all()],
            },
        )
    except IntegrityError as exc:
        raise ValidationError({
            "purchase_order": "A payable already exists for this purchase order (concurrent request)."
        }) from exc
    return expense, created
