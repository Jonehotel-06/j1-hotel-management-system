"""Make piece-counted stock truly piece-based.

The bug this repairs: an item could be saved with ``base_unit="PACK"`` *and*
``units_per_pack=24``. Receipts then converted "1 pack" into 24 and wrote that
into a column whose unit said PACK, so the ledger recorded 24 PACK (288 pieces)
for a single pack. The same wrong unit then appeared on the purchase order, the
goods receipt and every movement row.

The repair is a physical-unit rescale, not a relabelling: for every item that
has BOTH a packaging base unit and a declared pack size, quantities are
multiplied by the pack size and money per unit is divided by it. The physical
quantity and the total value are therefore unchanged; only the unit becomes
PIECE, which is what the operator meant. Items with a packaging unit and NO
declared pack size are left untouched — there is no trustworthy factor to
convert them with, and inventing one would corrupt the ledger.

This migration is intentionally one-way: the reverse would have to guess whether
a PIECE quantity was always piece-based. Keep the historical unit in the audit
log instead.
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import F

#: A container, not a countable unit. Mirrors StockItemSerializer.PACKAGING_UNITS.
PACKAGING_UNITS = {"PACK", "PACKET", "BAG", "CRATE", "CARTON", "CASE", "SACK", "BOX"}


def _rescale(apps, factor, item_id):
    StockBalance = apps.get_model("inventory", "StockBalance")
    StockMovement = apps.get_model("inventory", "StockMovement")
    PurchaseOrderLine = apps.get_model("inventory", "PurchaseOrderLine")
    GoodsReceiptLine = apps.get_model("inventory", "GoodsReceiptLine")
    StockRecipeLine = apps.get_model("inventory", "StockRecipeLine")
    StockCountLine = apps.get_model("inventory", "StockCountLine")

    StockBalance.objects.filter(item_id=item_id).update(
        quantity_on_hand=F("quantity_on_hand") * factor,
    )
    StockMovement.objects.filter(item_id=item_id).update(
        quantity_delta=F("quantity_delta") * factor,
        unit_cost=F("unit_cost") / factor,
    )
    PurchaseOrderLine.objects.filter(item_id=item_id).update(
        quantity_ordered=F("quantity_ordered") * factor,
        quantity_received=F("quantity_received") * factor,
        unit_cost=F("unit_cost") / factor,
    )
    GoodsReceiptLine.objects.filter(item_id=item_id).update(
        quantity_received=F("quantity_received") * factor,
        unit_cost=F("unit_cost") / factor,
    )
    StockRecipeLine.objects.filter(item_id=item_id).update(
        quantity_per_menu_unit=F("quantity_per_menu_unit") * factor,
    )
    StockCountLine.objects.filter(item_id=item_id).update(
        expected_quantity=F("expected_quantity") * factor,
        counted_quantity=F("counted_quantity") * factor,
        variance=F("variance") * factor,
    )


def normalise_pack_counted_items(apps, schema_editor):
    StockItem = apps.get_model("inventory", "StockItem")
    converted = 0
    skipped = 0
    for item in StockItem.objects.filter(base_unit__in=PACKAGING_UNITS).order_by("pk"):
        factor = item.units_per_pack
        if factor is None or Decimal(factor) <= Decimal("0"):
            # No declared pack size: the pack genuinely is the counted unit.
            skipped += 1
            continue
        factor = Decimal(factor)
        _rescale(apps, factor, item.pk)
        StockItem.objects.filter(pk=item.pk).update(
            base_unit="PIECE",
            reorder_level=F("reorder_level") * factor,
            standard_cost=F("standard_cost") / factor,
            direct_sale_quantity=F("direct_sale_quantity") * factor,
        )
        converted += 1
    if converted or skipped:
        print(
            f"\n  inventory units normalised: {converted} item(s) converted from a "
            f"packaging unit to PIECE; {skipped} item(s) kept their packaging unit "
            f"because no pack size is recorded."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0009_purchaseorder_approval_receipt_and_more"),
    ]

    operations = [
        migrations.RunPython(normalise_pack_counted_items, migrations.RunPython.noop),
    ]
