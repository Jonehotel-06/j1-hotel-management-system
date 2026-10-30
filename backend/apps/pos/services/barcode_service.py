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

    Delegates to the inventory service's shared ``pos_stock_summaries``
    resolver — the same recipe/direct-sale mapping the delivery-time deduction
    uses — so the location and figures here are exactly the store and stock a
    sale would be deducted from. Returns ``tracked=False`` for items with no
    stock mapping (or an explicit NO_STOCK recipe); the POS must not invent
    availability for those. For tracked items, ``saleable_quantity`` is how
    many whole menu units the scarcest component can still produce, and
    ``components`` keeps the raw per-component balances visible for staff
    diagnostics. ``unconfigured=True`` marks a product that is claimed for
    stock tracking but whose mapping (store, stock item, per-sale quantity) is
    invalid, so it must never be silently treated as available.
    """
    from apps.inventory.services.inventory_service import pos_stock_summaries

    from .food_service import prepared_stock_summaries
    summary = prepared_stock_summaries([menu_item], pos_stock_summaries([menu_item.pk])).get(menu_item.pk)
    if summary is None:  # pragma: no cover - defensive; resolver always answers
        return {
            "tracked": False,
            "unconfigured": False,
            "unconfigured_reason": "",
            "location_code": "",
            "location_name": "",
            "saleable_quantity": None,
            "sufficient_for_one": None,
            "stock_unit": "",
            "components": [],
        }
    return summary


def resolve_menu_item(menu_item_id, *, service_areas=None) -> MenuItem:
    """Resolve one known menu item by its identifier for live stock checks.

    Used by the POS page (through the barcode-lookup endpoint's optional
    ``menu_item_id`` parameter) to re-verify an item's CURRENT stock before
    adding it to an order. Applies exactly the same availability, category and
    service-area scoping as a scanned barcode, and never returns an item POS
    could not legally sell in the requested mode.
    """
    item = _base_queryset(service_areas).filter(pk=menu_item_id).first()
    if item is None:
        raise NotFound("Menu item not found or not currently sellable in this POS mode.")
    return item


def barcode_lookup_payload(item_or_code, *, service_areas=None) -> dict:
    if isinstance(item_or_code, MenuItem):
        item, matched_by = item_or_code, "menu_item"
    else:
        item, matched_by = resolve_barcode(item_or_code, service_areas=service_areas)
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
