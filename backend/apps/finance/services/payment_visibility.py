"""Supplier-payment and disbursement visibility for accounting and dashboards.

WHERE THE MONEY STAGES COME FROM
================================
The hotel records every supplier obligation through the ONE existing mechanism:
an :class:`~apps.finance.models.Expense` whose ``accounting_class`` says whether
it is an operating cost or an inventory acquisition (a supplier invoice for
goods), and whose ``payment_method`` says how it is settled (``CASH`` /
``BANK_TRANSFER`` / ``CARD`` = disbursed now, ``PAYABLE`` = an invoice on
account). Posting that expense writes balanced FinancialLine rows through
``expense_service.post_expense`` and ``accounting.create_posted_transaction``.

This module therefore adds **no new ledger, no new model and no new payment
record**. It reads the same immutable posted ledger the financial summary already
reads and reports the stages that were previously only implicit:

* **goods receipt** — a physical stock movement (``StockMovement.RECEIPT``). It
  moves quantity and carries unit cost but posts NO money. Its receipt value is
  reported separately as ``goods_receipts_value`` so supplier stock received is
  visible, and it is deliberately excluded from every posted-money total.
* **supplier invoice** — a posted ``EXPENSE`` transaction whose debit is
  ``ASSET.INVENTORY``. An obligation, settled now or carried on account.
* **payment approval** — the ``ApprovalRequest`` behind that expense. Approval
  authorises; it never moves money, so it is reported as a COUNT, never an amount.
* **actual disbursement** — a posted ``EXPENSE``/``PAYROLL_PAYMENT`` whose credit
  side is a tender asset (cash drawer, bank clearing, card clearing). The only
  thing counted as money leaving the hotel.

Because each stage is derived from a *different side* of the same immutable
postings, an invoice that is approved and then paid appears once as an obligation
and once as a disbursement — never twice inside one measure.

QUERY BUDGET
============
Every measure that can be expressed as a conditional aggregate over the period's
ledger lines is folded into the caller's EXISTING aggregate (see
``reporting_service._metrics``); it costs no additional statement. Only two
further statements are issued beyond the summary's own bounded plan:

1. one aggregate for the opening payable balance (transactions before the period),
2. one aggregate for the two expense-approval counts.

"Was this expense settled immediately or put on account?" is answered with
``transaction_id__in=<subquery>`` rather than a self-join, because joining
FinancialLine to itself inside a single aggregate multiplies rows and inflates
every total — exactly the double-counting the brief forbids.

COST OF GOODS SOLD IS REPORTED AS UNAVAILABLE, ON PURPOSE
========================================================
POS consumption writes an immutable *stock* movement with a unit cost, but no
journal entry is posted for it and the chart of accounts has no COGS account.
Reporting a figure here would fabricate an accounting balance, so the field is
``None`` with a stated reason.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.core.utils import money

from ..models import ApprovalRequest, FinancialLine, FinancialTransaction
from . import accounting

ZERO = Decimal("0.00")

#: Settlement accounts that represent money actually leaving the hotel.
TENDER_ASSET_ACCOUNTS = (accounting.CASH_ON_HAND, accounting.BANK_CLEARING, accounting.CARD_CLEARING)

#: Transaction types that can disburse money to a third party.
DISBURSEMENT_TYPES = (
    FinancialTransaction.Type.EXPENSE,
    FinancialTransaction.Type.PAYROLL_PAYMENT,
)


def settlement_measure_filters():
    """Conditional-aggregate filters for the disbursement stages.

    Returned as ``Q`` objects (not querysets) so the caller can evaluate them
    inside its existing ``aggregate()`` call at no extra query cost. The
    ``transaction_id__in`` subquery is what distinguishes "paid now" from "put
    on account": it selects the transactions that DEBIT the account in question.
    """
    def paid_out_of_tender_with_debit(**debit_filters):
        debits = FinancialLine.objects.filter(
            transaction__type__in=DISBURSEMENT_TYPES, **debit_filters,
        ).values("transaction_id")
        return Q(
            transaction__type__in=DISBURSEMENT_TYPES,
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
            transaction_id__in=debits,
        )

    # A later settlement of an on-account expense debits accounts payable
    # (not the expense/inventory account), so it is recognised by the
    # settlement marker the settlement service stamps on its transaction.
    def settled_on_account(accounting_class):
        return Q(
            transaction__type=FinancialTransaction.Type.EXPENSE,
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
            transaction__metadata__expense_settlement=True,
            transaction__metadata__accounting_class=accounting_class,
        )

    return {
        # Money that actually left a tender account, split by what it was for.
        "supplier_payments_made": paid_out_of_tender_with_debit(account_code=accounting.INVENTORY_ASSET) | settled_on_account("INVENTORY"),
        "operating_expenses_paid": paid_out_of_tender_with_debit(account_code=accounting.OPERATING_EXPENSE) | settled_on_account("OPERATING"),
        "net_payroll_paid": Q(
            transaction__type=FinancialTransaction.Type.PAYROLL_PAYMENT,
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
        ),
        "disbursement_reversals": Q(
            transaction__reversal_of__type__in=DISBURSEMENT_TYPES,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
        ),
        "payroll_cash_reversals": Q(
            transaction__reversal_of__type=FinancialTransaction.Type.PAYROLL_PAYMENT,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
        ),
        # Obligations raised in the period.
        "supplier_invoices_recognized": Q(
            transaction__type=FinancialTransaction.Type.EXPENSE,
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.INVENTORY_ASSET,
        ),
        "supplier_payables_raised": Q(
            direction=FinancialLine.Direction.CREDIT, account_code=accounting.ACCOUNTS_PAYABLE,
        ),
        "supplier_payables_settled": Q(
            direction=FinancialLine.Direction.DEBIT, account_code=accounting.ACCOUNTS_PAYABLE,
        ),
        # Every disbursement, so the remainder can be derived without a second pass.
        "total_disbursements": Q(
            transaction__type__in=DISBURSEMENT_TYPES,
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
        ),
    }


def _amount(value):
    return Decimal(value or ZERO)


def opening_payables_balance(*, before: date, currency: str) -> Decimal:
    """Posted payable balance strictly before ``before`` (the opening figure).

    One indexed aggregate. A period-end payable balance is the opening balance
    plus the period's net movement, which keeps a year-to-date position visible
    without scanning the whole ledger into memory.
    """
    lines = FinancialLine.objects.filter(
        transaction__status=FinancialTransaction.Status.POSTED,
        transaction__currency=currency,
        transaction__business_date__lt=before,
        account_code=accounting.ACCOUNTS_PAYABLE,
    )
    totals = lines.aggregate(
        credited=Sum("amount", filter=Q(direction=FinancialLine.Direction.CREDIT), default=ZERO),
        debited=Sum("amount", filter=Q(direction=FinancialLine.Direction.DEBIT), default=ZERO),
    )
    return _amount(totals["credited"]) - _amount(totals["debited"])


def expense_approval_counts(*, start_date: date, end_date: date) -> dict:
    """Expense-approval counts for the period. One aggregate, counts only."""
    return ApprovalRequest.objects.aggregate(
        decided=Count(
            "pk",
            filter=Q(
                type=ApprovalRequest.Type.EXPENSE,
                status__in=[ApprovalRequest.Status.APPROVED, ApprovalRequest.Status.REJECTED],
                reviewed_at__date__gte=start_date,
                reviewed_at__date__lte=end_date,
            ),
        ),
        pending=Count("pk", filter=Q(type=ApprovalRequest.Type.EXPENSE, status=ApprovalRequest.Status.PENDING)),
    )


def supplier_payment_visibility(
    *, totals: dict, start_date: date, end_date: date, currency: str,
    goods_receipts_value: Decimal = ZERO, goods_receipts_count: int = 0,
) -> dict:
    """Assemble the supplier-payment block from one aggregate plus two counts.

    ``totals`` must be the result of aggregating :func:`settlement_measure_filters`
    over the period's posted ledger lines (``reporting_service`` does this in its
    own bounded query plan). ``goods_receipts_value`` is an operational stock
    receipt total supplied by the caller; it is visibility only and is never
    added to a posted-money measure.
    """
    supplier_payments_made = _amount(totals.get("supplier_payments_made"))
    operating_expenses_paid = _amount(totals.get("operating_expenses_paid"))
    net_payroll_paid = _amount(totals.get("net_payroll_paid")) - _amount(totals.get("payroll_cash_reversals"))
    total_disbursements = _amount(totals.get("total_disbursements")) - _amount(totals.get("disbursement_reversals"))

    # "Other" is a remainder, so no posting can be counted in both this measure and
    # a more specific one, and the parts always sum to the reported total.
    other_disbursements = total_disbursements - supplier_payments_made - operating_expenses_paid - net_payroll_paid

    invoices_recognized = _amount(totals.get("supplier_invoices_recognized"))
    payables_raised = _amount(totals.get("supplier_payables_raised"))
    payables_settled = _amount(totals.get("supplier_payables_settled"))

    opening = opening_payables_balance(before=start_date, currency=currency)
    outstanding = opening + payables_raised - payables_settled
    approvals = expense_approval_counts(start_date=start_date, end_date=end_date)

    return {
        "goods_receipts_value": money(_amount(goods_receipts_value)),
        "goods_receipts_count": int(goods_receipts_count or 0),
        "supplier_invoices_recognized": money(invoices_recognized),
        "supplier_payments_made": money(supplier_payments_made),
        "operating_expenses_paid": money(operating_expenses_paid),
        "net_payroll_paid": money(net_payroll_paid),
        "other_cash_disbursements": money(other_disbursements),
        "total_cash_disbursements": money(total_disbursements),
        "supplier_payables_raised": money(payables_raised),
        "supplier_payables_settled": money(payables_settled),
        "supplier_payables_opening_balance": money(opening),
        "outstanding_supplier_payables": money(outstanding),
        "supplier_payment_approvals_decided": approvals["decided"] or 0,
        "supplier_payment_approvals_pending": approvals["pending"] or 0,
        "payable_settlement_posting_available": True,
        "payable_settlement_posting_note": (
            "An expense recorded as PAYABLE credits LIABILITY.ACCOUNTS_PAYABLE when posted. Recording the "
            "supplier payment (Accounting → Record supplier payment) posts Dr ACCOUNTS_PAYABLE / Cr the tender "
            "actually used and stamps the expense paid — the payable settles exactly once and the obligation is "
            "never expensed a second time."
        ),
        # COGS is not derivable from posted ledger evidence: reported as
        # unavailable rather than invented.
        "cost_of_goods_sold": None,
        "cost_of_goods_sold_unavailable_reason": (
            "POS stock consumption is recorded in the immutable stock ledger with a unit cost, but no "
            "journal entry is posted for it and the chart of accounts has no COGS account. Reporting a "
            "figure here would fabricate an accounting balance."
        ),
        "definitions": {
            "supplier_invoices_recognized": (
                "Debit postings to ASSET.INVENTORY from EXPENSE transactions in the period: goods received "
                "on invoice, whether paid now or carried on account."
            ),
            "supplier_payments_made": (
                "Credit postings to cash/bank/card on EXPENSE transactions that debit ASSET.INVENTORY: money "
                "actually paid to suppliers in the period."
            ),
            "operating_expenses_paid": (
                "Credit postings to cash/bank/card on EXPENSE transactions that debit EXPENSE.OPERATING: "
                "operating costs actually disbursed, as distinct from recognized."
            ),
            "net_payroll_paid": "Salary payments out of a tender account, less reversals of those payments.",
            "other_cash_disbursements": (
                "Other posted expense/payroll tender credits net of their reversal debits (not guest refunds). "
                "Derived as a remainder, so no posting can be counted in "
                "both this measure and a more specific one."
            ),
            "outstanding_supplier_payables": (
                "Posted payable balance before the period, plus credits raised, less debits settled: a "
                "period-end balance, not a movement. See `payable_settlement_posting_note` for why the "
                "credited balance is not yet reducible by a settlement entry."
            ),
            "supplier_payment_approvals_decided": (
                "Count of expense approval decisions in the period. Approval authorises a payment; it never "
                "moves money, so it is reported as a count and never as an amount."
            ),
            "goods_receipts": (
                "Physical stock receipts move quantity and unit cost only and post no money. Their value is "
                "reported separately as goods_receipts_value and excluded from posted-money totals to avoid "
                "double-counting against the supplier invoice/expense when Finance posts it."
            ),
            "goods_receipts_value": (
                "GoodsReceiptLine quantity received multiplied by unit cost for goods receipts in the period. "
                "This is stock received from suppliers, not a posted expense, invoice, payable, or payment."
            ),
        },
    }


__all__ = [
    "supplier_payment_visibility", "settlement_measure_filters", "expense_approval_counts",
    "opening_payables_balance", "TENDER_ASSET_ACCOUNTS", "DISBURSEMENT_TYPES",
]
