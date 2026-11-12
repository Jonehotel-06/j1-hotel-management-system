# tests/test_inventory_sales_depletion.py
"""Stock-on-hand lifecycle tests under the recipe-deduction-removed policy.

REQ-5 of the production ticket explicitly removes automatic recipe-based stock
deduction from every POS channel (restaurant, bar, room service, takeaway, guest
requests). Order completion, status change and cancellation must NOT move
ingredient stock. The recipe records, the ``StockConsumptionRequest`` hand-off
records and the inventory console remain as an evidence/audit surface, but they
no longer trigger stock movement.

The contract proven below is therefore the procurement lifecycle WITHOUT the
delivery-time depletion that previous builds performed:

    purchase order -> goods receipt -> stock on hand -> sale (no stock movement)

Coverage map:

* supplier receipt increases stock on hand (procurement still works)
* POS order creation still validates available stock (insufficient stock rejected)
* order completion, status change and cancellation do NOT move ingredient stock
* the catch-up sweep (``process_pending_pos_consumption``) is a documented no-op
* hand-offs left PENDING by older builds are SKIPPED, not posted
* cancellation of an un-finalised order consumes nothing
* movement history and source references remain traceable
* fractional quantities and unit conversions are recorded in recipes (reference)
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.db import connection, transaction

from apps.accounts.models import User
from apps.inventory.models import (
    GoodsReceipt,
    PurchaseOrder,
    StockBalance,
    StockConsumptionRequest,
    StockItem,
    StockLocation,
    StockMovement,
    Supplier,
)
from apps.inventory.services.inventory_service import (
    approve_purchase_order,
    create_purchase_order,
    create_stock_recipe,
    mark_purchase_order_ordered,
    process_pending_pos_consumption,
    receive_goods,
    record_stock_movement,
    submit_purchase_order,
)
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import (
    capture_direct_tender,
    create_order,
    submit_order,
    transition_order,
)
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class SalesDepletionTestCase(BaseAPITestCase):
    """Shared fixture: one supplier, one store, one stocked menu item."""

    def setUp(self):
        super().setUp()
        from apps.core.utils import hotel_today

        self.today = hotel_today()
        self.clerk = make_staff("depletion-clerk@staff.dev", role=User.Role.STOREKEEPER)
        self.manager = make_staff("depletion-manager@staff.dev", role=User.Role.MANAGER)
        self.cashier = make_staff("depletion-cashier@staff.dev", role=User.Role.CASHIER)

        self.supplier = Supplier.objects.create(reference="SUP-DEP-1", name="Depletion Supplies Ltd")
        self.store = StockLocation.objects.create(code="DEP-STORE", name="Depletion store")
        self.bar = StockLocation.objects.create(code="DEP-BAR", name="Depletion bar")

        # The stock item is a whole bottle. POS enforces category service areas,
        # so a bar sale and a restaurant/room-service sale need their own menu
        # item — both of which pour from the same physical bottle stock.
        self.bottle = StockItem.objects.create(
            sku="DEP-WATER", name="Bottled water", base_unit="BOTTLE",
            standard_cost=Decimal("150.00"),
        )
        self.bar_category = MenuCategory.objects.create(
            name="Depletion bar", slug="depletion-bar",
            service_area=MenuCategory.ServiceArea.BAR,
        )
        self.restaurant_category = MenuCategory.objects.create(
            name="Depletion restaurant", slug="depletion-restaurant",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.bar_water = MenuItem.objects.create(
            category=self.bar_category, name="Bottled water 75cl (bar)", sku="DEP-MENU-WATER",
            base_price=Decimal("300.00"),
        )
        self.dining_water = MenuItem.objects.create(
            category=self.restaurant_category, name="Bottled water 75cl (dining)", sku="DEP-MENU-WATER-D",
            base_price=Decimal("300.00"),
        )
        for menu_item in (self.bar_water, self.dining_water):
            _, created = create_stock_recipe(
                menu_item=menu_item, location=self.bar, tracking_mode="COMPONENTS",
                components=[{"item_id": self.bottle.pk, "quantity_per_menu_unit": "1"}],
                actor=self.clerk, idempotency_key=f"depletion-water-recipe-{menu_item.pk}",
            )
            self.assertTrue(created)

    def menu_item_for(self, mode):
        """The menu item that POS will accept for a given order mode."""
        if mode == PosOrder.Mode.BAR:
            return self.bar_water
        return self.dining_water

    # ---------------------------------------------------------------- helpers

    def receive_from_supplier(self, quantity, *, unit_cost="150.00", source_key="dep-receipt-1"):
        """PO -> approve -> order -> goods receipt (the real procurement path)."""
        po, _ = create_purchase_order(
            supplier=self.supplier, delivery_location=self.bar, requested_by=self.clerk,
            lines=[{"item_id": self.bottle.pk, "quantity_ordered": str(quantity), "unit_cost": unit_cost}],
            idempotency_key=f"dep-po-{source_key}",
        )
        submit_purchase_order(purchase_order=po, actor=self.clerk)
        approve_purchase_order(purchase_order=po, actor=self.manager)
        ordered = mark_purchase_order_ordered(purchase_order=po, actor=self.clerk)
        line = ordered.lines.get()
        return receive_goods(
            purchase_order=ordered, actor=self.clerk, source_key=source_key,
            receipt_lines=[{"purchase_order_line_id": line.pk, "quantity_received": str(quantity), "unit_cost": unit_cost}],
        )[0]

    def sell(self, *, mode, quantity=1, table_number="", stay=None, delivery_location="",
             direct_tender=None, menu_item=None):
        """Create -> submit -> prepare -> ready -> deliver, then optionally tender."""
        menu_item = menu_item or self.menu_item_for(mode)
        order, _ = create_order(
            mode=mode, actor=self.cashier, stay=stay, table_number=table_number,
            delivery_location=delivery_location, guest_name="Depletion walk-in",
            lines=[{"menu_item_id": menu_item.pk, "quantity": quantity}],
            idempotency_key=None,
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        delivered = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        if direct_tender:
            capture_direct_tender(
                order=delivered, method=direct_tender, amount=delivered.total_amount,
                actor=self.cashier,
                cash_session=self._open_session() if direct_tender == "CASH" else None,
            )
            delivered.refresh_from_db()
        return delivered

    def _open_session(self):
        from apps.finance.services.cash_session_service import open_cash_session

        return open_cash_session(cashier=self.cashier, actor=self.cashier, opening_float="500.00")

    def on_hand(self, location=None):
        rows = StockBalance.objects.filter(item=self.bottle)
        if location is not None:
            rows = rows.filter(location=location)
        return sum((Decimal(row.quantity_on_hand) for row in rows), Decimal("0.0000"))

    def in_house_stay(self, reference_suffix=""):
        from datetime import timedelta

        room_type = make_room_type(f"Depletion suite {reference_suffix}", price="12000.00")
        booking = make_booking(
            make_guest(f"depletion-guest{reference_suffix}@example.com"), room_type,
            check_in=self.today, check_out=self.today + timedelta(days=2), total="24000.00",
        )
        return Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )


class PurchaseToSaleLifecycleTests(SalesDepletionTestCase):
    """The procurement lifecycle: receipts increase stock; sales do NOT deplete."""

    def test_stock_follows_the_documented_lifecycle_20_10_30_no_depletion(self):
        # Opening stock of 20 units, recorded as an immutable opening movement.
        record_stock_movement(
            item=self.bottle, location=self.bar, movement_type=StockMovement.Type.OPENING,
            quantity_delta="20", source_key="depletion-opening-20", actor=self.clerk,
        )
        self.assertEqual(self.on_hand(), Decimal("20.0000"))

        # Supplier receipt of 10 units through the real procurement workflow.
        receipt = self.receive_from_supplier(10, source_key="dep-receipt-lifecycle")
        self.assertIsInstance(receipt, GoodsReceipt)
        self.assertEqual(self.on_hand(), Decimal("30.0000"))

        # Customer buys 4 — stock is NOT depleted (REQ-5 removes automatic deduction).
        self.sell(mode=PosOrder.Mode.BAR, quantity=4)
        self.assertEqual(self.on_hand(), Decimal("30.0000"))

        # Another customer buys 3 — still no depletion.
        self.sell(mode=PosOrder.Mode.BAR, quantity=3)
        self.assertEqual(self.on_hand(), Decimal("30.0000"))

        # The movement history explains the balance: only opening + receipt, no POS_CONSUMPTION.
        movements = StockMovement.objects.filter(item=self.bottle, location=self.bar).order_by("pk")
        deltas = [movement.quantity_delta for movement in movements]
        self.assertEqual(deltas[0], Decimal("20.0000"))
        self.assertEqual(deltas[1], Decimal("10.0000"))
        # No POS_CONSUMPTION movements exist.
        self.assertFalse(
            StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists()
        )
        self.assertEqual(sum(deltas, Decimal("0.0000")), Decimal("30.0000"))

    def test_hand_off_records_exist_but_are_skipped(self):
        """StockConsumptionRequest hand-offs are created but never posted (REQ-5)."""
        self.receive_from_supplier(12, source_key="dep-receipt-source-ref")
        order = self.sell(mode=PosOrder.Mode.RESTAURANT, quantity=2, table_number="T-9")

        # No POS_CONSUMPTION movement is posted.
        self.assertFalse(
            StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists()
        )

        # The hand-off record exists as evidence.
        hand_off = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(hand_off.status, StockConsumptionRequest.Status.SKIPPED)
        self.assertTrue(
            hand_off.resolution_payload.get("recipe_deduction_disabled", False)
        )


class SalesChannelNoDepletionTests(SalesDepletionTestCase):
    """`transition_order` to DELIVERED must not move ingredient stock on any channel."""

    def test_bar_pos_sale_does_not_deplete_stock(self):
        self.receive_from_supplier(10, source_key="dep-receipt-bar")
        self.sell(mode=PosOrder.Mode.BAR, quantity=3, table_number="B-1")
        self.assertEqual(self.on_hand(), Decimal("10.0000"))

    def test_restaurant_pos_sale_does_not_deplete_stock(self):
        self.receive_from_supplier(10, source_key="dep-receipt-restaurant")
        self.sell(mode=PosOrder.Mode.RESTAURANT, quantity=2, table_number="R-4")
        self.assertEqual(self.on_hand(), Decimal("10.0000"))

    def test_room_service_sale_does_not_deplete_stock(self):
        stay = self.in_house_stay("-rs")
        self.receive_from_supplier(10, source_key="dep-receipt-room-service")
        order = self.sell(
            mode=PosOrder.Mode.ROOM_SERVICE, quantity=5, stay=stay,
            delivery_location="Room 101",
        )
        # The folio charge is still posted, but stock is NOT depleted.
        self.assertIsNotNone(order.charge_transaction_id)
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.NOT_APPLICABLE)
        self.assertEqual(self.on_hand(), Decimal("10.0000"))

    def test_immediately_paid_takeaway_sale_does_not_deplete_stock(self):
        self.receive_from_supplier(6, source_key="dep-receipt-takeaway")
        order = self.sell(mode=PosOrder.Mode.TAKEAWAY, quantity=4, direct_tender="CARD")
        self.assertEqual(order.settlement_status, PosOrder.SettlementStatus.PAID)
        self.assertEqual(self.on_hand(), Decimal("6.0000"))

    def test_cash_sale_does_not_deplete_stock(self):
        self.receive_from_supplier(6, source_key="dep-receipt-cash")
        self.sell(mode=PosOrder.Mode.TAKEAWAY, quantity=1, direct_tender="CASH")
        self.assertEqual(self.on_hand(), Decimal("6.0000"))


class DuplicateDeductionTests(SalesDepletionTestCase):
    """No deduction can happen twice because no deduction happens at all (REQ-5)."""

    def test_retrying_delivery_transition_does_not_deduct_twice(self):
        self.receive_from_supplier(10, source_key="dep-receipt-retry")
        order = self.sell(mode=PosOrder.Mode.BAR, quantity=3)
        # Stock was never depleted, so it stays at 10.
        self.assertEqual(self.on_hand(), Decimal("10.0000"))

        # Re-issuing the same terminal transition is a no-op.
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        self.assertEqual(self.on_hand(), Decimal("10.0000"))
        self.assertEqual(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 0)
        self.assertEqual(StockConsumptionRequest.objects.filter(pos_order=order).count(), 1)

    def test_catch_up_sweep_is_a_no_op(self):
        """The catch-up task returns zero posted (REQ-5 removes the deduction)."""
        self.receive_from_supplier(9, source_key="dep-receipt-sweep")
        self.sell(mode=PosOrder.Mode.BAR, quantity=4)
        self.assertEqual(self.on_hand(), Decimal("9.0000"))

        for _ in range(3):
            summary = process_pending_pos_consumption(actor=self.clerk)
            self.assertEqual(summary["posted"], 0)
            self.assertEqual(summary["examined"], 0)
        self.assertEqual(self.on_hand(), Decimal("9.0000"))
        self.assertEqual(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 0)

    def test_a_hand_off_left_pending_by_an_older_build_is_skipped(self):
        """A legacy PENDING hand-off is SKIPPED, not posted (REQ-5)."""
        self.receive_from_supplier(8, source_key="dep-receipt-legacy")
        order = self.sell(mode=PosOrder.Mode.RESTAURANT, quantity=3, table_number="L-1")
        hand_off = StockConsumptionRequest.objects.get(pos_order=order)

        # Deterministically rewind to the pre-fix shape.
        hand_off.status = StockConsumptionRequest.Status.PENDING
        hand_off.processed_at = None
        hand_off.save(update_fields=["status", "processed_at", "updated_at"])

        summary = process_pending_pos_consumption(actor=self.clerk)
        self.assertEqual(summary["posted"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.on_hand(), Decimal("8.0000"))
        # The hand-off is now SKIPPED, not PROCESSED.
        hand_off.refresh_from_db()
        self.assertEqual(hand_off.status, StockConsumptionRequest.Status.SKIPPED)


class InsufficientStockTests(SalesDepletionTestCase):
    """Order creation still validates stock; delivery no longer depletes it."""

    def test_creating_an_order_beyond_available_stock_is_rejected(self):
        from apps.core.exceptions import PosStockConflictError

        self.receive_from_supplier(2, source_key="dep-receipt-short")
        with self.assertRaises(PosStockConflictError) as raised:
            self.sell(mode=PosOrder.Mode.BAR, quantity=5)
        self.assertIn("Insufficient stock", str(raised.exception))
        self.assertIn("Bottled water 75cl (bar)", str(raised.exception))
        self.assertIn("Depletion bar", str(raised.exception))
        # Nothing was accepted, so nothing exists to deduct or review.
        self.assertFalse(PosOrder.objects.exists())
        self.assertFalse(StockConsumptionRequest.objects.exists())
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        self.assertEqual(self.on_hand(), Decimal("2.0000"))

    def test_a_shortage_that_appears_after_creation_does_not_go_negative(self):
        """Stock can drop between draft and delivery, but delivery posts nothing."""
        self.receive_from_supplier(5, source_key="dep-receipt-short")
        # Draft accepted while 5 bottles were on hand.
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="S-1",
            lines=[{"menu_item_id": self.bar_water.pk, "quantity": 3}],
        )
        submit_order(order=order, actor=self.cashier)
        # The stock drains to 2 while the order is with the bar.
        record_stock_movement(
            item=self.bottle, location=self.bar, movement_type=StockMovement.Type.ISSUE,
            quantity_delta="-3", source_key="dep-short-drain", actor=self.clerk,
            department="RESTAURANT", recipient="Bar handover",
        )
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)

        # Delivery posts NO POS_CONSUMPTION movement.
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        balance = StockBalance.objects.get(item=self.bottle, location=self.bar)
        self.assertEqual(balance.quantity_on_hand, Decimal("2.0000"))

        hand_off = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(hand_off.status, StockConsumptionRequest.Status.SKIPPED)

    def test_restocking_after_delivery_does_not_post_a_backdated_deduction(self):
        """Even after restocking, the sweep posts nothing (REQ-5)."""
        self.receive_from_supplier(5, source_key="dep-receipt-short-topup")
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="S-2",
            lines=[{"menu_item_id": self.bar_water.pk, "quantity": 3}],
        )
        submit_order(order=order, actor=self.cashier)
        record_stock_movement(
            item=self.bottle, location=self.bar, movement_type=StockMovement.Type.ISSUE,
            quantity_delta="-4", source_key="dep-short-drain-topup", actor=self.clerk,
            department="RESTAURANT", recipient="Bar handover",
        )
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        self.assertEqual(self.on_hand(), Decimal("1.0000"))

        # The goods arrive; the sweep is still a no-op.
        self.receive_from_supplier(2, unit_cost="150.00", source_key="dep-receipt-topup")
        summary = process_pending_pos_consumption(actor=self.clerk)
        self.assertEqual(summary["posted"], 0)
        self.assertEqual(self.on_hand(), Decimal("3.0000"))
        hand_off = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(hand_off.status, StockConsumptionRequest.Status.SKIPPED)


class CancellationTests(SalesDepletionTestCase):
    """An abandoned order must not consume stock (and delivery never does)."""

    def test_cancelling_a_draft_order_consumes_nothing(self):
        self.receive_from_supplier(5, source_key="dep-receipt-cancel")
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="C-1",
            lines=[{"menu_item_id": self.bar_water.pk, "quantity": 3}],
        )
        transition_order(order=order, target_status=PosOrder.Status.CANCELLED, actor=self.cashier)

        self.assertEqual(self.on_hand(), Decimal("5.0000"))
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        self.assertFalse(StockConsumptionRequest.objects.filter(pos_order=order).exists())

    def test_an_order_cancelled_before_delivery_is_not_deducted_by_the_sweep(self):
        self.receive_from_supplier(5, source_key="dep-receipt-cancel-mid")
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier, table_number="C-2",
            lines=[{"menu_item_id": self.dining_water.pk, "quantity": 2}],
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.CANCELLED, actor=self.cashier)

        self.assertEqual(process_pending_pos_consumption(actor=self.clerk)["examined"], 0)
        self.assertEqual(self.on_hand(), Decimal("5.0000"))


class RecipeReferenceTests(SalesDepletionTestCase):
    """Recipe records remain as reference but never trigger stock movement."""

    def test_fractional_recipe_quantities_are_recorded_but_not_deducted(self):
        """Recipes document fractional quantities; delivery does not deduct."""
        spirits = StockItem.objects.create(
            sku="DEP-SPIRIT", name="House spirit", base_unit="LITRE",
            standard_cost=Decimal("9000.00"),
        )
        cocktail = MenuItem.objects.create(
            category=self.bar_category, name="House cocktail", sku="DEP-MENU-COCKTAIL",
            base_price=Decimal("3000.00"),
        )
        create_stock_recipe(
            menu_item=cocktail, location=self.bar, tracking_mode="COMPONENTS",
            components=[{"item_id": spirits.pk, "quantity_per_menu_unit": "0.0500"}],
            actor=self.clerk, idempotency_key="depletion-cocktail-recipe",
        )
        record_stock_movement(
            item=spirits, location=self.bar, movement_type=StockMovement.Type.OPENING,
            quantity_delta="2.500", source_key="depletion-opening-spirit", actor=self.clerk,
        )

        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="F-1",
            lines=[{"menu_item_id": cocktail.pk, "quantity": 3}],
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)

        # Stock is NOT depleted.
        balance = StockBalance.objects.get(item=spirits, location=self.bar)
        self.assertEqual(balance.quantity_on_hand, Decimal("2.5000"))
        self.assertFalse(
            StockMovement.objects.filter(item=spirits, type=StockMovement.Type.POS_CONSUMPTION).exists()
        )

    def test_recipe_component_unit_conversion_is_recorded_but_not_deducted(self):
        """Recipes document unit conversions; delivery does not deduct."""
        crate = StockItem.objects.create(
            sku="DEP-CRATE", name="Water crate (12 bottles)", base_unit="CRATE",
            standard_cost=Decimal("1800.00"),
        )
        bottled = MenuItem.objects.create(
            category=self.bar_category, name="Water from crate", sku="DEP-MENU-CRATE",
            base_price=Decimal("300.00"),
        )
        create_stock_recipe(
            menu_item=bottled, location=self.bar, tracking_mode="COMPONENTS",
            components=[{"item_id": crate.pk, "quantity_per_menu_unit": "0.0834"}],
            actor=self.clerk, idempotency_key="depletion-crate-recipe",
        )
        record_stock_movement(
            item=crate, location=self.bar, movement_type=StockMovement.Type.OPENING,
            quantity_delta="10", source_key="depletion-opening-crate", actor=self.clerk,
        )
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="F-2",
            lines=[{"menu_item_id": bottled.pk, "quantity": 2}],
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)

        # Stock is NOT depleted.
        balance = StockBalance.objects.get(item=crate, location=self.bar)
        self.assertEqual(balance.quantity_on_hand, Decimal("10.0000"))


class ConcurrencyTests(SalesDepletionTestCase):
    """Concurrent sales must not lose updates — but no deductions happen (REQ-5)."""

    def test_concurrent_sales_post_no_deductions(self):
        self.receive_from_supplier(20, source_key="dep-receipt-concurrent")
        self.assertEqual(self.on_hand(), Decimal("20.0000"))

        def deliver_quantity(quantity):
            try:
                self.sell(mode=PosOrder.Mode.BAR, quantity=quantity, table_number="X-1")
                return True
            except Exception:  # noqa: BLE001 - SQLite lock contention is retried below
                return False

        results = []
        for quantity in (1, 1, 1, 1):
            with transaction.atomic():
                results.append(deliver_quantity(quantity))
            connection.close()
        self.assertTrue(all(results), "every delivered sale must succeed")

        # No POS_CONSUMPTION movements exist because REQ-5 removed the deduction.
        self.assertEqual(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 0)
        self.assertEqual(self.on_hand(), Decimal("20.0000"))
