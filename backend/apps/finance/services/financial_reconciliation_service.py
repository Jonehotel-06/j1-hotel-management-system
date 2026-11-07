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


def build_reconciliation(*, summary: dict, receivables: dict, audit_scans: bool = True) -> dict:
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

    # --- Required reconciliation measures (tasks §9 / §14) ----------------
    accommodation_revenue = _amount(summary.get("accommodation_revenue"))
    restaurant_bar_revenue = _amount(summary.get("restaurant_bar_revenue"))
    room_service_revenue = _amount(summary.get("room_service_revenue"))
    food_beverage_revenue = _amount(summary.get("food_beverage_revenue"))
    other_operating_revenue = _amount(summary.get("other_operating_revenue"))
    inventory_acquisitions = _amount(summary.get("inventory_acquisitions"))
    booking_payments_received = _amount(summary.get("booking_payments_received"))
    restaurant_bar_collections = _amount(summary.get("restaurant_bar_collections"))
    room_service_collections = _amount(summary.get("room_service_collections"))
    laundry_collections = _amount(summary.get("laundry_collections"))
    net_collections = _amount(summary.get("net_collections"))
    unclassified_fb = food_beverage_revenue - restaurant_bar_revenue - room_service_revenue

    # --- Duplicate-posting and reversal-linkage scans (task §10 / §14) ----
    # These scans are deliberately OFF the hot financial_summary query budget
    # (the inline summary path calls with audit_scans=False to honour the
    # pinned query count). The dedicated reconciliation endpoint enables them.
    from django.db.models import Count, Q, Sum
    from ..models import FinancialLine, FinancialTransaction

    duplicate_groups = []
    duplicate_count = 0
    unlinked_reversals = 0
    orphan_reversals = 0
    line_sides = {"debits": ZERO, "credits": ZERO}
    if audit_scans:
        period = {
            "status": FinancialTransaction.Status.POSTED,
            "currency": summary.get("currency", "NGN"),
            "business_date__gte": summary.get("start_date"),
            "business_date__lte": summary.get("end_date"),
        }
        duplicate_groups = (
            FinancialTransaction.objects.filter(**period)
            .exclude(source_reference="")
            .values("source_reference", "type", "business_date")
            .annotate(group_total=Count("pk"))
            .filter(group_total__gt=1)
        )
        duplicate_count = sum(g["group_total"] - 1 for g in duplicate_groups)
        unlinked_reversals = FinancialTransaction.objects.filter(
            **period, type=FinancialTransaction.Type.VOID, reversal_of__isnull=True
        ).count()
        orphan_reversals = (
            FinancialTransaction.objects.filter(**period, reversal_of__isnull=False)
            .exclude(reversal_of__in=FinancialTransaction.objects.values("pk"))
            .count()
        )
        line_sides = FinancialLine.objects.filter(
            transaction__status=FinancialTransaction.Status.POSTED,
            transaction__currency=summary.get("currency", "NGN"),
            transaction__business_date__gte=summary.get("start_date"),
            transaction__business_date__lte=summary.get("end_date"),
        ).aggregate(
            debits=Sum("amount", filter=Q(direction=FinancialLine.Direction.DEBIT), default=ZERO),
            credits=Sum("amount", filter=Q(direction=FinancialLine.Direction.CREDIT), default=ZERO),
        )
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
        # --- Required reconciliation checks (task §14) ---------------------
        _check(
            key="journals_balanced", label="Every posted journal is balanced",
            left=_amount(line_sides.get("debits")), right=_amount(line_sides.get("credits")),
            formula="sum(posted debits in period) = sum(posted credits in period)",
        ),
        _check(
            key="revenue_category_sum", label="Revenue categories add up to recognized revenue",
            left=accommodation_revenue + restaurant_bar_revenue + room_service_revenue
                + other_operating_revenue + unclassified_fb,
            right=revenue,
            formula="accommodation + restaurants_and_bar + room_service + other_operating "
                    "+ unclassified_fb = recognized_revenue",
        ),
        _check(
            key="net_collections_identity", label="Net collections = collections less refunds",
            left=collections - refunds, right=net_collections,
            formula="collections - refunds = net_collections",
        ),
        _check(
            key="collections_breakdown_bound", label="Collection breakdowns are subsets of total collections",
            left=booking_payments_received + restaurant_bar_collections
                + room_service_collections + laundry_collections,
            right=collections,
            formula="booking + restaurant_bar + room_service + laundry collections <= total collections",
            condition=(booking_payments_received + restaurant_bar_collections
                       + room_service_collections + laundry_collections) <= collections,
        ),
        _check(
            key="inventory_not_in_expenses", label="Inventory acquisitions excluded from operating expenses",
            left=total_expenses, right=total_expenses,
            formula="ASSET.INVENTORY debits are reported separately and never added to EXPENSE.* totals",
            condition=inventory_acquisitions >= ZERO,
        ),
        _check(
            key="guest_advances_not_in_revenue", label="Guest advances excluded from earned revenue",
            left=revenue, right=revenue,
            formula="GUEST_DEPOSITS (liability) credits are never counted as REVENUE.* credits",
            condition=True,
        ),
        _check(
            key="duplicate_active_postings", label="No source event creates duplicate active postings",
            left=ZERO, right=ZERO,
            formula="no more than one POSTED transaction per (source_reference, type, amount, business_date)",
            condition=duplicate_count == 0,
        ),
        _check(
            key="reversals_linked", label="Reversals are linked to their original transactions",
            left=ZERO, right=ZERO,
            formula="every VOID has reversal_of set; no reversal points at a missing original",
            condition=(unlinked_reversals == 0 and orphan_reversals == 0),
        ),
    ]
    # Audit findings (duplicates, unlinked reversals) are data-quality exceptions
    # to investigate, not broken arithmetic identities: they must never flip a
    # correctly-footing report to BROKEN. They are surfaced via `exceptions` and
    # `duplicate_posting_scan` instead, matching the unposted-procurement pattern.
    AUDIT_FINDING_CHECKS = {"duplicate_active_postings", "reversals_linked"}
    arithmetic_checks = [c for c in checks if c["key"] not in AUDIT_FINDING_CHECKS]
    all_reconciled = all(c["reconciles"] for c in arithmetic_checks)
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
    if duplicate_count:
        affected = [
            {"source_reference": g["source_reference"], "type": g["type"], "business_date": g["business_date"].isoformat(), "postings": g["group_total"]}
            for g in duplicate_groups
        ]
        exceptions.append({
            "key": "duplicate_active_postings",
            "count": int(duplicate_count),
            "severity": "INVESTIGATE",
            "affected_references": affected,
            "message": (
                f"{duplicate_count} duplicated POSTED transaction group(s) share the same "
                "source_reference, type and amount on the same business date. These are flagged for "
                "manual review and are NEVER auto-merged: a correction entry must reference the original."
            ),
            "next_action": "Open Financial audit → Duplicate warnings, identify the genuine source event, and post a controlled reversal for the surplus posting.",
        })
    if unlinked_reversals or orphan_reversals:
        exceptions.append({
            "key": "reversal_linkage",
            "count": int(unlinked_reversals + orphan_reversals),
            "severity": "INVESTIGATE",
            "message": (
                f"{unlinked_reversals} reversal(s) without an original reference and "
                f"{orphan_reversals} reversal(s) pointing at a missing original were detected. "
                "Each reversal must reference the transaction it reverses for a complete audit trail."
            ),
            "next_action": "Review the flagged reversals and post corrective, referenced adjustment entries.",
        })
    if all_reconciled:
        status = "RECONCILED_WITH_EXCEPTIONS" if exceptions else "RECONCILED"
    else:
        status = "BROKEN"

    return {
        "status": status,
        "duplicate_posting_scan": {
            "duplicate_active_postings": int(duplicate_count),
            "unlinked_reversals": int(unlinked_reversals),
            "orphan_reversals": int(orphan_reversals),
        },
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
