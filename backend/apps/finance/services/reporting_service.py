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

from django.db.models import Count, F, Q, Sum, Case, When, DecimalField, Value

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

# --------------------------------------------------------------------------
# Food & beverage outlets (batch 01, task 1)
#
# Every restaurant, bar, takeaway AND room-service sale credits the SAME
# ledger account, ``REVENUE.FOOD_BEVERAGE`` — the chart of accounts has no
# per-outlet revenue account and inventing one would rewrite history. The
# separation the owner asked for is therefore derived from evidence that
# already exists on the transaction: every POS-derived transaction (the charge,
# its tender collection, and a returned-goods credit) is posted with
# ``source_reference = PosOrder.reference``, and ``PosOrder.mode`` records the
# outlet the sale was made in.
#
# "Restaurants and bar" is the counter trade: dine-in, bar and takeaway.
# Room service is deliberately NOT part of it — it is food and drink billed to
# a room, which the owner reads as accommodation-side activity, and merging the
# two is exactly the confusion task 1 asks to remove.
# --------------------------------------------------------------------------
RESTAURANT_BAR_MODES = ("RESTAURANT", "BAR", "TAKEAWAY")
ROOM_SERVICE_MODES = ("ROOM_SERVICE",)

# Accounts whose presentation the hotel asked to remove from Accounting
# (batch 01, task 3). The ledger keeps every posting; only the named line is
# withdrawn from the statement, and the money stays inside the totals through
# an "Other" aggregate so a statement can never stop footing. Payroll
# deductions (LIABILITY.PAYROLL_TAX / PAYROLL_DEDUCTIONS / PAYROLL_PENSION) are
# legitimate employee obligations and are NOT in this set.
SUPPRESSED_PRESENTATION_ACCOUNTS = (
    accounting.TAX_PAYABLE,
    accounting.STATUTORY_TAX_PAYABLE,
    accounting.STATUTORY_TAX_ABSORBED,
)

#: Labels for the expense accounts the Total Expenses figure is built from.
EXPENSE_ACCOUNT_LABELS = {
    accounting.OPERATING_EXPENSE: "Operating, procurement, kitchen and departmental expenses",
    accounting.PAYROLL_EXPENSE: "Payroll expense (accrued salaries)",
    accounting.PAYROLL_PENSION_EXPENSE: "Payroll pension expense",
    accounting.CASH_OVER_SHORT: "Cash over / short",
}


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


def _pos_order_references(modes: tuple[str, ...]):
    """References of the POS orders made in ``modes`` (an indexed subquery).

    Every POS-derived ledger transaction — the charge, the tender collection
    and a returned-goods credit — carries ``source_reference =
    PosOrder.reference``.  Matching on that indexed column lets the outlet
    split run inside the existing conditional aggregate instead of adding a
    second ledger pass or a JSON metadata scan.
    """
    from apps.pos.models import PosOrder

    return PosOrder.objects.filter(mode__in=modes).values("reference")


def _transactions_touching(account_code: str, direction):
    """Transaction ids that post ``direction`` against ``account_code``.

    Used to tell a *booking* payment (debit tender / credit guest deposits)
    apart from a *POS* tender (debit tender / credit guest receivable) using
    the ledger alone, with no cross-application join.
    """
    return FinancialLine.objects.filter(
        account_code=account_code, direction=direction,
    ).values("transaction_id")


