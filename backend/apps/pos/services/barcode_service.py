"""Server-side barcode resolution for the POS terminal.

The browser is never trusted for identity or price: a scanned value is
resolved here against active, available menu items in the service areas the
caller may sell, and the authoritative ``base_price`` stored on the item is
returned. Stock figures reuse the existing inventory recipe snapshot and
``StockBalance`` projection used by POS stock consumption — this module
never deducts stock and never creates a second depletion path. Stock is
deducted exactly once, later, when an order is delivered (see
``apps.inventory.services.inventory_service.consume_stock_for_delivered_order``).
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q
from rest_framework.exceptions import NotFound

from ..barcode import normalize_barcode, validate_barcode
from ..models import MenuItem

NOT_FOUND_MESSAGE = "Product barcode not found. Check the barcode or register the product first."


def _base_queryset(service_areas=None):
    queryset = (
        MenuItem.objects.select_related("category")
        .prefetch_related("modifiers")
        .filter(is_active=True, is_available=True, category__is_active=True)
    )
    if service_areas:
        queryset = queryset.filter(category__service_area__in=service_areas)
    return queryset


def resolve_barcode(code, *, service_areas=None) -> tuple[MenuItem, str]:
    """Resolve one scanned code to ``(menu_item, matched_by)``.

    Exact barcode matches win; the legacy unique SKU is also accepted so
    labels printed from existing SKUs keep scanning. A case-insensitive
    fallback covers Code 39-era capitalised labels, but any ambiguity (one
    item's SKU equalling another item's barcode, or case variants) is a
    hard error instead of a guess.
    """
    try:
        normalized = validate_barcode(code)
    except ValueError as exc:
        raise ValidationError({"barcode": str(exc)}) from exc
    if normalized is None:
        raise ValidationError({"barcode": "Enter or scan a barcode."})

    queryset = _base_queryset(service_areas)
    exact = list(queryset.filter(Q(barcode__exact=normalized) | Q(sku__exact=normalized))[:3])
    matched_by = None
    if len(exact) == 1:
        item = exact[0]
        matched_by = "barcode" if item.barcode == normalized else "sku"
        return item, matched_by
    if len(exact) > 1:
        raise ValidationError({"barcode": "This barcode matches more than one product. Ask a manager to correct the barcode assignment."})

    insensitive = list(queryset.filter(Q(barcode__iexact=normalized) | Q(sku__iexact=normalized))[:3])
    if len(insensitive) == 1:
        item = insensitive[0]
        matched_by = "barcode" if (item.barcode or "").casefold() == normalized.casefold() else "sku"
        return item, matched_by
    if len(insensitive) > 1:
        raise ValidationError({"barcode": "This barcode matches more than one product. Ask a manager to correct the barcode assignment."})
    raise NotFound(NOT_FOUND_MESSAGE)


def stock_summary_for_menu_item(menu_item: MenuItem) -> dict:
    """Saleable-stock estimate from the existing recipe/balance projection.

    Returns ``tracked=False`` for items with no stock recipe (or an explicit
    NO_STOCK recipe); the POS must not invent availability for those. For
    tracked items, ``saleable_quantity`` is how many whole menu units the
    scarcest component can still produce, and ``components`` keeps the raw
    per-component balances visible for staff diagnostics.
    """
    from apps.inventory.models import StockBalance
    from apps.inventory.services.inventory_service import _active_recipe_snapshots

    snapshots = _active_recipe_snapshots([menu_item.pk])
    recipe = snapshots.get(str(menu_item.pk))
    if not recipe or recipe.get("tracking_mode") == "NO_STOCK":
        return {
            "tracked": False,
            "location_code": (recipe or {}).get("location_code", ""),
            "saleable_quantity": None,
            "sufficient_for_one": None,
            "components": [],
        }
    components = list(recipe.get("components") or ())
    if not components:
        return {
            "tracked": False,
            "location_code": recipe.get("location_code", ""),
            "saleable_quantity": None,
            "sufficient_for_one": None,
            "components": [],
        }
    balances = {
        balance.item_id: balance
        for balance in StockBalance.objects.filter(
            location_id=recipe["location_id"], item_id__in=[c["item_id"] for c in components]
        )
    }
    saleable = None
    component_rows = []
    for component in components:
        per_unit = Decimal(str(component["quantity_per_menu_unit"]))
        balance = balances.get(component["item_id"])
        on_hand = Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")
        component_saleable = (on_hand / per_unit) if per_unit > 0 else Decimal("0")
        saleable = component_saleable if saleable is None else min(saleable, component_saleable)
        component_rows.append({
            "item_id": component["item_id"],
            "item_sku": component["item_sku"],
            "quantity_per_menu_unit": str(per_unit),
            "quantity_on_hand": str(on_hand),
        })
    whole_units = int(saleable) if saleable is not None else 0
    return {
        "tracked": True,
        "location_code": recipe.get("location_code", ""),
        "saleable_quantity": str(whole_units),
        "sufficient_for_one": whole_units >= 1,
        "components": component_rows,
    }


def barcode_lookup_payload(code, *, service_areas=None) -> dict:
    item, matched_by = resolve_barcode(code, service_areas=service_areas)
    return {
        "menu_item_id": item.pk,
        "name": item.name,
        "sku": item.sku,
        "barcode": item.barcode,
        "matched_by": matched_by,
        "category_id": item.category_id,
        "category_name": item.category.name,
        "service_area": item.category.service_area,
        "base_price": item.base_price,
        "currency": item.currency,
        "is_available": item.is_available,
        "modifiers": [m for m in item.modifiers.all() if m.is_active],
        "stock": stock_summary_for_menu_item(item),
    }
