"""One authoritative reconciliation contract over the posted financial ledger.

Calculations are NOT re-implemented here. This module checks the measures
already produced by ``reporting_service.financial_summary`` and the
ASSET.GUEST_RECEIVABLE control-account report, then exposes the arithmetic,
duplicate controls and unposted operational exceptions in one machine-readable
block. Every amount is taken from immutable POSTED financial lines; drafts,
approval counts, stock receipts and legacy Payment/Booking projections never
enter a financial measure.

The output intentionally distinguishes a BROKEN arithmetic identity from a
RECONCILED_WITH_EXCEPTIONS state: an unposted Procurement payment is a workflow
exception to investigate, not a fabricated second ledger posting.
"""
from decimal import Decimal

from apps.core.utils import money

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _amount(value) -> Decimal:
    """Accept either the scalar or ``{"amount": ...}`` shape of report keys."""
    if isinstance(value, dict):
        value = value.get("amount")
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value)).quantize(CENT)
    except (TypeError, ValueError, ArithmeticError):
        return ZERO


def _check(*, key, label, left, right, formula, condition=None):
    left, right = _amount(left), _amount(right)
    difference = left - right
    reconciles = bool(condition) if condition is not None else difference == ZERO
    return {
        "key": key,
        "label": label,
        "left": money(left),
        "right": money(right),
        "difference": money(difference),
        "reconciles": reconciles,
        "formula": formula,
    }