def _metrics() -> dict[str, Q]:
    """Reusable signed-line predicates for each distinct operational measure."""
    restaurant_bar_orders = _pos_order_references(RESTAURANT_BAR_MODES)
    room_service_orders = _pos_order_references(ROOM_SERVICE_MODES)
    deposit_credits = _transactions_touching(
        accounting.GUEST_DEPOSITS, FinancialLine.Direction.CREDIT,
    )
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
        "revenue_reversals": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code__startswith="REVENUE.",
        ),
        # ---- Revenue by source (task 1 / task 4) -------------------------
        # Each line is attributed to exactly one of these buckets by its
        # ledger account, so they sum back to recognized_revenue without
        # double counting.
        "accommodation_revenue": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.ACCOMMODATION_REVENUE,
        ),
        "accommodation_revenue_reversals": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.ACCOMMODATION_REVENUE,
        ),
        "food_beverage_revenue": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.FOOD_BEVERAGE_REVENUE,
        ),
        "food_beverage_revenue_reversals": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.FOOD_BEVERAGE_REVENUE,
        ),
        "other_operating_revenue": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=[accounting.OTHER_OPERATING_REVENUE, accounting.SERVICE_FEE_REVENUE],
        ),
        "other_operating_revenue_reversals": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=[accounting.OTHER_OPERATING_REVENUE, accounting.SERVICE_FEE_REVENUE],
        ),
        # ---- Restaurants and bar (task 1) --------------------------------
        # Counter trade only (dine-in, bar, takeaway). Room service is a
        # separate measure so the two are never conflated.
        "restaurant_bar_revenue": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.FOOD_BEVERAGE_REVENUE,
            transaction__source_reference__in=restaurant_bar_orders,
        ),
        "restaurant_bar_revenue_reversals": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.FOOD_BEVERAGE_REVENUE,
            transaction__source_reference__in=restaurant_bar_orders,
        ),
        "restaurant_bar_collections": Q(
            transaction__type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
            transaction__source_reference__in=restaurant_bar_orders,
        ),
        "room_service_revenue": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.FOOD_BEVERAGE_REVENUE,
            transaction__source_reference__in=room_service_orders,
        ),
        "room_service_revenue_reversals": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code=accounting.FOOD_BEVERAGE_REVENUE,
            transaction__source_reference__in=room_service_orders,
        ),
        "room_service_collections": Q(
            transaction__type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
            transaction__source_reference__in=room_service_orders,
        ),
        # ---- Booking money received (task 4) -----------------------------
        # A booking payment debits a tender account and credits GUEST
        # DEPOSITS; a POS tender credits GUEST RECEIVABLE instead. That is the
        # ledger's own discriminator, so this is a CASH COLLECTION measure and
        # never adds to recognized revenue.
        "booking_payments_received": Q(
            transaction__type=FinancialTransaction.Type.PAYMENT_COLLECTION,
            direction=FinancialLine.Direction.DEBIT,
            account_code__in=TENDER_ASSET_ACCOUNTS,
            transaction_id__in=deposit_credits,
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
        "discount_reversals": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code__startswith="CONTRA_REVENUE.",
        ),
        "operating_expense_reversals": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code=accounting.OPERATING_EXPENSE,
        ),
        "payroll_expense_reversals": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code__in=[accounting.PAYROLL_EXPENSE, accounting.PAYROLL_PENSION_EXPENSE],
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
        # ---- Total expenses (task 2) -------------------------------------
        # Every EXPENSE.* account, whatever transaction type raised it, less
        # the credits that reverse one. Deliberately account-driven rather
        # than type-driven so no posted cost can fall outside the total:
        #   * operating, procurement, kitchen and other departmental costs all
        #     debit EXPENSE.OPERATING once, when the expense is POSTED;
        #   * payroll debits EXPENSE.PAYROLL / EXPENSE.PAYROLL_PENSION;
        #   * cash over/short and absorbed statutory tax are included too.
        # Drafts, submitted-but-unapproved and rejected expenses never reach
        # the ledger, so they are excluded automatically. Paying a supplier
        # later debits ACCOUNTS PAYABLE, not an expense account, so an
        # on-account cost can never be counted twice. Stock purchases debit
        # ASSET.INVENTORY (an asset) and are reported separately.
        "total_expenses_recognized": Q(
            direction=FinancialLine.Direction.DEBIT,
            account_code__startswith="EXPENSE.",
        ),
        "total_expense_reversals": Q(
            direction=FinancialLine.Direction.CREDIT,
            account_code__startswith="EXPENSE.",
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


def _presentable_label(account_code: str) -> str:
    """Readable fallback for an account that has no curated label."""
    tail = account_code.split(".", 1)[-1]
    return tail.replace("_", " ").capitalize()


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


def _expense_detail_breakdown(*, start_date: date, end_date: date, currency: str) -> dict:
    """Departmental / category detail behind EXPENSE.OPERATING (task 2).

    Only POSTED expenses are read, and they are dated by the business date of
    the ledger transaction they created — the same basis the headline figure
    uses — so the detail reconciles to the ledger instead of drifting against
    it.  Drafts, submitted, approved-but-unposted, rejected and voided
    expenses have no posting and are therefore absent by construction.

    Inventory-class expenses debit ``ASSET.INVENTORY`` (a stock asset, not a
    cost) and are reported on their own line so a procurement purchase is
    never silently added to Total Expenses.
    """
    from ..models import Expense

    # ONE grouped query. The grouping key is small and bounded (accounting
    # class × department × category × "came from a purchase order"), so the
    # department, category, procurement and inventory views are all folded
    # out of the same result set rather than costing a query each.
    rows = (
        Expense.objects.filter(
            status=Expense.Status.POSTED,
            financial_transaction__status=FinancialTransaction.Status.POSTED,
            financial_transaction__currency=currency,
            financial_transaction__business_date__gte=start_date,
            financial_transaction__business_date__lte=end_date,
        )
        .values("accounting_class", "department", "category")
        .annotate(
            total=Sum("amount", default=ZERO),
            expenses=Count("pk"),
            from_purchase_order=Sum(
                Case(
                    When(purchase_order__isnull=False, then=F("amount")),
                    default=Value(ZERO),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                default=ZERO,
            ),
            purchase_order_expenses=Count("pk", filter=Q(purchase_order__isnull=False)),
        )
        .order_by()
    )

    departments: dict[str, list] = {}
    categories: dict[str, list] = {}
    procurement_amount, procurement_count = ZERO, 0
    inventory_amount, inventory_count = ZERO, 0
    for row in rows:
        amount = _amount(row["total"])
        count = row["expenses"] or 0
        if row["accounting_class"] == Expense.AccountingClass.INVENTORY:
            inventory_amount += amount
            inventory_count += count
            continue
        procurement_amount += _amount(row["from_purchase_order"])
        procurement_count += row["purchase_order_expenses"] or 0
        for bucket, key, blank in (
            (departments, (row["department"] or "").strip(), "Unassigned department"),
            (categories, (row["category"] or "").strip(), "Uncategorised"),
        ):
            label = key or blank
            current = bucket.setdefault(label, [ZERO, 0])
            current[0] += amount
            current[1] += count

    def _sorted(bucket):
        return [
            {"label": label, "amount": money(value), "expenses": count}
            for label, (value, count) in sorted(
                bucket.items(), key=lambda item: (-item[1][0], item[0])
            )
        ]

    return {
        "by_department": _sorted(departments),
        "by_category": _sorted(categories),
        "purchase_order_expenses": {
            "amount": money(procurement_amount), "expenses": procurement_count,
        },
        "inventory_class_expenses": {
            "amount": money(inventory_amount), "expenses": inventory_count,
        },
    }


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
        lines.filter(account_code__startswith="REVENUE.")
        .values("account_code")
        .annotate(amount=Sum(Case(
            When(direction=FinancialLine.Direction.DEBIT, then=-F("amount")),
            default=F("amount"), output_field=DecimalField(max_digits=14, decimal_places=2),
        )), transactions=Count("transaction_id", distinct=True))
        .order_by("account_code")
    )
    # One extra grouped aggregate: the expense detail that Total Expenses has
    # to reconcile against (task 2). Debits are positive, reversing credits
    # negative, so the rows sum to the headline figure by construction.
    by_expense_account = list(
        lines.filter(account_code__startswith="EXPENSE.")
        .values("account_code")
        .annotate(amount=Sum(Case(
            When(direction=FinancialLine.Direction.CREDIT, then=-F("amount")),
            default=F("amount"), output_field=DecimalField(max_digits=14, decimal_places=2),
        )), transactions=Count("transaction_id", distinct=True))
        .order_by("account_code")
    )
    totals_payload = _serialize_metrics(totals, metrics, include_transaction_counts=True)
    serialized_days = []
    for row in by_day:
        row_payload = _serialize_metrics(row, metrics, include_transaction_counts=False)
        row_payload["recognized_revenue"]["amount"] = money(
            _amount(row.get("recognized_revenue")) - _amount(row.get("revenue_reversals")))
        collections = _amount(row.get("collections"))
        refunds = _amount(row.get("refunds"))
        revenue = _amount(row.get("recognized_revenue")) - _amount(row.get("revenue_reversals"))
        operating_expenses = _amount(row.get("operating_expenses")) - _amount(row.get("operating_expense_reversals"))
        payroll_expenses = _amount(row.get("payroll_expenses")) - _amount(row.get("payroll_expense_reversals"))
        discounts = _amount(row.get("discounts")) - _amount(row.get("discount_reversals"))
        row_payload["operating_expenses"]["amount"] = money(operating_expenses)
        row_payload["payroll_expenses"]["amount"] = money(payroll_expenses)
        row_payload["discounts"]["amount"] = money(discounts)
        day_total_expenses = _amount(row.get("total_expenses_recognized")) - _amount(
            row.get("total_expense_reversals"))
        for key in (
            "restaurant_bar_revenue", "room_service_revenue", "food_beverage_revenue",
            "accommodation_revenue", "other_operating_revenue",
        ):
            row_payload[key]["amount"] = money(
                _amount(row.get(key)) - _amount(row.get(f"{key}_reversals")))
        payroll_payments = _amount(row.get("payroll_payments"))
        payroll_payment_reversals = _amount(row.get("payroll_payment_reversals"))
        serialized_days.append(
            {
                "date": row["transaction__business_date"].isoformat(),
                **row_payload,
                "net_collections": money(collections - refunds),
                "net_payroll_payments": money(payroll_payments - payroll_payment_reversals),
                "gross_operating_result": money(revenue - operating_expenses - payroll_expenses - discounts),
                "total_expenses": money(day_total_expenses),
                "net_result": money(revenue - discounts - day_total_expenses),
            }
        )

    collections = _amount(totals.get("collections"))
    refunds = _amount(totals.get("refunds"))
    revenue = _amount(totals.get("recognized_revenue")) - _amount(totals.get("revenue_reversals"))
    totals_payload["recognized_revenue"]["amount"] = money(revenue)
    operating_expenses = _amount(totals.get("operating_expenses")) - _amount(totals.get("operating_expense_reversals"))
    totals_payload["operating_expenses"]["amount"] = money(operating_expenses)
    payroll_expenses = _amount(totals.get("payroll_expenses")) - _amount(totals.get("payroll_expense_reversals"))
    totals_payload["payroll_expenses"]["amount"] = money(payroll_expenses)
    discounts = _amount(totals.get("discounts")) - _amount(totals.get("discount_reversals"))
    totals_payload["discounts"]["amount"] = money(discounts)
    payroll_payments_raw = _amount(totals.get("payroll_payments"))
    payroll_payments = payroll_payments_raw
    payroll_payment_reversals = _amount(totals.get("payroll_payment_reversals"))
    cash_paid_out = _amount(totals.get("cash_paid_out"))
    net_payroll_payments = payroll_payments - payroll_payment_reversals

    def _net(metric: str) -> Decimal:
        """Debit-side measure less the credits that reverse it."""
        return _amount(totals.get(metric)) - _amount(totals.get(f"{metric}_reversals"))

    # ---- Task 1: restaurants and bar, kept apart from room service -------
    restaurant_bar_revenue = _net("restaurant_bar_revenue")
    room_service_revenue = _net("room_service_revenue")
    food_beverage_revenue = _net("food_beverage_revenue")
    accommodation_revenue = _net("accommodation_revenue")
    other_operating_revenue = _net("other_operating_revenue")
    # Food and drink posted without a matching POS order reference (legacy or
    # manually journalled). Surfaced instead of being quietly folded into the
    # restaurant figure, so the three buckets always sum to F&B revenue.
    unclassified_fb_revenue = food_beverage_revenue - restaurant_bar_revenue - room_service_revenue
    restaurant_bar_collections = _amount(totals.get("restaurant_bar_collections"))
    room_service_collections = _amount(totals.get("room_service_collections"))
    for key, value in (
        ("restaurant_bar_revenue", restaurant_bar_revenue),
        ("room_service_revenue", room_service_revenue),
        ("food_beverage_revenue", food_beverage_revenue),
        ("accommodation_revenue", accommodation_revenue),
        ("other_operating_revenue", other_operating_revenue),
    ):
        totals_payload[key]["amount"] = money(value)

    # ---- Task 2: total expenses -----------------------------------------
    total_expenses = _amount(totals.get("total_expenses_recognized")) - _amount(
        totals.get("total_expense_reversals"))
    expense_rows = []
    for row in by_expense_account:
        code = row["account_code"]
        expense_rows.append(
            {
                "account_code": code,
                "label": EXPENSE_ACCOUNT_LABELS.get(code, _presentable_label(code)),
                "amount": money(_amount(row.get("amount"))),
                "transactions": row.get("transactions") or 0,
                "presented": code not in SUPPRESSED_PRESENTATION_ACCOUNTS,
            }
        )
    components_total = sum((_amount(row["amount"]) for row in expense_rows), ZERO)
    expense_detail = _expense_detail_breakdown(
        start_date=start_date, end_date=end_date, currency=currency,
    )
    expense_breakdown = {
        "by_account": expense_rows,
        **expense_detail,
        "reconciliation": {
            "ledger_total": money(total_expenses),
            "components_total": money(components_total),
            "difference": money(total_expenses - components_total),
            "reconciles": total_expenses == components_total,
        },
    }

    # ---- Task 4: profit and loss ----------------------------------------
    # Income is EARNED revenue only. Booking payments are shown underneath as
    # a collection, never added to income, because a payment settles a charge
    # that income has already recognised (or creates a deposit liability for a
    # charge not yet recognised).
    booking_payments_received = _amount(totals.get("booking_payments_received"))
    net_result = revenue - discounts - total_expenses
    profit_and_loss = {
        "basis": "accrual_posted_ledger",
        "income": [
            {"key": "accommodation_revenue", "label": "Accommodation earned (rooms)",
             "amount": money(accommodation_revenue)},
            {"key": "restaurant_bar_revenue", "label": "Restaurants and bar",
             "amount": money(restaurant_bar_revenue)},
            {"key": "room_service_revenue", "label": "Room service (food and drink billed to rooms)",
             "amount": money(room_service_revenue)},
            {"key": "other_operating_revenue", "label": "Other operating income and service fees",
             "amount": money(other_operating_revenue)},
        ] + ([
            {"key": "unclassified_food_beverage_revenue",
             "label": "Food and beverage not linked to a POS outlet",
             "amount": money(unclassified_fb_revenue)},
        ] if unclassified_fb_revenue else []),
        "total_income": money(revenue),
        "deductions": [
            {"key": "discounts", "label": "Discounts allowed", "amount": money(discounts)},
        ],
        "net_income": money(revenue - discounts),
        "expenses": [row for row in expense_rows if row["presented"]] + (
            [{"account_code": "", "label": "Other expenses", "transactions": 0, "presented": True,
              "amount": money(sum((_amount(row["amount"]) for row in expense_rows
                                   if not row["presented"]), ZERO))}]
            if any(not row["presented"] for row in expense_rows) else []
        ),
        "total_expenses": money(total_expenses),
        "net_result": money(net_result),
        # Cash movements shown BELOW the result line so they can never be read
        # as income. Each one is money moving, not value being earned.
        "collections_memorandum": [
            {"key": "booking_payments_received",
             "label": "Booking payments received (cash collected, not revenue)",
             "amount": money(booking_payments_received)},
            {"key": "restaurant_bar_collections",
             "label": "Restaurants and bar collections",
             "amount": money(restaurant_bar_collections)},
            {"key": "collections", "label": "All collections", "amount": money(collections)},
            {"key": "refunds", "label": "Refunds paid out", "amount": money(refunds)},
            {"key": "net_collections", "label": "Net collections",
             "amount": money(collections - refunds)},
        ],
    }
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
            "recognized_revenue": "Credits less reversal debits to REVENUE.* in the period (earned, not collected).",
            "collections": "Debit postings to cash/bank/card clearing from PAYMENT_COLLECTION (money received).",
            "refunds": "Credit postings to tender assets from REFUND transactions (money returned).",
            "operating_expenses": "Debit postings to EXPENSE.OPERATING from EXPENSE transactions (recognized, not necessarily paid).",
            "cash_paid_out": "Credit postings to cash on hand from EXPENSE transactions (actual cash leaving the drawer).",
            "gross_operating_result": "Net recognized revenue less discounts, operating expenses and accrued payroll (signed posted lines).",
            "net_cash_movement": (
                "Collections less refunds less cash actually disbursed (supplier payments, operating costs "
                "paid, net salaries, other disbursements) in the period. A movement, not a bank balance."
            ),
            "restaurant_bar_collections": (
                "Money actually taken at the restaurant, bar and takeaway counters: tender debits on "
                "PAYMENT_COLLECTION transactions whose POS order was raised in those outlets. A collection, "
                "not earned revenue, and it excludes room service."
            ),
            "restaurant_bar_revenue": (
                "REVENUE.FOOD_BEVERAGE credits less return debits for restaurant, bar and takeaway orders. "
                "Room service is reported separately and accommodation is never included."
            ),
            "room_service_revenue": "REVENUE.FOOD_BEVERAGE for ROOM_SERVICE orders (food and drink billed to a room).",
            "accommodation_revenue": "REVENUE.ACCOMMODATION credits less reversal debits (nights earned).",
            "total_expenses": (
                "Debits less reversing credits across every EXPENSE.* account: operating, procurement, "
                "kitchen and other departmental costs, payroll, pension, cash over/short. Only POSTED "
                "evidence; drafts, unapproved submissions and unpaid proposals are excluded. Settling a "
                "payable debits ACCOUNTS PAYABLE, not an expense, so nothing is counted twice. Stock "
                "purchases debit ASSET.INVENTORY and are excluded as an asset."
            ),
            "booking_payments_received": (
                "Tender debits on booking payment collections (the leg that credits guest deposits). "
                "A CASH COLLECTION, deliberately excluded from income so accommodation is never counted twice."
            ),
            "profit_and_loss": (
                "Accrual result: earned income less discounts less total expenses. Collections, deposits and "
                "refunds are listed below the result line as cash movements, never as income."
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
        "gross_operating_result": money(revenue - operating_expenses - payroll_expenses - discounts),
        "net_cash_movement": money(net_cash_movement),
        # ---- Batch 01 additive measures ---------------------------------
        "total_expenses": {"amount": money(total_expenses),
                           "transactions": totals.get("total_expenses_recognized_transactions", 0)},
        "expense_breakdown": expense_breakdown,
        "net_booking_payments": money(booking_payments_received - refunds),
        "unclassified_food_beverage_revenue": {"amount": money(unclassified_fb_revenue)},
        "net_restaurant_bar_collections": money(restaurant_bar_collections),
        "net_result": money(net_result),
        "profit_and_loss": profit_and_loss,
        # Supplier payments, payables and every other disbursement. Additive keys
        # only: the existing contract above is unchanged.
        "supplier_payments": payments,
        "by_department": by_department,
        "by_day": serialized_days,
    }


MEASURE_LABELS = (
    ("guest_charges", "Guest charges"),
    ("recognized_revenue", "Total revenue earned"),
    ("accommodation_revenue", "Accommodation earned (rooms)"),
    ("restaurant_bar_collections", "Restaurants and Bar Collections"),
    ("restaurant_bar_revenue", "Restaurants and bar revenue earned"),
    ("room_service_revenue", "Room service revenue earned"),
    ("other_operating_revenue", "Other operating income"),
    ("collections", "Collections"),
    ("booking_payments_received", "Booking payments received (cash)"),
    ("refunds", "Refunds"),
    ("net_collections", "Net collections"),
    ("discounts", "Discounts"),
    ("total_expenses", "Total expenses"),
    ("operating_expenses", "Operating expenses"),
    ("payroll_expenses", "Payroll expenses"),
    ("inventory_acquisitions", "Inventory acquisitions"),
    ("cash_paid_out", "Cash paid-out"),
    ("payroll_payments", "Payroll payments"),
    ("payroll_payment_reversals", "Payroll payment reversals"),
    ("net_payroll_payments", "Net salary paid"),
    ("actual_payments_made", "Actual payments made"),
    ("gross_operating_result", "Gross operating result"),
    ("net_result", "Net result (revenue less discounts and total expenses)"),
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


def _display_amount(amount: str, currency: str = "") -> str:
    """Thousands-separated 2-dp display for printed documents (§9).

    Presentation only: the value, its precision and the API payload are
    unchanged. ``currency`` prefixes the ISO code (``NGN 1,234.56``) rather
    than the ₦ glyph, which the PDF's core Helvetica font cannot render and
    would otherwise print as a black box.
    """
    try:
        rendered = f"{Decimal(str(amount)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(amount)
    return f"{currency} {rendered}".strip() if currency else rendered


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
    unit = report.get("currency") or "NGN"
    money_format = f'#,##0.00" {unit}";(#,##0.00" {unit}");0.00" {unit}"'
    sheet.append(["Measure", "Amount", "Ledger transactions"])
    for key, label in MEASURE_LABELS:
        row_index = sheet.max_row + 1
        amount = _metric_amount_number(report, key)
        sheet.append([label, amount if amount is not None else _metric_amount(report, key), _metric_count(report, key)])
        sheet.cell(row=row_index, column=2).number_format = money_format
    sheet.append([])
    sheet.append(["Revenue account", "Amount", "Share of recognized revenue %", "Transactions"])
    for row in report.get("by_department") or []:
        row_index = sheet.max_row + 1
        amount = _metric_amount_number({"amount": row.get("amount")}, "amount")
        sheet.append([row.get("label"), amount, row.get("percent_of_recognized_revenue"), row.get("transactions")])
        sheet.cell(row=row_index, column=2).number_format = money_format
    for column, width in (("A", 52), ("B", 20), ("C", 28), ("D", 18)):
        sheet.column_dimensions[column].width = width

    def _money_row(target, values, money_columns=(2,)):
        index = target.max_row + 1
        target.append(values)
        for column in money_columns:
            target.cell(row=index, column=column).number_format = money_format

    # ---- Profit and loss worksheet (task 4) -------------------------------
    profit_and_loss = report.get("profit_and_loss") or {}
    pnl = book.create_sheet("Profit and loss")
    pnl.append(["J-ONE HOTEL & LODGE — Profit and loss"])
    pnl.append(["Period", report.get("start_date"), report.get("end_date")])
    pnl.append(["Basis", profit_and_loss.get("basis") or "accrual_posted_ledger"])
    pnl.append([])
    pnl.append(["Income", f"Amount ({unit})"])
    for row in profit_and_loss.get("income") or []:
        _money_row(pnl, [row.get("label"), _metric_amount_number(row, "amount")])
    _money_row(pnl, ["Total income", _metric_amount_number(profit_and_loss, "total_income")])
    pnl.append([])
    for row in profit_and_loss.get("deductions") or []:
        _money_row(pnl, [row.get("label"), _metric_amount_number(row, "amount")])
    _money_row(pnl, ["Net income after discounts", _metric_amount_number(profit_and_loss, "net_income")])
    pnl.append([])
    pnl.append(["Expenses", f"Amount ({unit})"])
    for row in profit_and_loss.get("expenses") or []:
        _money_row(pnl, [row.get("label"), _metric_amount_number(row, "amount")])
    _money_row(pnl, ["Total expenses", _metric_amount_number(profit_and_loss, "total_expenses")])
    pnl.append([])
    _money_row(pnl, ["Net result", _metric_amount_number(profit_and_loss, "net_result")])
    pnl.append([])
    pnl.append(["Cash collected in the period (NOT income)", f"Amount ({unit})"])
    for row in profit_and_loss.get("collections_memorandum") or []:
        _money_row(pnl, [row.get("label"), _metric_amount_number(row, "amount")])
    for column, width in (("A", 58), ("B", 22)):
        pnl.column_dimensions[column].width = width

    # ---- Expense detail worksheet (task 2) --------------------------------
    breakdown = report.get("expense_breakdown") or {}
    expenses = book.create_sheet("Expenses")
    expenses.append(["J-ONE HOTEL & LODGE — Total expenses"])
    expenses.append(["Period", report.get("start_date"), report.get("end_date")])
    expenses.append([])
    expenses.append(["Ledger account", f"Amount ({unit})", "Transactions"])
    for row in breakdown.get("by_account") or []:
        _money_row(expenses, [row.get("label"), _metric_amount_number(row, "amount"), row.get("transactions")])
    _money_row(expenses, ["Total expenses", _metric_amount_number(report, "total_expenses")])
    expenses.append([])
    expenses.append(["Department", f"Amount ({unit})", "Expenses"])
    for row in breakdown.get("by_department") or []:
        _money_row(expenses, [row.get("label"), _metric_amount_number(row, "amount"), row.get("expenses")])
    expenses.append([])
    expenses.append(["Category", f"Amount ({unit})", "Expenses"])
    for row in breakdown.get("by_category") or []:
        _money_row(expenses, [row.get("label"), _metric_amount_number(row, "amount"), row.get("expenses")])
    expenses.append([])
    _money_row(expenses, [
        "Of which raised from a purchase order",
        _metric_amount_number(breakdown.get("purchase_order_expenses") or {}, "amount"),
        (breakdown.get("purchase_order_expenses") or {}).get("expenses"),
    ])
    _money_row(expenses, [
        "Stock purchases capitalised to inventory (NOT an expense)",
        _metric_amount_number(breakdown.get("inventory_class_expenses") or {}, "amount"),
        (breakdown.get("inventory_class_expenses") or {}).get("expenses"),
    ])
    reconciliation = breakdown.get("reconciliation") or {}
    expenses.append([])
    expenses.append(["Reconciliation", "Ledger total", "Sum of accounts", "Difference", "Reconciles"])
    expenses.append([
        "EXPENSE.* debits less reversing credits",
        reconciliation.get("ledger_total"), reconciliation.get("components_total"),
        reconciliation.get("difference"), "YES" if reconciliation.get("reconciles") else "NO",
    ])
    for column, width in (("A", 58), ("B", 22), ("C", 20), ("D", 16), ("E", 12)):
        expenses.column_dimensions[column].width = width

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
    unit = report.get("currency") or "NGN"

    def _table(header, body, widths):
        """A readable table that never renders as a bare header row."""
        rows = [header] + (body or [["No activity in this period", *([""] * (len(header) - 1))]])
        table = Table(rows, colWidths=widths, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#373435")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return table

    elements.append(_table(
        ["Measure", f"Amount ({unit})", "Transactions"],
        [[label, _display_amount(_metric_amount(report, key), unit),
          "" if _metric_count(report, key) is None else str(_metric_count(report, key))]
         for key, label in MEASURE_LABELS],
        [250, 150, 90],
    ))

    profit_and_loss = report.get("profit_and_loss") or {}
    if profit_and_loss:
        pnl_rows = [[row.get("label"), _display_amount(row.get("amount"), unit)]
                    for row in profit_and_loss.get("income") or []]
        pnl_rows.append(["Total income", _display_amount(profit_and_loss.get("total_income"), unit)])
        pnl_rows += [[row.get("label"), _display_amount(row.get("amount"), unit)]
                     for row in profit_and_loss.get("deductions") or []]
        pnl_rows.append(["Net income after discounts",
                         _display_amount(profit_and_loss.get("net_income"), unit)])
        pnl_rows += [[row.get("label"), _display_amount(row.get("amount"), unit)]
                     for row in profit_and_loss.get("expenses") or []]
        pnl_rows.append(["Total expenses", _display_amount(profit_and_loss.get("total_expenses"), unit)])
        pnl_rows.append(["Net result", _display_amount(profit_and_loss.get("net_result"), unit)])
        elements += [
            Spacer(1, 16),
            Paragraph("Profit and loss", styles["Heading2"]),
            _table(["Line", f"Amount ({unit})"], pnl_rows, [330, 160]),
            Spacer(1, 10),
            Paragraph(
                "Cash collected below is money received, not income. It is listed "
                "separately so a collection can never be read as revenue earned.",
                styles["Normal"],
            ),
            Spacer(1, 6),
            _table(
                ["Collection", f"Amount ({unit})"],
                [[row.get("label"), _display_amount(row.get("amount"), unit)]
                 for row in profit_and_loss.get("collections_memorandum") or []],
                [330, 160],
            ),
        ]

    breakdown = report.get("expense_breakdown") or {}
    if breakdown:
        reconciliation = breakdown.get("reconciliation") or {}
        elements += [
            Spacer(1, 16),
            Paragraph("Total expenses — detail", styles["Heading2"]),
            _table(
                ["Ledger account", f"Amount ({unit})", "Transactions"],
                [[row.get("label"), _display_amount(row.get("amount"), unit), str(row.get("transactions") or 0)]
                 for row in breakdown.get("by_account") or []],
                [250, 150, 90],
            ),
            Spacer(1, 10),
            _table(
                ["Department", f"Amount ({unit})", "Expenses"],
                [[row.get("label"), _display_amount(row.get("amount"), unit), str(row.get("expenses") or 0)]
                 for row in breakdown.get("by_department") or []],
                [250, 150, 90],
            ),
            Spacer(1, 8),
            Paragraph(
                "Reconciliation: ledger total {ledger}; sum of the accounts above {components}; "
                "difference {difference} ({state}).".format(
                    ledger=_display_amount(reconciliation.get("ledger_total"), unit),
                    components=_display_amount(reconciliation.get("components_total"), unit),
                    difference=_display_amount(reconciliation.get("difference"), unit),
                    state="reconciles" if reconciliation.get("reconciles") else "DOES NOT RECONCILE",
                ),
                styles["Normal"],
            ),
        ]

    document.build(elements)
    return output.getvalue()
