"""The one-way rescale from pack-counted stock to piece-counted stock.

Reported defect: after a receipt, "pieces" were shown, approved and recorded as
PACK/CRATE/BAG. The data migration (inventory/0010) makes the stored numbers
agree with the unit by rescaling physically: quantities × pack size, money ÷
pack size. Total quantity and total value are unchanged.

This test drives the migration's own function against the current schema (the
fields it touches still exist), so the conversion maths and the "no declared
pack size → leave it alone" rule are both pinned.

Run: python manage.py test tests.test_inventory_unit_migration
"""
from decimal import Decimal

from django.apps import apps as global_apps
from django.test import TestCase

from apps.accounts.models import User
from apps.inventory.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    StockBalance,
    StockCount,
    StockCountLine,
    StockItem,
    StockLocation,
    StockMovement,
    StockRecipe,
    StockRecipeLine,
    Supplier,
)
from apps.pos.models import MenuCategory, MenuItem

from .factories import make_staff


def _migration_module():
    import importlib

    return importlib.import_module("apps.inventory.migrations.0010_normalize_pack_counted_items")


class PackUnitNormalisationTests(TestCase):
    def setUp(self):
        self.clerk = make_staff("migration-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.supplier = Supplier.objects.create(reference="SUP-MIG-1", name="Migration Supplier")
        self.location = StockLocation.objects.create(code="MIG-STORE", name="Migration Store")
        self.item = StockItem.objects.create(
            sku="MIG-WATER", name="Sachet water", base_unit="PACK", units_per_pack=Decimal("24.00"),
            standard_cost=Decimal("2400.00"), reorder_level=Decimal("2.0000"),
        )
        # A legacy container item with no declared size: nothing can convert it.
        self.untouched = StockItem.objects.create(
            sku="MIG-CRATE", name="Empty crate", base_unit="CRATE",
            standard_cost=Decimal("5000.00"),
        )

        self.balance = StockBalance.objects.create(
            item=self.item, location=self.location, quantity_on_hand=Decimal("5.0000"),
        )
        self.movement = StockMovement.objects.create(
            reference="MV-MIG-1", source_key="migration-movement-1", item=self.item,
            location=self.location, balance=self.balance, type=StockMovement.Type.RECEIPT,
            quantity_delta=Decimal("5.0000"), unit_cost=Decimal("2400.00"),
        )
        self.po = PurchaseOrder.objects.create(
            reference="PO-MIG-1", supplier=self.supplier, delivery_location=self.location,
            requested_by=self.clerk,
        )
        self.po_line = PurchaseOrderLine.objects.create(
            purchase_order=self.po, item=self.item, item_sku=self.item.sku, item_name=self.item.name,
            quantity_ordered=Decimal("5.0000"), quantity_received=Decimal("5.0000"),
            unit_cost=Decimal("2400.00"),
        )
        self.receipt = GoodsReceipt.objects.create(
            reference="GR-MIG-1", source_key="migration-receipt-1", purchase_order=self.po,
            location=self.location, supplier=self.supplier,
        )
        self.receipt_line = GoodsReceiptLine.objects.create(
            goods_receipt=self.receipt, purchase_order_line=self.po_line, item=self.item,
            quantity_received=Decimal("5.0000"), unit_cost=Decimal("2400.00"),
            stock_movement=self.movement,
        )

        category = MenuCategory.objects.create(
            name="Migration drinks", slug="migration-drinks", service_area="BAR",
        )
        menu_item = MenuItem.objects.create(
            category=category, name="Sachet water", sku="MIG-MENU-1", base_price=Decimal("100.00"),
        )
        self.recipe = StockRecipe.objects.create(
            reference="RCP-MIG-1", menu_item=menu_item, location=self.location, version=1,
            created_by=self.clerk,
        )
        self.recipe_line = StockRecipeLine.objects.create(
            recipe=self.recipe, item=self.item, quantity_per_menu_unit=Decimal("1.0000"),
        )

        self.count = StockCount.objects.create(
            reference="CNT-MIG-1", location=self.location, initiated_by=self.clerk,
        )
        self.count_line = StockCountLine.objects.create(
            stock_count=self.count, item=self.item, expected_quantity=Decimal("5.0000"),
            counted_quantity=Decimal("4.0000"), variance=Decimal("-1.0000"),
        )

    def _run(self):
        module = _migration_module()
        module.normalise_pack_counted_items(global_apps, None)

    def _refresh(self):
        for obj in (
            self.item, self.untouched, self.balance, self.movement, self.po_line,
            self.receipt_line, self.recipe_line, self.count_line,
        ):
            obj.refresh_from_db()

    def test_pack_quantities_become_pieces_and_money_stays_per_piece(self):
        self._run()
        self._refresh()

        self.assertEqual(self.item.base_unit, "PIECE")
        self.assertEqual(self.item.units_per_pack, Decimal("24.00"))

        # 5 packs of 24 = 120 pieces, and ₦2,400 per pack = ₦100 per piece.
        self.assertEqual(self.balance.quantity_on_hand, Decimal("120.0000"))
        self.assertEqual(self.movement.quantity_delta, Decimal("120.0000"))
        self.assertEqual(self.movement.unit_cost, Decimal("100.00"))
        self.assertEqual(self.po_line.quantity_ordered, Decimal("120.0000"))
        self.assertEqual(self.po_line.quantity_received, Decimal("120.0000"))
        self.assertEqual(self.po_line.unit_cost, Decimal("100.00"))
        self.assertEqual(self.receipt_line.quantity_received, Decimal("120.0000"))
        self.assertEqual(self.receipt_line.unit_cost, Decimal("100.00"))
        self.assertEqual(self.recipe_line.quantity_per_menu_unit, Decimal("24.0000"))
        self.assertEqual(self.count_line.expected_quantity, Decimal("120.0000"))
        self.assertEqual(self.count_line.counted_quantity, Decimal("96.0000"))
        self.assertEqual(self.count_line.variance, Decimal("-24.0000"))

        # Item-level thresholds follow the same conversion.
        self.assertEqual(self.item.reorder_level, Decimal("48.0000"))
        self.assertEqual(self.item.standard_cost, Decimal("100.00"))

    def test_the_total_value_of_stock_is_unchanged(self):
        before = self.balance.quantity_on_hand * self.movement.unit_cost
        self._run()
        self._refresh()
        after = self.balance.quantity_on_hand * self.movement.unit_cost
        self.assertEqual(before, after)

    def test_an_item_without_a_declared_pack_size_is_left_alone(self):
        self._run()
        self._refresh()
        self.assertEqual(self.untouched.base_unit, "CRATE")
        self.assertEqual(self.untouched.standard_cost, Decimal("5000.00"))

    def test_running_it_twice_changes_nothing_further(self):
        self._run()
        self._refresh()
        snapshot = (
            self.item.base_unit, self.item.standard_cost, self.balance.quantity_on_hand,
            self.po_line.quantity_ordered, self.recipe_line.quantity_per_menu_unit,
        )
        # The item is no longer a packaging unit, so a second pass finds nothing.
        self._run()
        self._refresh()
        self.assertEqual(snapshot, (
            self.item.base_unit, self.item.standard_cost, self.balance.quantity_on_hand,
            self.po_line.quantity_ordered, self.recipe_line.quantity_per_menu_unit,
        ))
