"""Bounded, ledger-authoritative financial report projections.

These reports intentionally use posted ``FinancialTransaction``/``FinancialLine``
evidence rather than legacy booking or payment summaries.  That keeps guest
charges, earned revenue, collections, refunds, expenses, and physical cash
paid-outs as distinct measures rather than collapsing them into a misleading
single "revenue" number.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Count, F, Q, Sum

from apps.core.utils import money

from ..models import FinancialLine, FinancialTransaction
from . import accounting
from .payment_visibility import settlement_measure_filters, supplier_payment_visibility

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
        "statutory_tax_accrued": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.STATUTORY_TAX_PAYABLE,
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
        "payroll_payments": Q(
            transaction__type=FinancialTransaction.Type.PAYROLL_PAYMENT,
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.PAYROLL_PAYABLE,
        ),
        "payroll_payment_reversals": Q(
            transaction__type=FinancialTransaction.Type.PAYROLL_PAYMENT,
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.PAYROLL_PAYABLE,
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
        # Supplier-payment stages. These are conditional aggregates over the same
        # ledger lines the summary already aggregates, so the whole supplier block
        # costs no additional statement beyond the two bounded counts it makes for
        # its opening balance and approval counts.
        **settlement_measure_filters(),
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


def _goods_receipt_totals(*, start_date: date, end_date: date, currency: str) -> tuple[Decimal, int]:
    """Operational supplier stock received in the period (not posted money).

    Goods receipts are the inventory record a storekeeper creates when supplier
    goods arrive. Reporting their quantity × unit cost here makes that supplier
    cost visible in Accounting/Dashboard without pretending that a Finance
    invoice/expense has been approved or posted.
    """
    from apps.inventory.models import GoodsReceiptLine

    totals = GoodsReceiptLine.objects.filter(
        goods_receipt__received_on__gte=start_date,
        goods_receipt__received_on__lte=end_date,
        item__currency=currency,
    ).aggregate(
        value=Sum(F("quantity_received") * F("unit_cost"), default=ZERO),
        receipts=Count("goods_receipt", distinct=True),
    )
    return _amount(totals.get("value")), int(totals.get("receipts") or 0)


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
    by_revenue_account = list(
        lines.filter(
            direction=FinancialLine.Direction.CREDIT,
            account_code__startswith="REVENUE.",
        )
        .values("account_code")
        .annotate(amount=Sum("amount", default=ZERO), transactions=Count("transaction_id", distinct=True))
        .order_by("account_code")
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
        payroll_payments = _amount(row.get("payroll_payments"))
        payroll_payment_reversals = _amount(row.get("payroll_payment_reversals"))
        serialized_days.append(
            {
                "date": row["transaction__business_date"].isoformat(),
                **row_payload,
                "net_collections": money(collections - refunds),
                "net_payroll_payments": money(payroll_payments - payroll_payment_reversals),
                "gross_operating_result": money(revenue - operating_expenses - payroll_expenses),
            }
        )

    collections = _amount(totals.get("collections"))
    refunds = _amount(totals.get("refunds"))
    revenue = _amount(totals.get("recognized_revenue"))
    operating_expenses = _amount(totals.get("operating_expenses"))
    payroll_expenses = _amount(totals.get("payroll_expenses"))
    payroll_payments = _amount(totals.get("payroll_payments"))
    payroll_payment_reversals = _amount(totals.get("payroll_payment_reversals"))
    cash_paid_out = _amount(totals.get("cash_paid_out"))
    net_payroll_payments = payroll_payments - payroll_payment_reversals
    account_labels = {
        accounting.ACCOMMODATION_REVENUE: "Rooms and hotel bookings",
        accounting.FOOD_BEVERAGE_REVENUE: "Restaurant, bar and food sales",
        accounting.OTHER_OPERATING_REVENUE: "Other operating income",
        accounting.SERVICE_FEE_REVENUE: "Guest services and fees",
    }
    revenue_total = revenue if revenue else Decimal("0.00")
    by_department = []
    for row in by_revenue_account:
        amount = _amount(row.get("amount"))
        code = row["account_code"]
        share = (amount / revenue_total * Decimal("100.00")) if revenue_total else Decimal("0.00")
        by_department.append(
            {
                "account_code": code,
                "label": account_labels.get(code, code),
                "amount": money(amount),
                "transactions": row.get("transactions") or 0,
                "percent_of_recognized_revenue": str(share.quantize(Decimal("0.01"))),
            }
        )
    # Assembled from the aggregate above plus two bounded counts — never from a
    # second ledger pass.
    goods_receipts_value, goods_receipts_count = _goods_receipt_totals(
        start_date=start_date, end_date=end_date, currency=currency,
    )
    payments = supplier_payment_visibility(
        totals=totals, start_date=start_date, end_date=end_date, currency=currency,
        goods_receipts_value=goods_receipts_value, goods_receipts_count=goods_receipts_count,
    )
    # Net cash movement is defined once, from the same measures the rest of the
    # report exposes, so a reader can always reproduce it: money received less
    # money returned to guests less money actually disbursed. It is a MOVEMENT for
    # the period, unlike `outstanding_supplier_payables`, which is a balance.
    net_cash_movement = collections - refunds - _amount(payments["total_cash_disbursements"])
    return {
        "basis": "posted_immutable_ledger_business_date",
        "definitions": {
            "recognized_revenue": "Credit postings to REVENUE.* accounts in the period (earned, not necessarily collected).",
            "collections": "Debit postings to cash/bank/card clearing from PAYMENT_COLLECTION (money received).",
            "refunds": "Credit postings to tender assets from REFUND transactions (money returned).",
            "operating_expenses": "Debit postings to EXPENSE.OPERATING from EXPENSE transactions (recognized, not necessarily paid).",
            "cash_paid_out": "Credit postings to cash on hand from EXPENSE transactions (actual cash leaving the drawer).",
            "gross_operating_result": "Recognized revenue less operating expense and accrued payroll. Not statutory net profit.",
            "net_cash_movement": (
                "Collections less refunds less cash actually disbursed (supplier payments, operating costs "
                "paid, net salaries, other disbursements) in the period. A movement, not a bank balance."
            ),
        },
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "currency": currency,
        "ledger_transactions": transactions.count(),
        **totals_payload,
        "net_collections": money(collections - refunds),
        "net_payroll_payments": money(net_payroll_payments),
        "actual_payments_made": money(cash_paid_out + net_payroll_payments),
        "gross_operating_result": money(revenue - operating_expenses - payroll_expenses),
        "net_cash_movement": money(net_cash_movement),
        # Supplier payments, payables and every other disbursement. Additive keys
        # only: the existing contract above is unchanged.
        "supplier_payments": payments,
        "by_department": by_department,
        "by_day": serialized_days,
    }


MEASURE_LABELS = (
    ("guest_charges", "Guest charges"),
    ("recognized_revenue", "Recognized revenue"),
    ("tax_accrued", "Historical tax collected (pre-removal)"),
    ("statutory_tax_accrued", "Statutory tax accrued (not billed to guests)"),
    ("collections", "Collections"),
    ("refunds", "Refunds"),
    ("net_collections", "Net collections"),
    ("discounts", "Discounts"),
    ("operating_expenses", "Operating expenses"),
    ("inventory_acquisitions", "Inventory acquisitions"),
    ("cash_paid_out", "Cash paid-out"),
    ("payroll_payments", "Payroll payments"),
    ("payroll_payment_reversals", "Payroll payment reversals"),
    ("net_payroll_payments", "Net salary paid"),
    ("actual_payments_made", "Actual payments made"),
    ("gross_operating_result", "Gross operating result"),
)


def _metric_amount(report: dict, key: str) -> str:
    """Canonical STRING amount for a measure (undefined measures stay textual)."""
    value = report.get(key)
    if isinstance(value, dict):
        return str(value.get("amount") or "0.00")
    return str(value or "0.00")


def _metric_amount_number(report: dict, key: str):
    """The same amount as a Decimal, for spreadsheets that must sum the column.

    Falls back to the textual value when the measure is absent (for example COGS,
    which is deliberately reported as unavailable rather than as a zero).
    """
    value = report.get(key)
    if isinstance(value, dict):
        value = value.get("amount")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)


def _display_amount(amount: str) -> str:
    """Thousands-separated 2-dp display for printed documents (§9).

    Presentation only: the value, its precision and the API payload are unchanged.
    """
    try:
        return f"{Decimal(str(amount)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(amount)


def _metric_count(report: dict, key: str):
    value = report.get(key)
    if isinstance(value, dict):
        return value.get("transactions")
    return None


def summary_xlsx_bytes(report: dict) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Financial summary"
    sheet.append(["J-ONE HOTEL & LODGE — Ledger financial summary"])
    sheet.append(["Basis", report.get("basis") or ""])
    sheet.append(["Period", report.get("start_date"), report.get("end_date")])
    sheet.append(["Currency", report.get("currency")])
    sheet.append(["Posted ledger transactions", report.get("ledger_transactions")])
    sheet.append([])
    # Amounts are written as REAL numbers with a grouped display format, not as
    # pre-formatted strings: a text cell cannot be summed, and a hard-coded
    # "₦1,000.00" string would both lose precision and change the exported value.
    # The stored precision (2 decimal places) is untouched — only how Excel shows
    # the cell changes.
    money_format = f'#,##0.00" {report.get("currency") or "NGN"}"'
    sheet.append(["Measure", "Amount", "Ledger transactions"])
    for key, label in MEASURE_LABELS:
        row_index = sheet.max_row + 1
        amount = _metric_amount_number(report, key)
        sheet.append([label, amount if amount is not None else _metric_amount(report, key), _metric_count(report, key)])
        sheet.cell(row=row_index, column=2).number_format = money_format
    sheet.append([])
    sheet.append(["Department", "Amount", "Share of recognized revenue %", "Transactions"])
    for row in report.get("by_department") or []:
        row_index = sheet.max_row + 1
        amount = _metric_amount_number({"amount": row.get("amount")}, "amount")
        sheet.append([row.get("label"), amount, row.get("percent_of_recognized_revenue"), row.get("transactions")])
        sheet.cell(row=row_index, column=2).number_format = money_format
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def summary_pdf_bytes(report: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, title="J-ONE financial summary")
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("J-ONE HOTEL &amp; LODGE — Ledger financial summary", styles["Title"]),
        Paragraph(
            f"Period {report.get('start_date')} to {report.get('end_date')} · {report.get('currency')} · "
            f"{report.get('ledger_transactions') or 0} posted ledger transactions",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]
    rows = [["Measure", "Amount", "Transactions"]]
    for key, label in MEASURE_LABELS:
        count = _metric_count(report, key)
        rows.append([label, _display_amount(_metric_amount(report, key)), "" if count is None else str(count)])
    table = Table(rows, colWidths=[240, 120, 100])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#373435")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elements.append(table)
    document.build(elements)
    return output.getvalue()
