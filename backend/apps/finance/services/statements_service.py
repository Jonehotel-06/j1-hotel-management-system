"""Balance sheet and cash-flow statement from the posted ledger.

Both statements are read-only projections of the same balanced, immutable
journal lines every other finance report reads — no separate statement
ledger exists, so the statements reconcile to the ledger by construction:

* Opening balances are not a special table: they are balanced journals
  posted (normally dated before go-live) against EQUITY.OPENING_BALANCE.
* The balance sheet is every posted line up to the as-of date, grouped into
  assets / liabilities / equity, with net revenue-less-expenses shown as
  current earnings inside equity. Because every journal balances, total
  assets must equal liabilities + equity + current earnings; the check is
  computed and reported, never assumed.
* The cash-flow statement explains the change in the three cash accounts
  (cash on hand, bank clearing, card clearing) between two dates. Each
  transaction touching cash is classified once from its counter-accounts:
  equity counter-accounts are financing, everything else in this chart is
  operating (the chart has no fixed-asset or investment accounts, so no
  investing figure is fabricated). Drawer float openings are internal and
  excluded. The sections must tie to the actual change in cash balances —
  again computed and reported.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from apps.core.utils import hotel_today, money

from ..models import FinancialLine, FinancialTransaction
from . import accounting
from .journal_service import CHART_OF_ACCOUNTS

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
CASH_ACCOUNTS = (accounting.CASH_ON_HAND, accounting.BANK_CLEARING, accounting.CARD_CLEARING)
LABELS = dict(CHART_OF_ACCOUNTS) | {
    accounting.TAX_PAYABLE: "Statutory tax payable (historical guest tax)",
    accounting.PAYROLL_PENSION_EXPENSE: "Payroll pension expense (historical)",
    accounting.PAYROLL_DEDUCTIONS_PAYABLE: "Payroll deductions payable",
    accounting.PAYROLL_TAX_PAYABLE: "Payroll tax payable (historical)",
    accounting.PAYROLL_PENSION_PAYABLE: "Payroll pension payable (historical)",
    accounting.CASH_FLOAT: "Cash float",
}

# --------------------------------------------------------------------------
# Presentation policy (batch 01, tasks 3 and 5)
#
# The owner asked for two things to disappear from the Accounting screens:
# the statutory tax payable figure, and the equity section. Neither request
# may touch the ledger — the accounts, their journals and their history stay
# exactly as they are, and every total below still includes them. What
# changes is only what the statement NAMES.
#
# A statement that drops a line but keeps its value in the total stops
# footing, which is its own audit defect. So each suppressed balance is
# carried into a neutral aggregate line instead ("Other liabilities"), and the
# equity section is replaced by NET ASSETS — assets less liabilities — which
# is arithmetically identical to total equity and is the standard way to
# present a position statement without itemising capital.
#
# Payroll deductions, payroll tax and pension payable are genuine employee
# obligations and are NOT suppressed.
# --------------------------------------------------------------------------
SUPPRESSED_LIABILITY_ACCOUNTS = (
    accounting.TAX_PAYABLE,
    accounting.STATUTORY_TAX_PAYABLE,
)
SUPPRESSED_EXPENSE_ACCOUNTS = (
    accounting.STATUTORY_TAX_ABSORBED,
)
OTHER_LIABILITIES_LABEL = "Other liabilities"
OTHER_EXPENSES_LABEL = "Other expenses"


def _dec(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _signed(account_code: str, debits: Decimal, credits: Decimal) -> Decimal:
    """Balance in the account's normal direction."""
    if account_code.startswith(("ASSET.", "EXPENSE.", "CONTRA_REVENUE.")):
        return _dec(debits - credits)
    return _dec(credits - debits)


