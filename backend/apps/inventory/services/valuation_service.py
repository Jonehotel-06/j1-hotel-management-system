"""Forward-looking product profitability and stock valuation projections.

This module answers "what is this stock worth, and what would it earn if it were
sold through the recipes that already consume it?" for the inventory screen.

It is a *projection*, never a posting:

* It reads the append-only :class:`~apps.inventory.models.StockMovement` ledger and
  the existing :class:`~apps.inventory.models.StockBalance` projection. It writes
  nothing, creates no journal entries, no supplier payments and no accounting
  balances.
* It never invents a quantity, a cost or a price. Every number is either read from
  a stored column or derived by an explicit, documented formula below. When the
  inputs do not exist the field is returned as ``None`` and the row is labelled
  ``NOT_SELLABLE`` / ``NO_COST_BASIS`` rather than guessed at.
* It is presentation-only, so it cannot alter calculations, stored amounts or any
  API contract used by the transactional endpoints.

Formulas
--------
``purchase_unit_cost`` — weighted-average of every goods-receipt movement for the
item at that location (``sum(qty * unit_cost) / sum(qty)`` over
``StockMovement.type == RECEIPT``). Received quantities, not ordered or invoiced
ones, carry the cost, so a partial receipt or a price change is handled correctly
and the historical purchase costs recorded on the movements are preserved. If no
receipt carries a cost the item's ``standard_cost`` is used and ``cost_basis``
reports which of the two applied.

``implied unit selling price`` — recipes bridge the stock item to a POS menu price.
For each active ``COMPONENTS`` recipe that consumes the item, the menu price is
split across its components in proportion to each component's cost (a standard
joint-cost allocation, so a recipe with a 60% cost share of gin carries 60% of the
menu price). The revenue attributed to the item is then divided by the number of
base units the recipe consumes per menu unit, giving revenue per *base unit*.
Because one base unit can be sold through several different dishes, the row
reports the *mean* of those per-recipe implied unit prices and discloses
``price_basis = "recipe_implied_average"`` together with ``recipe_count`` so the
reader can see how many onward uses the average is drawn from. Items with no
active recipe report no selling price, no expected revenue and no margin — they
are shown for valuation only.

``stock_value`` = ``current_stock * purchase_unit_cost``.
``expected_sales_revenue`` = ``current_stock * implied unit selling price``.
``expected_gross_profit`` = ``expected_sales_revenue - stock_value``.
``expected_gross_margin_percent`` = ``expected_gross_profit / expected_sales_revenue * 100``.

All arithmetic is :class:`decimal.Decimal` at the model's own precision; no float
maths is used and nothing is rounded before the final presentation step.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import DecimalField, F, Q, Sum

from apps.pos.models import MenuCategory

from ..models import GoodsReceiptLine, StockBalance, StockItem, StockMovement, StockRecipe

#: Quantisation used for the money/percentage columns returned by this projection.
MONEY_QUANTUM = Decimal("0.01")
PERCENT_QUANTUM = Decimal("0.01")
QUANTITY_QUANTUM = Decimal("0.0001")
ZERO = Decimal("0")

#: ISO-4217 codes whose minor unit is not two decimals. Only used to shape the
#: presentation fields; the stored precision of the underlying models is untouched.
CURRENCY_QUANTUM = {"NGN": "0.01", "USD": "0.01", "EUR": "0.01", "GBP": "0.01"}

#: Whitelisted sort keys. Anything else is rejected by the view so an attacker
#: cannot reach an unexpected attribute through ``?sort=``.
SORT_KEYS = (
    "sku", "name", "department", "location", "supplier", "purchase_unit_cost",
    "selling_price", "current_stock", "stock_value", "expected_sales_revenue",
    "expected_gross_profit", "expected_gross_margin_percent",
)

PROJECTION_LABEL = "PROJECTION"


def _quantize(value, quantum):
    if value is None:
        return None
    return value.quantize(Decimal(quantum), rounding=ROUND_HALF_UP)


def _divide(numerator, denominator):
    """Decimal division that returns ``None`` instead of raising on a zero divisor."""
    if numerator is None or denominator in (None, ZERO):
        return None
    return numerator / denominator


def _receipt_cost_basis(item_ids):
    """Weighted-average and most-recent receipt cost per (item, location).

    Two aggregate queries for the whole page (never one per row): a weighted
    average and a "latest receipt" lookup, both keyed on the movement ledger so
    the numbers stay tied to the append-only source of truth.
    """
    weighted = {}
    if not item_ids:
        return weighted, {}
    rows = (
        StockMovement.objects.filter(
            type=StockMovement.Type.RECEIPT, item_id__in=item_ids, unit_cost__isnull=False,
        )
        .values("item_id", "location_id")
        .annotate(
            # quantity_delta * unit_cost at 4+2 decimal places needs a wider
            # output field than either operand so the aggregate never truncates.
            cost_total=Sum(F("quantity_delta") * F("unit_cost"), output_field=DecimalField(max_digits=26, decimal_places=6)),
            quantity_total=Sum("quantity_delta", output_field=DecimalField(max_digits=26, decimal_places=6)),
        )
    )
    for row in rows:
        quantity_total = row["quantity_total"]
        if quantity_total and quantity_total > ZERO and row["cost_total"] is not None:
            weighted[(row["item_id"], row["location_id"])] = row["cost_total"] / quantity_total

    latest = {}
    latest_rows = (
        StockMovement.objects.filter(
            type=StockMovement.Type.RECEIPT, item_id__in=item_ids, unit_cost__isnull=False,
        )
        .order_by("item_id", "location_id", "-occurred_at", "-pk")
        .values("item_id", "location_id", "unit_cost", "occurred_at")
    )
    for row in latest_rows:
        latest.setdefault((row["item_id"], row["location_id"]), row["unit_cost"])
    return weighted, latest


def _supplier_basis(item_ids):
    """Most recent goods-receipt supplier per (item, location).

    Used only as a fallback when a stock item has no configured preferred
    supplier, so the supplier column reflects who actually delivered the stock.
    """
    suppliers = {}
    if not item_ids:
        return suppliers
    rows = (
        GoodsReceiptLine.objects.filter(item_id__in=item_ids, goods_receipt__supplier__isnull=False)
        .order_by("item_id", "goods_receipt__location_id", "-goods_receipt__received_on", "-pk")
        .select_related("goods_receipt__supplier", "goods_receipt__location")
        .values(
            "item_id", "goods_receipt__location_id", "goods_receipt__supplier_id",
            "goods_receipt__supplier__name", "goods_receipt__received_on",
        )
    )
    for row in rows:
        suppliers.setdefault((row["item_id"], row["goods_receipt__location_id"]), (
            row["goods_receipt__supplier_id"], row["goods_receipt__supplier__name"],
        ))
    return suppliers


def _recipe_basis(item_ids):
    """Per-item implied base-unit selling price and department labels.

    Walks the active component-tracked recipes that consume the scoped items once,
    allocating each menu price across its components by cost share.
    ``select_related``/``prefetch_related`` keep this to a constant number of
    queries regardless of how many rows the page holds.
    """
    if not item_ids:
        return {}, {}
    recipes = (
        StockRecipe.objects.filter(
            is_active=True, tracking_mode=StockRecipe.TrackingMode.COMPONENTS, lines__item_id__in=item_ids,
        )
        .select_related("menu_item", "menu_item__category", "location")
        .prefetch_related("lines__item", "lines__item__preferred_supplier")
        .distinct()
    )
    per_item = {}
    departments = {}
    for recipe in recipes:
        lines = list(recipe.lines.all())
        if not lines:
            continue
        component_costs = []
        recipe_cost = ZERO
        for line in lines:
            unit_cost = line.item.standard_cost or ZERO
            cost = (line.quantity_per_menu_unit or ZERO) * unit_cost
            component_costs.append((line.item_id, line.quantity_per_menu_unit or ZERO, cost))
            recipe_cost += cost
        menu_price = recipe.menu_item.base_price or ZERO
        area = recipe.menu_item.category.get_service_area_display() if recipe.menu_item.category_id else ""
        for item_id, quantity, cost in component_costs:
            if not quantity or quantity <= ZERO:
                continue
            if recipe_cost > ZERO:
                share = cost / recipe_cost
            else:
                # Zero-cost recipe (e.g. a complimentary pour): fall back to an
                # equal split so the row still reports a bounded implied price.
                share = Decimal(1) / Decimal(len(component_costs))
            implied = (menu_price * share) / quantity
            if item_id not in item_ids:
                continue
            if implied > ZERO:
                per_item.setdefault(item_id, []).append(implied)
            if area:
                departments.setdefault(item_id, set()).add(area)
    return per_item, departments


def product_profitability_projection(*, search="", location_id=None, department="", supplier_id=None,
                                     category="", low_stock_only=False, sellable=None, min_margin=None,
                                     item_ids=None):
    """Build the labelled projection rows. Returns a plain list of dicts."""
    balances = (
        StockBalance.objects.select_related("item", "item__preferred_supplier", "location")
        .filter(item__is_active=True)
        .order_by("item__sku", "location__name")
    )
    if location_id:
        balances = balances.filter(location_id=location_id)
    if item_ids is not None:
        balances = balances.filter(item_id__in=item_ids)
    if search:
        balances = balances.filter(Q(item__sku__icontains=search) | Q(item__name__icontains=search))
    if category:
        balances = balances.filter(item__category=category)
    if supplier_id:
        balances = balances.filter(item__preferred_supplier_id=supplier_id)
    balances = list(balances)

    scoped_item_ids = sorted({balance.item_id for balance in balances})
    weighted_cost, latest_cost = _receipt_cost_basis(scoped_item_ids)
    receipt_supplier = _supplier_basis(scoped_item_ids)
    implied_prices, departments = _recipe_basis(scoped_item_ids)

    rows = []
    for balance in balances:
        item = balance.item
        key = (item.pk, balance.location_id)
        average_cost = weighted_cost.get(key)
        if average_cost is not None:
            unit_cost, cost_basis = average_cost, "WEIGHTED_AVERAGE_RECEIPT"
        elif item.standard_cost and item.standard_cost > ZERO:
            unit_cost, cost_basis = item.standard_cost, "STANDARD_COST"
        else:
            unit_cost, cost_basis = None, "NO_COST_BASIS"

        prices = implied_prices.get(item.pk, ())
        selling_price = (sum(prices, ZERO) / Decimal(len(prices))) if prices else None
        price_basis = "RECIPE_IMPLIED_AVERAGE" if prices else "NO_ACTIVE_RECIPE"

        quantity = balance.quantity_on_hand or ZERO
        stock_value = _quantize(quantity * unit_cost, MONEY_QUANTUM) if unit_cost is not None else None
        revenue = _quantize(quantity * selling_price, MONEY_QUANTUM) if selling_price is not None else None
        profit = (revenue - stock_value) if (revenue is not None and stock_value is not None) else None
        margin = _divide(profit, revenue) if (profit is not None and revenue is not None and revenue > ZERO) else None
        margin_percent = _quantize(margin * Decimal(100), PERCENT_QUANTUM) if margin is not None else None

        if not prices:
            row_status = "NOT_SELLABLE"
        elif unit_cost is None:
            row_status = "NO_COST_BASIS"
        else:
            row_status = "OK"

        fallback_supplier = receipt_supplier.get(key)
        supplier_id_value = item.preferred_supplier_id or (fallback_supplier[0] if fallback_supplier else None)
        supplier_name = (
            item.preferred_supplier.name if item.preferred_supplier_id
            else (fallback_supplier[1] if fallback_supplier else None)
        )
        seen_departments = departments.get(item.pk)
        department_label = (
            " + ".join(sorted(seen_departments)) if seen_departments
            else (item.category or "Unassigned")
        )

        rows.append({
            "projection": PROJECTION_LABEL,
            "status": row_status,
            "item_id": item.pk,
            "sku": item.sku,
            "name": item.name,
            "category": item.category,
            "department": department_label,
            "base_unit": item.base_unit,
            "currency": item.currency,
            "location_id": balance.location_id,
            "location_code": balance.location.code,
            "location_name": balance.location.name,
            "supplier_id": supplier_id_value,
            "supplier_name": supplier_name,
            "purchase_unit_cost": _quantize(unit_cost, MONEY_QUANTUM) if unit_cost is not None else None,
            "cost_basis": cost_basis,
            "standard_cost": _quantize(item.standard_cost, MONEY_QUANTUM) if item.standard_cost is not None else None,
            "latest_receipt_cost": _quantize(latest_cost.get(key), MONEY_QUANTUM) if latest_cost.get(key) is not None else None,
            "selling_price": _quantize(selling_price, MONEY_QUANTUM) if selling_price is not None else None,
            "price_basis": price_basis,
            "recipe_count": len(prices),
            "current_stock": _quantize(quantity, QUANTITY_QUANTUM),
            "reorder_level": _quantize(item.reorder_level or ZERO, QUANTITY_QUANTUM),
            "needs_reorder": (balance.quantity_on_hand or ZERO) <= (item.reorder_level or ZERO),
            "stock_value": stock_value,
            "expected_sales_revenue": revenue,
            "expected_gross_profit": _quantize(profit, MONEY_QUANTUM) if profit is not None else None,
            "expected_gross_margin_percent": margin_percent,
            "last_movement_at": balance.last_movement_at,
        })

    if department:
        rows = [row for row in rows if department.lower() in row["department"].lower()]
    if low_stock_only:
        rows = [row for row in rows if row["needs_reorder"]]
    if sellable is True:
        rows = [row for row in rows if row["price_basis"] == "RECIPE_IMPLIED_AVERAGE"]
    elif sellable is False:
        rows = [row for row in rows if row["price_basis"] != "RECIPE_IMPLIED_AVERAGE"]
    if min_margin is not None:
        rows = [
            row for row in rows
            if row["expected_gross_margin_percent"] is not None and row["expected_gross_margin_percent"] >= min_margin
        ]
    return rows


def sort_projection(rows, *, sort="sku", direction="asc"):
    """Sort projection rows on a whitelisted key, keeping ``None`` values last.

    ``None`` means "not derivable" (no cost basis / no active recipe). Those rows
    sort to the bottom in either direction so a descending sort by profit does not
    put unknown rows on top of a real result.
    """
    if sort not in SORT_KEYS:
        sort = "sku"
    reverse = str(direction).lower() == "desc"

    def sort_key(row):
        value = row.get(sort)
        # Money and quantity columns must sort NUMERICALLY: comparing Decimals as
        # strings makes "800.00" outrank "40000.00". Text columns stay folded to
        # lower case so SKUs/names order the way a reader expects.
        if isinstance(value, Decimal):
            return (0, value, "")
        if isinstance(value, bool):
            return (0, Decimal(1 if value else 0), "")
        return (0, Decimal(0), str(value).lower())

    # Missing values are sorted separately so they stay LAST in both directions:
    # reversing the whole list would otherwise float "not derivable" rows to the
    # top of a descending result.
    unknown = [row for row in rows if row.get(sort) is None]
    known = [row for row in rows if row.get(sort) is not None]
    return sorted(known, key=sort_key, reverse=reverse) + unknown


def projection_summary(rows):
    """Page-independent totals for the projection footer.

    Totals only cover rows where the value is derivable, and the count of rows
    excluded from each total is returned alongside it so the UI can disclose the
    coverage instead of implying a complete figure.
    """
    def _sum(field):
        return sum((row[field] for row in rows if row[field] is not None), ZERO)

    valued = [row for row in rows if row["stock_value"] is not None]
    sellable = [row for row in rows if row["expected_sales_revenue"] is not None]
    total_value = _sum("stock_value")
    total_revenue = _sum("expected_sales_revenue")
    total_profit = _sum("expected_gross_profit")
    overall_margin = _quantize(total_profit / total_revenue * Decimal(100), PERCENT_QUANTUM) if total_revenue > ZERO else None
    return {
        "projection": PROJECTION_LABEL,
        "row_count": len(rows),
        "valued_row_count": len(valued),
        "sellable_row_count": len(sellable),
        "unpriced_row_count": len(rows) - len(sellable),
        "total_stock_value": _quantize(total_value, MONEY_QUANTUM),
        "total_expected_sales_revenue": _quantize(total_revenue, MONEY_QUANTUM),
        "total_expected_gross_profit": _quantize(total_profit, MONEY_QUANTUM),
        "overall_expected_gross_margin_percent": overall_margin,
    }


def projection_filter_choices():
    """Filter option lists for the UI, resolved from existing records only.

    ``departments`` are the configured POS service areas — the departments that
    actually sell a product — and only areas with at least one active category are
    offered. ``categories`` are the stock-item categories already in use. Nothing
    is invented; an empty database returns empty lists.
    """
    active_areas = set(
        MenuCategory.objects.filter(is_active=True).values_list("service_area", flat=True)
    )
    areas = [
        label for value, label in MenuCategory.ServiceArea.choices if value in active_areas
    ]
    return {
        "departments": sorted(set(areas)),
        "categories": sorted({
            category for category in StockItem.objects.values_list("category", flat=True) if category
        }),
    }


__all__ = [
    "MONEY_QUANTUM", "SORT_KEYS", "PROJECTION_LABEL",
    "product_profitability_projection", "projection_filter_choices",
    "projection_summary", "sort_projection",
]