def build_reconciliation(*, summary: dict, receivables: dict) -> dict:
    """Build the reconciliation block from the two authoritative reports."""
    payments = summary.get("supplier_payments") or {}
    profit_and_loss = summary.get("profit_and_loss") or {}
    receivables_reconciliation = receivables.get("reconciliation") or {}
    settlement = receivables.get("settlement") or {}
    unposted = payments.get("unposted_procurement_payments") or {}
    unposted_count = int(unposted.get("count") or 0)
    unposted_amount = _amount(unposted.get("amount"))

    revenue = _amount(summary.get("recognized_revenue"))
    discounts = _amount(summary.get("discounts"))
    net_income = _amount(profit_and_loss.get("net_income"))
    total_expenses = _amount(summary.get("total_expenses"))
    net_result = _amount(summary.get("net_result"))
    operating_expenses = _amount(summary.get("operating_expenses"))
    payroll_expenses = _amount(summary.get("payroll_expenses"))
    gross_operating_result = _amount(summary.get("gross_operating_result"))
    collections = _amount(summary.get("collections"))
    refunds = _amount(summary.get("refunds"))
    disbursements = _amount(payments.get("total_cash_disbursements"))
    net_cash_movement = _amount(summary.get("net_cash_movement"))

    disbursement_parts = sum(
        (
            _amount(payments.get("supplier_payments_made")),
            _amount(payments.get("operating_expenses_paid")),
            _amount(payments.get("net_payroll_paid")),
            _amount(payments.get("other_cash_disbursements")),
        ),
        ZERO,
    )
    opening_payables = _amount(payments.get("supplier_payables_opening_balance"))
    payables_raised = _amount(payments.get("supplier_payables_raised"))
    payables_settled = _amount(payments.get("supplier_payables_settled"))
    outstanding_payables = _amount(payments.get("outstanding_supplier_payables"))

    gross_receivables = _amount(receivables.get("gross_receivables"))
    guest_credits = _amount(receivables.get("guest_credit_balances"))
    unassigned_receivables = _amount(receivables.get("unassigned_receivables"))
    net_receivable = _amount(receivables.get("net_receivable"))
    control_balance = _amount(receivables_reconciliation.get("control_account_balance"))
    folio_balances = _amount(receivables_reconciliation.get("sum_of_folio_balances"))
    no_folio = _amount(receivables_reconciliation.get("receivables_without_a_folio"))
    paid_amount = _amount((settlement.get("PAID") or {}).get("amount"))
    credit_folios = int(receivables.get("folios_in_credit") or 0)
    expense_breakdown = (summary.get("expense_breakdown") or {}).get("reconciliation") or {}

    checks = [
        _check(
            key="net_income", label="Earned revenue less discounts",
            left=revenue - discounts, right=net_income,
            formula="recognized_revenue - discounts = net_income",
        ),
        _check(
            key="net_result", label="Accrual operating result",
            left=net_income - total_expenses, right=net_result,
            formula="net_income - total_posted_expenses = net_result",
        ),
        _check(
            key="gross_operating_result", label="Gross operating result",
            left=revenue - operating_expenses - payroll_expenses - discounts,
            right=gross_operating_result,
            formula="recognized_revenue - operating_expenses - payroll_expenses - discounts = gross_operating_result",
        ),
        _check(
            key="net_cash_movement", label="Net cash movement",
            left=collections - refunds - disbursements, right=net_cash_movement,
            formula="collections - refunds - total_cash_disbursements = net_cash_movement",
        ),
        _check(
            key="cash_disbursement_components", label="Cash disbursement components",
            left=disbursement_parts, right=disbursements,
            formula="supplier_payments + operating_paid + net_payroll + other_disbursements = total_cash_disbursements",
        ),
        _check(
            key="expense_breakdown", label="Expense breakdown",
            left=_amount(expense_breakdown.get("ledger_total")),
            right=_amount(expense_breakdown.get("components_total")),
            formula="posted expense ledger total = sum of classified expense components",
            condition=bool(expense_breakdown.get("reconciles", True)),
        ),
        _check(
            key="outstanding_payables", label="Outstanding supplier payables",
            left=opening_payables + payables_raised - payables_settled,
            right=outstanding_payables,
            formula="opening_payables + payables_raised - payables_settled = outstanding_payables",
        ),
        _check(
            key="guest_receivables", label="Guest receivable identity",
            left=gross_receivables - guest_credits + unassigned_receivables,
            right=net_receivable,
            formula="gross_receivables - guest_credit_balances + unassigned_receivables = net_receivable",
        ),
        _check(
            key="guest_receivable_control", label="Guest receivable control account",
            left=control_balance, right=folio_balances + no_folio,
            formula="ASSET.GUEST_RECEIVABLE control account = sum of folio balances + receivable lines with no folio",
        ),
        _check(
            key="settled_guest_is_zero", label="Settled guest balance",
            left=paid_amount, right=ZERO,
            formula="PAID settlement balance = 0.00; genuine credits are reported separately and never clamped",
        ),
        _check(
            key="guest_credits_visible", label="Guest credits remain visible",
            left=guest_credits, right=guest_credits,
            formula="guest_credit_balances >= 0.00 and is never netted against another guest's debt",
            condition=guest_credits >= ZERO and (credit_folios == 0 or guest_credits > ZERO),
        ),
    ]
    all_reconciled = all(check["reconciles"] for check in checks)
    exceptions = []
    if unposted_count:
        exceptions.append({
            "key": "unposted_procurement_payments",
            "count": unposted_count,
            "amount": money(unposted_amount),
            "severity": "REVIEW",
            "message": (
                "Procurement payment records await a Finance handover, approval or posted settlement. "
                "They are workflow/evidence exceptions and are NOT added to posted expenses or cash."
            ),
            "next_action": "Open Accounting → Expense handovers, prepare and submit each draft for independent approval and posting.",
        })
    if all_reconciled:
        status = "RECONCILED_WITH_EXCEPTIONS" if exceptions else "RECONCILED"
    else:
        status = "BROKEN"

    return {
        "status": status,
        "all_arithmetic_checks_reconciled": all_reconciled,
        "posted_only": True,
        "basis": "posted_immutable_financial_lines_and_guest_receivable_control_account",
        "start_date": summary.get("start_date"),
        "end_date": summary.get("end_date"),
        "currency": summary.get("currency"),
        "data_sources": [
            "FinancialTransaction (POSTED) / FinancialLine, inclusive business_date",
            "Expense + SupplierPaymentRequest workflow evidence (no unposted amount)",
            "PurchaseOrderPayment.accounting_posting_status (exception visibility only)",
            "ASSET.GUEST_RECEIVABLE control account, per-folio signed balances and settlement states",
        ],
        "duplicate_controls": [
            "FinancialTransaction.source_key and Expense.idempotency_key are unique posting guards",
            "One supplier payable per PurchaseOrder, one settlement per payable",
            "Goods receipt lines and stock movements use stable source keys; rejected/damaged quantities never enter stock",
            "Manual ingredient issues use one immutable batch source key and one source key per movement",
            "Explicit MANUAL stock consumption suppresses automatic POS recipe deductions for that ingredient",
            "PosOrder.charge_transaction is OneToOne; delivery posts one idempotent folio charge",
            "Procurement payment → Finance Expense is a DRAFT handover or mirrored settlement, never a second posting",
        ],
        "formulas": {
            "recognized_revenue": "Signed Revenue credits in the period, from posted lines only",
            "collections": "Tender debits from posted PAYMENT_COLLECTION transactions",
            "total_expenses": "Signed EXPENSE.* debits in the period, excluding ASSET.INVENTORY and payable settlements",
            "net_result": "net_income - total_expenses",
            "net_cash_movement": "collections - refunds - total_cash_disbursements",
            "outstanding_payables": "opening_payables + payables_raised - payables_settled",
            "net_receivable": "ASSET.GUEST_RECEIVABLE control-account balance through end_date",
        },
        "checks": checks,
        "exceptions": exceptions,
        "guest_receivables": {
            "gross_receivables": money(gross_receivables),
            "guest_credit_balances": money(guest_credits),
            "unassigned_receivables": money(unassigned_receivables),
            "net_receivable": money(net_receivable),
            "control_account_balance": money(control_balance),
            "settlement": settlement,
            "credit_folios": credit_folios,
            "reconciles": bool(receivables_reconciliation.get("reconciles", True)),
            "basis": receivables.get("basis"),
        },
        "supplier_payables": {
            "opening_balance": money(opening_payables),
            "raised": money(payables_raised),
            "settled": money(payables_settled),
            "outstanding": money(outstanding_payables),
            "cash_disbursed": money(disbursements),
        },
        "unposted_procurement_payments": {
            "count": unposted_count,
            "amount": money(unposted_amount),
            "added_to_posted_totals": False,
        },
        "legacy_payment_booking_fallback": {
            "used": False,
            "note": (
                "The legacy Payment + Booking projection is operational history only. "
                "It is never substituted for posted-ledger KPIs."
            ),
        },
    }


__all__ = ["build_reconciliation"]
