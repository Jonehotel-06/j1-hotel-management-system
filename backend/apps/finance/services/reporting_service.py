"""Bounded, ledger-authoritative financial report projections.

These reports intentionally use posted ``FinancialTransaction``/``FinancialLine``
evidence rather than legacy booking or payment summaries.  That keeps guest
charges, earned revenue, collections, refunds, expenses, and physical cash
paid-outs as distinct measures rather than collapsing them into a misleading
single "revenue" number.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.core.utils import money

from ..models import FinancialLine, FinancialTransaction
from . import accounting

ZERO = Decimal("0.00")
CHARGE_TYPES = (
    FinancialTransaction.Type.ACCOMMODATION_CHARGE,
    FinancialTransaction.Type.POS_CHARGE,
    FinancialTransaction.Type.OTHER_CHARGE,
)
TENDER_ASSET_ACCOUNTS = (
    accounting.CASH_ON_HAND,
    accounting.BANK_CLEARING,
    accounting.CARD_CLEARING,
)


def _validate_range(start_date: date | None, end_date: date | None) -> tuple[date, date]:
    if start_date is None or end_date is None:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"detail": "start and end are required (YYYY-MM-DD)."})
    if end_date < start_date:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"end": "Must be on or after start."})
    if (end_date - start_date).days > 366:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"detail": "Date range cannot exceed 366 days."})
    return start_date, end_date


def _validate_currency(currency: str | None) -> str:
    value = str(currency or "NGN").upper()
    if len(value) != 3 or not value.isalpha():
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"currency": "Use a three-letter ISO currency code."})
    return value


def _metrics() -> dict[str, Q]:
    """Reusable signed-line predicates for each distinct operational measure."""
    return {
        "guest_charges": Q(
            transaction__type__in=CHARGE_TYPES,
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.ACCOUNTS_RECEIVABLE,
        ),
        "recognized_revenue": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code__startswith="REVENUE.",
        ),
        "tax_accrued": Q(
            transaction__type__in=CHARGE_TYPES,
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.TAX_PAYABLE,
        ),
        "collections": Q(
            transaction__type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
        ),
        "refunds": Q(
            transaction__type=FinancialTransaction.Type.REFUND,
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
        ),
        "discounts": Q(
            transaction__type=FinancialTransaction.Type.DISCOUNT,
            direction=FinancialLine.Direction.DEBIT,
            account_code__startswith="CONTRA_REVENUE.",
        ),
        "operating_expenses": Q(
            transaction__type=FinancialTransaction.Type.EXPENSE,
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.OPERATING_EXPENSE,
        ),
        "payroll_expenses": Q(
            transaction__type=FinancialTransaction.Type.PAYROLL_ACCRUAL,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=[accounting.PAYROLL_EXPENSE, accounting.PAYROLL_PENSION_EXPENSE],
        ),
        "inventory_acquisitions": Q(
            transaction__type=FinancialTransaction.Type.EXPENSE,
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.INVENTORY_ASSET,
        ),
        "cash_paid_out": Q(
            transaction__type=FinancialTransaction.Type.EXPENSE,
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.CASH_ON_HAND,
        ),
    }


def _aggregation_kwargs(metrics: dict[str, Q], *, include_transaction_counts: bool) -> dict:
    values = {
        metric: Sum("amount", filter=predicate, default=ZERO)
        for metric, predicate in metrics.items()
    }
    if include_transaction_counts:
        values.update(
            {
                f"{metric}_transactions": Count(
                    "transaction_id",
                    filter=predicate,
                    distinct=True,
                )
                for metric, predicate in metrics.items()
            }
        )
    return values


def _amount(value) -> Decimal:
    return Decimal(value or ZERO).quantize(Decimal("0.01"))


def _serialize_metrics(values: dict, metrics: dict[str, Q], *, include_transaction_counts: bool) -> dict:
    payload = {}
    for metric in metrics:
        item = {"amount": money(_amount(values.get(metric)))}
        if include_transaction_counts:
            item["transactions"] = values.get(f"{metric}_transactions", 0)
        payload[metric] = item
    return payload


def financial_summary(*, start_date: date | None, end_date: date | None, currency: str = "NGN") -> dict:
    """Summarize immutable posted ledger evidence by hotel business date.

    The bounded query plan is deliberately stable: one indexed transaction
    count, one conditional aggregate across ledger lines, and one conditional
    aggregate grouped by business date.  No legacy mutable projection is used.
    ``gross_operating_result`` is recognized revenue less non-payroll operating
    expense and accrued payroll; discounts, refunds, inventory acquisitions,
    and cash paid-outs stay visible as distinct measures.
    """
    start_date, end_date = _validate_range(start_date, end_date)
    currency = _validate_currency(currency)
    transactions = FinancialTransaction.objects.filter(
        status=FinancialTransaction.Status.POSTED,
        currency=currency,
        business_date__gte=start_date,
        business_date__lte=end_date,
    )
    lines = FinancialLine.objects.filter(
        transaction__status=FinancialTransaction.Status.POSTED,
        transaction__currency=currency,
        transaction__business_date__gte=start_date,
        transaction__business_date__lte=end_date,
    )
    metrics = _metrics()

    totals = lines.aggregate(**_aggregation_kwargs(metrics, include_transaction_counts=True))
    by_day = list(
        lines.values("transaction__business_date")
        .annotate(**_aggregation_kwargs(metrics, include_transaction_counts=False))
        .order_by("transaction__business_date")
    )
    totals_payload = _serialize_metrics(totals, metrics, include_transaction_counts=True)
    serialized_days = []
    for row in by_day:
        row_payload = _serialize_metrics(row, metrics, include_transaction_counts=False)
        collections = _amount(row.get("collections"))
        refunds = _amount(row.get("refunds"))
        revenue = _amount(row.get("recognized_revenue"))
        operating_expenses = _amount(row.get("operating_expenses"))
        payroll_expenses = _amount(row.get("payroll_expenses"))
        serialized_days.append(
            {
                "date": row["transaction__business_date"].isoformat(),
                **row_payload,
                "net_collections": money(collections - refunds),
                "gross_operating_result": money(revenue - operating_expenses - payroll_expenses),
            }
        )

    collections = _amount(totals.get("collections"))
    refunds = _amount(totals.get("refunds"))
    revenue = _amount(totals.get("recognized_revenue"))
    operating_expenses = _amount(totals.get("operating_expenses"))
    payroll_expenses = _amount(totals.get("payroll_expenses"))
    return {
        "basis": "posted_immutable_ledger_business_date",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "currency": currency,
        "ledger_transactions": transactions.count(),
        **totals_payload,
        "net_collections": money(collections - refunds),
        "gross_operating_result": money(revenue - operating_expenses - payroll_expenses),
        "by_day": serialized_days,
    }
