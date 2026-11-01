"""
Task 03 of targeted_fixes_01 — USB HID barcode scanning for the INVENTORY
screen.

What a scan is allowed to do
============================
**Identify one stock item and nothing else.**

The POS terminal already resolves a scan to a menu item and adds it to a cart
(``apps.pos.services.barcode_service``); that workflow is untouched. The stock
room had no equivalent, so a store keeper receiving a delivery had to hunt for
the item in a long list. This module answers exactly one question — "which
stock item is this barcode?" — and returns its identity plus the CURRENT
balance at each location.

What a scan must never do
=========================
* it does not create, adjust or transfer stock;
* it does not create a receipt, an issue, a count or a movement of any kind;
* it does not change the balance projection.

Recording stock remains an explicit, separately-permissioned action
(``inventory.record``, see task 09). A scan only *selects* a row; the operator
still presses the button that records the movement.

Ambiguity and absence
=====================
A code that matches more than one item is refused with an explanation rather
than silently picking one. A code that matches nothing returns a clear
"not found" message — the screen shows the code that was scanned so the
operator can check it, and it is never mistaken for a successful scan.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework import status
from rest_framework.exceptions import ValidationError

from apps.core.exceptions import JOneAPIError

from ..models import StockBalance, StockItem


class StockBarcodeNotFound(JOneAPIError):
    """404 with the operator-facing message preserved.

    DRF's generic ``NotFound`` is rewritten to "The requested resource was not
    found." by the project exception handler, which is unhelpful when a store
    keeper has just scanned a label that is not in the catalogue. A domain error
    keeps the specific message and a machine-readable code the screen can key
    off, while still answering HTTP 404.
    """

    status_code = status.HTTP_404_NOT_FOUND
    default_detail = (
        "No stock item matches this barcode. Check the barcode, or add the item to the stock catalogue first."
    )
    default_code = "STOCK_BARCODE_NOT_FOUND"


class StockBarcodeAmbiguous(JOneAPIError):
    """400 when one scanned code honestly matches two stock items.

    Guessing would put the wrong item on a receipt, so the scan is refused and
    the message names the fix (correct the duplicate). A domain error keeps that
    wording instead of collapsing it to "Validation failed."
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = (
        "This barcode matches more than one stock item. Correct the duplicate barcode before scanning it."
    )
    default_code = "STOCK_BARCODE_AMBIGUOUS"

#: The message every "no such item" path returns, so the screen, the API and
#: the tests all agree on one wording.
NOT_FOUND_MESSAGE = (
    "No stock item matches this barcode. Check the barcode, or add the item to the stock catalogue first."
)
AMBIGUOUS_MESSAGE = (
    "This barcode matches more than one stock item. Correct the duplicate barcode before scanning it."
)

MIN_SCAN_LENGTH = 3
MAX_SCAN_LENGTH = 64


def normalize_scan(value) -> str:
    """Trim a scanned value. Leading zeros are significant and are preserved."""
    return str(value or "").strip()


def resolve_stock_item(code):
    """Resolve one scanned code to ``(stock_item, matched_by)``.

    Resolution order is deliberate and mirrors the POS lookup so staff learn one
    behaviour:

    1. exact barcode, then exact SKU (labels printed from an existing SKU keep
       scanning);
    2. a case-insensitive retry for older capitalised labels;
    3. otherwise "not found".

    An inactive item still resolves — the store keeper needs to be told that the
    item exists but is retired — and the caller decides what to show.
    """
    scanned = normalize_scan(code)
    if not scanned:
        raise ValidationError({"barcode": "Scan or type a barcode first."})
    if len(scanned) < MIN_SCAN_LENGTH:
        raise ValidationError({"barcode": f"A barcode must be at least {MIN_SCAN_LENGTH} characters."})
    if len(scanned) > MAX_SCAN_LENGTH:
        raise ValidationError({"barcode": f"A barcode must be at most {MAX_SCAN_LENGTH} characters."})

    queryset = StockItem.objects.all()

    exact = list(
        queryset.filter(Q(barcode__exact=scanned) | Q(sku__exact=scanned)).order_by("pk")[:3]
    )
    if len(exact) == 1:
        item = exact[0]
        return item, ("barcode" if (item.barcode or "") == scanned else "sku")
    if len(exact) > 1:
        raise StockBarcodeAmbiguous()

    insensitive = list(
        queryset.filter(Q(barcode__iexact=scanned) | Q(sku__iexact=scanned)).order_by("pk")[:3]
    )
    if len(insensitive) == 1:
        item = insensitive[0]
        matched_by = "barcode" if (item.barcode or "").casefold() == scanned.casefold() else "sku"
        return item, matched_by
    if len(insensitive) > 1:
        raise StockBarcodeAmbiguous()

    raise StockBarcodeNotFound()


def scan_payload(item: StockItem, matched_by: str) -> dict:
    """The identity a scan resolves to, plus its CURRENT balances.

    Read-only: this function issues SELECTs only.
    """
    balances = (
        StockBalance.objects.filter(item=item)
        .select_related("location")
        .order_by("location__code", "pk")
    )
    locations = [
        {
            "location_id": balance.location_id,
            "location_code": balance.location.code,
            "location_name": balance.location.name,
            "quantity_on_hand": str(balance.quantity_on_hand),
            "last_movement_at": balance.last_movement_at.isoformat() if balance.last_movement_at else None,
        }
        for balance in balances
    ]
    return {
        "item_id": item.pk,
        "sku": item.sku,
        "barcode": item.barcode or "",
        "name": item.name,
        "category": item.category or "",
        "base_unit": item.base_unit,
        "standard_cost": str(item.standard_cost),
        "currency": item.currency,
        "is_active": bool(item.is_active),
        "is_reusable_asset": bool(getattr(item, "is_reusable_asset", False)),
        "matched_by": matched_by,
        "locations": locations,
        # `total_quantity` is the sum across locations, expressed in the item's
        # base unit. It is a disclosure of current stock, NOT an action.
        "total_quantity": _sum_quantities(locations),
    }


def _sum_quantities(locations) -> str:
    from decimal import Decimal

    total = Decimal("0.0000")
    for row in locations:
        try:
            total += Decimal(str(row.get("quantity_on_hand") or "0"))
        except Exception:  # noqa: BLE001 - a malformed stored value must not crash a scan
            continue
    return str(total)