def _balances(*, before=None, through=None):
    qs = FinancialLine.objects.filter(transaction__status=FinancialTransaction.Status.POSTED)
    if before is not None:
        qs = qs.filter(transaction__business_date__lt=before)
    if through is not None:
        qs = qs.filter(transaction__business_date__lte=through)
    rows = qs.values("account_code", "direction").annotate(total=Sum("amount"))
    result: dict[str, list[Decimal]] = {}
    for row in rows:
        pair = result.setdefault(row["account_code"], [ZERO, ZERO])
        if row["direction"] == FinancialLine.Direction.DEBIT:
            pair[0] += Decimal(row["total"] or 0)
        else:
            pair[1] += Decimal(row["total"] or 0)
    return result


def balance_sheet(*, as_of=None) -> dict:
    as_of = as_of or hotel_today()
    balances = _balances(through=as_of)

    def section(prefix, suppressed=(), aggregate_label=OTHER_LIABILITIES_LABEL):
        """Rows for a section, with suppressed accounts folded into one line.

        ``total`` always covers every account, suppressed or not, so the
        statement keeps balancing; the rows always sum to ``total``, so the
        statement keeps footing.
        """
        rows, total, withheld = [], ZERO, ZERO
        for code in sorted(balances):
            if not code.startswith(prefix):
                continue
            amount = _signed(code, *balances[code])
            total += amount
            if amount == ZERO:
                continue
            if code in suppressed:
                withheld += amount
                continue
            rows.append({"account_code": code, "label": LABELS.get(code, code), "amount": money(amount)})
        if withheld != ZERO:
            rows.append({"account_code": "", "label": aggregate_label, "amount": money(withheld)})
        return rows, total

    assets, total_assets = section("ASSET.")
    liabilities, total_liabilities = section("LIABILITY.", SUPPRESSED_LIABILITY_ACCOUNTS)
    # Equity is still computed — the balance check depends on it — but it is
    # no longer itemised for the reader (task 5).
    _equity_rows, total_equity = section("EQUITY.")

    earnings = ZERO
    earnings_rows = []
    suppressed_earnings: list[Decimal] = []
    for code in sorted(balances):
        if code.startswith(("REVENUE.", "EXPENSE.", "CONTRA_REVENUE.")):
            amount = _signed(code, *balances[code])
            if code.startswith("REVENUE."):
                contribution = amount
            else:
                contribution = -amount
            earnings += contribution
            if amount != ZERO and code not in SUPPRESSED_EXPENSE_ACCOUNTS:
                earnings_rows.append({
                    "account_code": code, "label": LABELS.get(code, code),
                    "amount": money(amount if code.startswith("REVENUE.") else -amount),
                })
            elif amount != ZERO:
                suppressed_earnings.append(-amount)
    if suppressed_earnings:
        earnings_rows.append({
            "account_code": "", "label": OTHER_EXPENSES_LABEL,
            "amount": money(sum(suppressed_earnings, ZERO)),
        })
    total_equity_with_earnings = total_equity + earnings
    liabilities_and_equity = total_liabilities + total_equity_with_earnings
    # Net assets is assets less liabilities. It equals total equity by
    # definition, which is what lets the equity section be withdrawn from the
    # presentation without the statement becoming false or unbalanced.
    net_assets = _dec(total_assets - total_liabilities)
    return {
        "as_of": as_of.isoformat(),
        "currency": "NGN",
        "assets": assets,
        "total_assets": money(total_assets),
        "liabilities": liabilities,
        "total_liabilities": money(total_liabilities),
        "net_assets": money(net_assets),
        "current_earnings": money(earnings),
        "current_earnings_basis": "All posted revenue less expenses and contra-revenue to date; not yet closed into retained earnings.",
        "earnings_lines": earnings_rows,
        # Integrity is still proved on every call, from the equity balances
        # that are no longer itemised.
        "balanced": total_assets == liabilities_and_equity,
        "difference": money(total_assets - liabilities_and_equity),
        "presentation": {
            "equity_section": "withdrawn",
            "equity_note": (
                "Owner capital, drawings and opening balances remain posted in the EQUITY.* accounts and "
                "are fully included in this statement's arithmetic. They are presented as NET ASSETS "
                "(total assets less total liabilities), which equals total equity, instead of being "
                "itemised. Nothing has been deleted from the ledger."
            ),
            "withheld_liability_accounts": list(SUPPRESSED_LIABILITY_ACCOUNTS),
            "withheld_expense_accounts": list(SUPPRESSED_EXPENSE_ACCOUNTS),
            "withheld_note": (
                "Statutory tax payable is no longer named on the statement. Any balance it still carries "
                "is reported inside 'Other liabilities' so the section continues to foot and the ledger "
                "history stays intact."
            ),
        },
        "opening_balances_basis": "Opening balances are posted as balanced journals against EQUITY.OPENING_BALANCE via Accounting → Post journal entry, normally dated before the period under review.",
    }


