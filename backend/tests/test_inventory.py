"""Stock-ledger conservation and procurement maker-checker tests."""
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.inventory.models import PurchaseOrder, StockBalance, StockConsumptionRequest, StockCount, StockMovement, StockItem, StockLocation, Supplier
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosOrderLine
from apps.inventory.services.inventory_service import (
    approve_purchase_order, create_purchase_order, issue_stock, mark_purchase_order_ordered,
    receive_goods, record_stock_movement, request_pos_stock_consumption, start_stock_count, submit_purchase_order, submit_stock_count,
    approve_stock_count, create_stock_recipe, process_pos_stock_consumption,
)

from .base import BaseAPITestCase
from .factories import make_staff


class InventoryProcurementTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("inventory-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("inventory-manager@staff.dev", role=User.Role.MANAGER)
        self.supplier = Supplier.objects.create(reference="SUP-TEST-1", name="Fresh Supplies Ltd")
        self.location = StockLocation.objects.create(code="STORE", name="Main Store")
        self.item = StockItem.objects.create(sku="TOWEL-WHITE", name="White towel", base_unit="PIECE", standard_cost="500.00")

    def test_receipts_append_stock_ledger_and_prevent_over_receive_or_negative_issue(self):
        po, created = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location, requested_by=self.clerk,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "5", "unit_cost": "500"}],
            idempotency_key="inventory-po-1",
        )
        self.assertTrue(created)
        submitted = submit_purchase_order(purchase_order=po, actor=self.clerk)
        approved = approve_purchase_order(purchase_order=submitted, actor=self.manager)
        ordered = mark_purchase_order_ordered(purchase_order=approved, actor=self.clerk)
        po_line = ordered.lines.get()
        first, first_created = receive_goods(
            purchase_order=ordered, actor=self.clerk, source_key="receipt-1",
            receipt_lines=[{"purchase_order_line_id": po_line.pk, "quantity_received": "3", "unit_cost": "510"}],
        )
        repeated, repeat_created = receive_goods(
            purchase_order=ordered, actor=self.clerk, source_key="receipt-1",
            receipt_lines=[{"purchase_order_line_id": po_line.pk, "quantity_received": "3", "unit_cost": "510"}],
        )
        self.assertTrue(first_created)
        self.assertFalse(repeat_created)
        self.assertEqual(first.pk, repeated.pk)
        balance = StockBalance.objects.get(item=self.item, location=self.location)
        self.assertEqual(balance.quantity_on_hand, Decimal("3.0000"))
        ordered.refresh_from_db()
        self.assertEqual(ordered.status, PurchaseOrder.Status.PARTIALLY_RECEIVED)

        second, second_created = receive_goods(
            purchase_order=ordered, actor=self.clerk, source_key="receipt-2",
            receipt_lines=[{"purchase_order_line_id": po_line.pk, "quantity_received": "2"}],
        )
        self.assertTrue(second_created)
        ordered.refresh_from_db(); balance.refresh_from_db()
        self.assertEqual(ordered.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(balance.quantity_on_hand, Decimal("5.0000"))
        self.assertEqual(StockMovement.objects.filter(item=self.item).count(), 2)

        with self.assertRaisesMessage(ValidationError, "negative"):
            issue_stock(item=self.item, location=self.location, quantity="6", actor=self.clerk, source_key="issue-too-many")
        issued, issued_created = issue_stock(item=self.item, location=self.location, quantity="1", actor=self.clerk, source_key="issue-one")
        self.assertTrue(issued_created)
        self.assertEqual(issued.quantity_delta, Decimal("-1.0000"))
        balance.refresh_from_db()
        self.assertEqual(balance.quantity_on_hand, Decimal("4.0000"))

    def test_purchase_order_requester_cannot_self_approve(self):
        po, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.location, requested_by=self.manager,
            lines=[{"item_id": self.item.pk, "quantity_ordered": "1", "unit_cost": "500"}],
        )
        po = submit_purchase_order(purchase_order=po, actor=self.manager)
        with self.assertRaises(PermissionDenied):
            approve_purchase_order(purchase_order=po, actor=self.manager)


    def test_stock_count_variance_needs_independent_approval_and_posts_a_ledger_adjustment(self):
        record_stock_movement(
            item=self.item, location=self.location, movement_type=StockMovement.Type.RECEIPT,
            quantity_delta="5", source_key="test-opening-count-stock", actor=self.clerk,
        )
        stock_count = start_stock_count(location=self.location, item_ids=[self.item.pk], actor=self.clerk)
        line = stock_count.lines.get()
        stock_count = submit_stock_count(
            stock_count=stock_count, actor=self.clerk,
            counts=[{"line_id": line.pk, "counted_quantity": "3"}],
        )
        self.assertEqual(stock_count.status, StockCount.Status.PENDING_APPROVAL)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal("5.0000"))

        posted = approve_stock_count(stock_count=stock_count, actor=self.manager)
        self.assertEqual(posted.status, StockCount.Status.POSTED)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal("3.0000"))
        line.refresh_from_db()
        self.assertEqual(line.adjustment_movement.type, StockMovement.Type.ADJUSTMENT)
        self.assertEqual(line.adjustment_movement.quantity_delta, Decimal("-2.0000"))
        self.assertEqual(approve_stock_count(stock_count=posted, actor=self.manager).pk, posted.pk)

    def test_stock_count_initiator_cannot_approve_own_variance(self):
        stock_count = start_stock_count(location=self.location, item_ids=[self.item.pk], actor=self.manager)
        line = stock_count.lines.get()
        stock_count = submit_stock_count(
            stock_count=stock_count, actor=self.manager,
            counts=[{"line_id": line.pk, "counted_quantity": "1"}],
        )
        with self.assertRaises(PermissionDenied):
            approve_stock_count(stock_count=stock_count, actor=self.manager)


    def test_inventory_processes_frozen_pos_recipe_request_exactly_once(self):
        record_stock_movement(
            item=self.item, location=self.location, movement_type=StockMovement.Type.OPENING,
            quantity_delta="10", source_key="test-pos-opening", actor=self.clerk,
        )
        category = MenuCategory.objects.create(name="Drinks", slug="drinks")
        menu_item = MenuItem.objects.create(category=category, name="Bottled water", sku="MENU-WATER", base_price="300")
        recipe, recipe_created = create_stock_recipe(
            menu_item=menu_item, location=self.location, tracking_mode="COMPONENTS",
            components=[{"item_id": self.item.pk, "quantity_per_menu_unit": "1"}],
            actor=self.clerk, idempotency_key="test-water-recipe",
        )
        self.assertTrue(recipe_created)
        order = PosOrder.objects.create(reference="POS-STOCK-TEST", mode=PosOrder.Mode.RESTAURANT, created_by=self.clerk)
        PosOrderLine.objects.create(order=order, menu_item=menu_item, item_name=menu_item.name, item_sku=menu_item.sku,
                                    unit_price="300", quantity=2, line_total="600")
        order.status = PosOrder.Status.DELIVERED; order.save(update_fields=["status", "updated_at"])
        request, request_created = request_pos_stock_consumption(order=order, actor=self.clerk)
        self.assertTrue(request_created)
        self.assertEqual(request.payload["recipe_snapshot"][str(menu_item.pk)]["recipe_reference"], recipe.reference)

        processed, processed_now = process_pos_stock_consumption(request=request, actor=self.clerk)
        self.assertTrue(processed_now)
        self.assertEqual(processed.status, StockConsumptionRequest.Status.PROCESSED)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.location).quantity_on_hand, Decimal("8.0000"))
        movement = StockMovement.objects.get(type=StockMovement.Type.POS_CONSUMPTION)
        self.assertEqual(movement.quantity_delta, Decimal("-2.0000"))
        retried, retried_now = process_pos_stock_consumption(request=processed, actor=self.clerk)
        self.assertFalse(retried_now)
        self.assertEqual(retried.pk, processed.pk)
        self.assertEqual(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 1)
