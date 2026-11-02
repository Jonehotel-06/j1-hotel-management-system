"""Ledger-only rebuild preview reports; never mix currencies or fabricate balances."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum

from ..models import ChartAccount, FinancialLine, FinancialTransaction
from . import accounting

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value):
    return str(Decimal(value or ZERO).quantize(CENT))


def _kind(code, known):
    account = known.get(code)
    return account.kind if account else code.split(".", 1)[0]


def _base(currency="NGN"):
    return FinancialLine.objects.filter(transaction__status=FinancialTransaction.Status.POSTED,
                                        transaction__currency=currency)


def account_rows(*, start, end, currency="NGN"):
    """Opening, period movement and closing per existing code in two queries."""
    known = {a.code: a for a in ChartAccount.objects.all()}
    records = (_base(currency).filter(transaction__business_date__lte=end)
        .values("account_code")
        .annotate(
            opening_debit=Sum("amount", filter=Q(direction="DEBIT", transaction__business_date__lt=start), default=ZERO),
            opening_credit=Sum("amount", filter=Q(direction="CREDIT", transaction__business_date__lt=start), default=ZERO),
            debit=Sum("amount", filter=Q(direction="DEBIT", transaction__business_date__gte=start), default=ZERO),
            credit=Sum("amount", filter=Q(direction="CREDIT", transaction__business_date__gte=start), default=ZERO),
        ).order_by("account_code"))
    rows = []
    for item in records:
        code = item["account_code"]
        opening = Decimal(item["opening_debit"]) - Decimal(item["opening_credit"])
        debit, credit = Decimal(item["debit"]), Decimal(item["credit"])
        account = known.get(code)
        rows.append({"code": code, "label": account.label if account else code,
                     "kind": _kind(code, known), "mapped": account is not None,
                     "opening": opening, "debit": debit, "credit": credit,
                     "closing": opening + debit - credit})
    return rows


def _warnings(rows):
    unmapped = sorted(r["code"] for r in rows if not r["mapped"])
    result = ["PREVIEW ONLY: historical migration and source reconciliation have not been certified."]
    if unmapped:
        result.append("Unmapped ledger codes require review: " + ", ".join(unmapped))
    return result


def trial_balance(*, start, end, currency="NGN"):
    rows = account_rows(start=start, end=end, currency=currency)
    records = [{"code": r["code"], "account": r["label"], "kind": r["kind"],
                "opening_debit": money(max(r["opening"], ZERO)),
                "opening_credit": money(max(-r["opening"], ZERO)),
                "period_debit": money(r["debit"]), "period_credit": money(r["credit"]),
                "closing_debit": money(max(r["closing"], ZERO)),
                "closing_credit": money(max(-r["closing"], ZERO)), "mapped": r["mapped"]}
               for r in rows]
    totals = {key: money(sum((Decimal(r[key]) for r in records), ZERO)) for key in
              ("opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit")}
    return {"report": "trial-balance", "currency": currency, "start": start.isoformat(),
            "end": end.isoformat(), "records": records, "totals": totals,
            "balanced": totals["closing_debit"] == totals["closing_credit"], "warnings": _warnings(rows)}


def income_statement(*, start, end, currency="NGN"):
    rows = account_rows(start=start, end=end, currency=currency)
    records = []
    revenues = contra = expenses = ZERO
    for row in rows:
        movement = row["debit"] - row["credit"]
        if row["kind"] == "REVENUE":
            value = -movement
            revenues += value
        elif row["kind"] == "CONTRA_REVENUE":
            value = movement
            contra += value
        elif row["kind"] == "EXPENSE":
            value = movement
            expenses += value
        else:
            continue
        records.append({"code": row["code"], "account": row["label"], "kind": row["kind"],
                        "amount": money(value), "mapped": row["mapped"]})
    return {"report": "income-statement", "currency": currency, "start": start.isoformat(),
            "end": end.isoformat(), "records": records,
            "totals": {"earned_revenue": money(revenues), "contra_revenue": money(contra),
                       "net_revenue": money(revenues-contra), "expenses": money(expenses),
                       "net_profit": money(revenues-contra-expenses)}, "warnings": _warnings(rows)}


def balance_sheet(*, start, end, currency="NGN"):
    rows = account_rows(start=start, end=end, currency=currency)
    records = []
    totals = defaultdict(lambda: ZERO)
    earnings = ZERO
    for row in rows:
        kind, raw = row["kind"], row["closing"]
        if kind in {"ASSET", "LIABILITY", "EQUITY"}:
            value = raw if kind == "ASSET" else -raw
            totals[kind] += value
            records.append({"code": row["code"], "account": row["label"], "kind": kind,
                            "amount": money(value), "mapped": row["mapped"]})
        elif kind in {"REVENUE", "CONTRA_REVENUE", "EXPENSE"}:
            # Unclosed cumulative income is part of equity. Debit-negative in
            # each income/expense account is exactly its contribution.
            earnings += -raw
    records.append({"code": "UN_CLOSED_EARNINGS", "account": "Unclosed cumulative earnings",
                    "kind": "EQUITY", "amount": money(earnings), "mapped": True})
    equity = totals["EQUITY"] + earnings
    difference = totals["ASSET"] - totals["LIABILITY"] - equity
    return {"report": "balance-sheet", "currency": currency, "as_of": end.isoformat(),
            "records": records, "totals": {"assets": money(totals["ASSET"]),
            "liabilities": money(totals["LIABILITY"]), "equity_including_earnings": money(equity),
            "difference": money(difference)}, "balanced": difference == ZERO, "warnings": _warnings(rows)}


def overview(*, start, end, currency="NGN"):
    rows = account_rows(start=start, end=end, currency=currency)
    pnl = income_statement(start=start, end=end, currency=currency)
    positions = {r["code"]: r["closing"] for r in rows}
    liability_other = sum((-r["closing"] for r in rows if r["kind"] == "LIABILITY"
                           and r["code"] not in {accounting.GUEST_DEPOSITS, accounting.ACCOUNTS_PAYABLE}), ZERO)
    accounts = [{"code": r["code"], "account": r["label"], "amount": money(r["closing"])}
                for r in rows if r["code"].startswith("ASSET.BANK")]
    # Accounting-audit repair: the signed AR control balance is NOT a
    # receivable. When guests prepay, it is credit-normal and used to render a
    # negative "Guest receivables" card. The subledger position separates
    # genuine debit receivables, guest credit/advances and unapplied deposits.
    from .guest_receivables_service import guest_position

    guest = guest_position(as_of=end, currency=currency)
    payable_closing = positions.get(accounting.ACCOUNTS_PAYABLE, ZERO)
    return {"currency": currency, "start": start.isoformat(), "end": end.isoformat(),
            "earned_revenue": pnl["totals"]["net_revenue"], "expenses": pnl["totals"]["expenses"],
            "net_profit": pnl["totals"]["net_profit"],
            "cash_balance": money(positions.get(accounting.CASH_ON_HAND, ZERO)),
            "bank_balances": accounts,
            "bank_total": money(sum((Decimal(row["amount"]) for row in accounts), ZERO)),
            "pos_clearing": money(positions.get(accounting.CARD_CLEARING, ZERO)),
            "paystack_clearing": money(positions.get("ASSET.PAYSTACK_SETTLEMENT", ZERO)),
            # Non-negative, per-folio debit balances only (plus unassigned AR
            # debits). Credit balances are never presented as negative AR.
            "guest_receivables_control": guest["gross_guest_receivables"],
            # Genuine money held for guests: folio credits + unapplied deposits.
            "guest_advances_control": guest["guest_advances"],
            "guest_credit_balances": guest["guest_credit_balances"],
            "unapplied_guest_deposits": guest["unapplied_guest_deposits"],
            "unpaid_bills": guest["unpaid_bills"]["amount"],
            "partially_paid": guest["partially_paid"]["amount"],
            "fully_paid_folios": guest["fully_paid"]["folios"],
            "refunded_total": guest["refunded_total"],
            "net_receivable_control": guest["net_receivable_control"],
            "guest_balances": guest,
            # A debit-normal payable control is an overpayment, not a negative
            # liability: clamp and surface it separately.
            "supplier_payables_control": money(max(-payable_closing, ZERO)),
            "supplier_prepayments": money(max(payable_closing, ZERO)),
            "other_obligations": money(liability_other), "warnings": _warnings(rows) + [
                "Bank clearing may include historic Paystack collections; it is not a verified bank balance.",
                "Guest balances are classified from posted folio lines; the signed control balance is audit evidence only.",
                "Supplier control balances can include unallocated or legacy differences; reconcile subledgers."]}


def cash_bank(*, start, end, currency="NGN"):
    rows = account_rows(start=start, end=end, currency=currency)
    codes = {accounting.CASH_ON_HAND, accounting.CARD_CLEARING, "ASSET.PAYSTACK_SETTLEMENT"}
    records = [{"code": r["code"], "account": r["label"], "opening": money(r["opening"]),
                "increases": money(r["debit"]), "decreases": money(r["credit"]),
                "closing": money(r["closing"])} for r in rows
               if r["code"] in codes or r["code"].startswith("ASSET.BANK")]
    return {"report": "cash-bank", "currency": currency, "start": start.isoformat(), "end": end.isoformat(),
            "records": records, "warnings": _warnings(rows) + [
                "Clearing accounts are shown separately from physical cash; historic bank clearing may include Paystack."]}


def general_ledger(*, code, start, end, currency="NGN", page=1, page_size=50):
    base = _base(currency).filter(account_code=code)
    before = base.filter(transaction__business_date__lt=start).aggregate(
        debit=Sum("amount", filter=Q(direction="DEBIT"), default=ZERO),
        credit=Sum("amount", filter=Q(direction="CREDIT"), default=ZERO))
    opening = Decimal(before["debit"]) - Decimal(before["credit"])
    period = base.filter(transaction__business_date__gte=start, transaction__business_date__lte=end)
    totals = period.aggregate(debit=Sum("amount", filter=Q(direction="DEBIT"), default=ZERO),
                              credit=Sum("amount", filter=Q(direction="CREDIT"), default=ZERO))
    count = period.count()
    ordered = period.select_related("transaction", "folio", "transaction__initiated_by").order_by(
        "transaction__business_date", "transaction_id", "pk")
    offset = (page-1)*page_size
    # For later pages compute the running balance at the page boundary without
    # loading all earlier rows into application memory.
    prior = {"debit": ZERO, "credit": ZERO}
    if offset >= count and offset:
        prior = totals
    elif offset:
        boundary = list(ordered.values("transaction__business_date", "transaction_id", "pk")[offset-1:offset])[0]
        day = boundary["transaction__business_date"]
        tx_id, line_id = boundary["transaction_id"], boundary["pk"]
        predicate = (Q(transaction__business_date__lt=day)
                     | Q(transaction__business_date=day, transaction_id__lt=tx_id)
                     | Q(transaction__business_date=day, transaction_id=tx_id, pk__lte=line_id))
        prior = period.filter(predicate).aggregate(
            debit=Sum("amount", filter=Q(direction="DEBIT"), default=ZERO),
            credit=Sum("amount", filter=Q(direction="CREDIT"), default=ZERO))
    running = opening + Decimal(prior["debit"]) - Decimal(prior["credit"])
    records = []
    for line in ordered[offset:offset+page_size]:
        running += line.amount if line.direction == "DEBIT" else -line.amount
        tx = line.transaction
        records.append({"date": tx.business_date.isoformat(), "reference": tx.reference,
                        "source_reference": tx.source_reference, "external_reference": tx.external_reference,
                        "description": line.description or tx.narrative, "folio": getattr(line.folio, "reference", ""),
                        "debit": money(line.amount if line.direction == "DEBIT" else ZERO),
                        "credit": money(line.amount if line.direction == "CREDIT" else ZERO),
                        "running_debit_balance": money(running)})
    known = ChartAccount.objects.filter(code=code).first()
    return {"report": "general-ledger", "account": code, "label": known.label if known else code,
            "currency": currency, "start": start.isoformat(), "end": end.isoformat(),
            "opening_debit_balance": money(opening), "period_debit": money(totals["debit"]),
            "period_credit": money(totals["credit"]),
            "closing_debit_balance": money(opening + Decimal(totals["debit"]) - Decimal(totals["credit"])),
            "records": records, "page": page, "page_size": page_size, "count": count,
            "warnings": ["Debit-positive balances are signed; negative means credit balance.",
                         "PREVIEW ONLY: historical reconciliation has not been certified."]}