def cash_flow(*, start, end) -> dict:
    if end < start:
        start, end = end, start
    opening_balances = _balances(before=start)
    closing_balances = _balances(through=end)
    opening_cash = sum((_signed(c, *opening_balances.get(c, [ZERO, ZERO])) for c in CASH_ACCOUNTS), ZERO)
    closing_cash = sum((_signed(c, *closing_balances.get(c, [ZERO, ZERO])) for c in CASH_ACCOUNTS), ZERO)

    lines = (
        FinancialLine.objects.filter(
            transaction__status=FinancialTransaction.Status.POSTED,
            transaction__business_date__gte=start,
            transaction__business_date__lte=end,
        )
        .values("transaction_id", "transaction__type", "account_code", "direction")
        .annotate(total=Sum("amount"))
    )
    per_transaction: dict[int, dict] = {}
    for row in lines:
        entry = per_transaction.setdefault(row["transaction_id"], {
            "type": row["transaction__type"], "cash_delta": ZERO, "counters": set(),
        })
        amount = Decimal(row["total"] or 0)
        signed = amount if row["direction"] == FinancialLine.Direction.DEBIT else -amount
        if row["account_code"] in CASH_ACCOUNTS:
            entry["cash_delta"] += signed
        else:
            entry["counters"].add(row["account_code"])

    operating = financing = other = ZERO
    counts = {"operating": 0, "financing": 0, "other": 0}
    for entry in per_transaction.values():
        if entry["cash_delta"] == ZERO:
            continue
        if entry["type"] == FinancialTransaction.Type.CASH_OPEN:
            bucket = "other"  # drawer float is internal cash handling, disclosed separately below
        elif any(code.startswith("EQUITY.") for code in entry["counters"]):
            bucket = "financing"
        else:
            bucket = "operating"
        if bucket == "operating":
            operating += entry["cash_delta"]
        elif bucket == "financing":
            financing += entry["cash_delta"]
        else:
            other += entry["cash_delta"]
        counts[bucket] += 1

    net_change = closing_cash - opening_cash
    classified = operating + financing + other
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "currency": "NGN",
        "operating_activities": money(operating),
        "investing_activities": "0.00",
        "investing_note": "No fixed-asset or investment accounts exist in this ledger; inventory purchases are operating activities for a hotel.",
        "financing_activities": money(financing),
        "internal_float_and_other": money(other),
        "net_cash_change_classified": money(classified),
        "opening_cash": money(opening_cash),
        "closing_cash": money(closing_cash),
        "net_cash_change_per_balances": money(net_change),
        "reconciles": classified == net_change,
        "difference": money(net_change - classified),
        "transaction_counts": counts,
        "definitions": {
            "cash_accounts": "Cash on hand + bank clearing + card clearing, from posted ledger balances.",
            "operating": "Every cash movement whose counter-accounts are revenue, receivables, payables, expenses or payroll.",
            "financing": "Cash movements against owner capital, drawings or opening-balance equity.",
            "internal_float_and_other": "Cash-drawer float openings (internal), itemised so the statement still ties to balances.",
        },
    }
