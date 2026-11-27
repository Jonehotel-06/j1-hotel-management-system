# tests/test_critical_repair_bar_store.py
"""Acceptance tests for the Bar Store stock-deduction repair (2026-11-26).

Each test corresponds to one of the required scenarios in the repair brief:

===========  ==========================================================
Test A       Main 100 -> issue 30 to the Bar Store -> 70 / 30
Test B       Bar POS sells 5 -> Bar 25, Main still 70
Test C       Bar POS tries to sell more than the Bar Store holds ->
             refused, Main untouched
Test D       the same delivery/completion event submitted twice ->
             exactly one deduction
Test E       restaurant POS behaviour is unchanged for unconfigured
             products (no invented deduction)
Test G       issuing more stock to the Bar Store moves both balances
             by the issued quantity only
Test H       concurrent Bar POS sales never drive the store negative
             and never double-deduct
Extra        a bar product wired to deduct from the Main Store is
             refused with the required store named (never silently
             drained from Main Store)
===========  ==========================================================
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.core.exceptions import PosStockConflictError
from apps.inventory.models import (
    StockBalance, StockConsumptionRequest, StockItem, StockLocation, StockMovement, StockRecipe,
)
from apps.inventory.services.inventory_service import (
    consume_stock_for_delivered_order,
    process_pos_stock_consumption,
    record_stock_movement,
    request_pos_stock_consumption,
    transfer_stock,
)
from apps.pos.models import MenuCategory, MenuItem, PosOrder, PosOrderLine
from apps.pos.services.order_service import create_order, submit_order, transition_order
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


def _on_hand(item, location):
    balance = StockBalance.objects.filter(item=item, location=location).first()
    return Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")


class BarStoreDeductionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from apps.core.utils import hotel_today

        self.today = hotel_today()
        self.cashier = make_staff("bar-store-cashier@staff.dev", role=User.Role.CASHIER)
        self.keeper = make_staff("bar-store-keeper@staff.dev", role=User.Role.INVENTORY_CLERK)

        self.main_store = StockLocation.objects.create(code="TEST-MAIN", name="Main Store")
        self.bar_store = StockLocation.objects.create(code="TEST-BAR", name="Bar Store")

        self.bar_category = MenuCategory.objects.create(
            name="Repair bar", slug="repair-bar", service_area=MenuCategory.ServiceArea.BAR,
        )
        self.malt = MenuItem.objects.create(
            category=self.bar_category, name="Malt Drink", sku="REPAIR-MALT", base_price=Decimal("500.00"),
        )
        self.malt_stock = StockItem.objects.create(
            sku="REPAIR-MALT-STOCK", name="Malt Drink bottle", base_unit="BOTTLE",
            item_type=StockItem.ItemType.SELLABLE_PRODUCT,
            direct_sale_menu_item=self.malt, direct_sale_quantity=Decimal("1"),
            direct_sale_location=self.bar_store,
        )

        self.restaurant_category = MenuCategory.objects.create(
            name="Repair restaurant", slug="repair-restaurant",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.jollof = MenuItem.objects.create(
            category=self.restaurant_category, name="Jollof Rice", sku="REPAIR-JOLLOF",
            base_price=Decimal("2500.00"),
        )

        room_type = make_room_type("Repair suite", price="20000.00")
        booking = make_booking(
            make_guest("repair-guest@example.com"), room_type,
            check_in=self.today, check_out=self.today + timedelta(days=2), total="40000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
            expected_departure=booking.check_out,
        )

    # ------------------------------------------------------------- helpers

    def open_stock(self, item, location, quantity, key):
        return record_stock_movement(
            item=item, location=location, movement_type=StockMovement.Type.OPENING,
            quantity_delta=quantity, source_key=key, actor=self.keeper,
        )

    def issue_to_bar(self, quantity, key):
        return transfer_stock(
            item=self.malt_stock, from_location=self.main_store, to_location=self.bar_store,
            quantity=quantity, actor=self.keeper, transfer_key=key, notes="Issue to bar",
        )

    def sell(self, quantity, *, key_suffix=""):
        order, _created = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[{"menu_item_id": self.malt.pk, "quantity": quantity}],
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        return order

    def deliver_without_auto_processing(self, quantity, reference=None):
        """A delivered order whose hand-off has NOT yet been resolved."""
        order = PosOrder.objects.create(
            reference=reference or f"POS-MANUAL-{PosOrder.objects.count() + 1}",
            mode=PosOrder.Mode.BAR, created_by=self.cashier, status=PosOrder.Status.DRAFT,
        )
        PosOrderLine.objects.create(
            order=order, menu_item=self.malt, item_name=self.malt.name, item_sku=self.malt.sku,
            unit_price=self.malt.base_price, quantity=quantity,
            line_total=self.malt.base_price * quantity,
        )
        order.status = PosOrder.Status.DELIVERED
        order.save(update_fields=["status", "updated_at"])
        return order

    # --------------------------------------------------------------- tests

    def test_A_issue_from_main_store_to_bar_store_moves_both_balances(self):
        self.open_stock(self.malt_stock, self.main_store, "100", "repair-a-main")
        self.issue_to_bar("30", "repair-a-issue-30")

        self.assertEqual(_on_hand(self.malt_stock, self.main_store), Decimal("70.0000"))
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("30.0000"))
        # The transfer is two traceable legs, not a purchase or a sale.
        types = sorted(StockMovement.objects.filter(item=self.malt_stock).values_list("type", flat=True))
        self.assertIn(StockMovement.Type.TRANSFER_OUT, types)
        self.assertIn(StockMovement.Type.TRANSFER_IN, types)

    def test_B_bar_pos_sale_deducts_bar_store_only(self):
        self.open_stock(self.malt_stock, self.main_store, "100", "repair-b-main")
        self.issue_to_bar("30", "repair-b-issue")
        self.sell(5)

        self.assertEqual(_on_hand(self.malt_stock, self.main_store), Decimal("70.0000"))
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("25.0000"))
        movements = StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION)
        self.assertEqual(movements.count(), 1)
        movement = movements.get()
        self.assertEqual(movement.location_id, self.bar_store.pk)
        self.assertEqual(movement.quantity_delta, Decimal("-5.0000"))
        # Main Store is never touched by the sale.
        self.assertFalse(
            StockMovement.objects.filter(
                type=StockMovement.Type.POS_CONSUMPTION, location=self.main_store,
            ).exists()
        )

    def test_C_selling_more_than_the_bar_store_holds_is_refused(self):
        self.open_stock(self.malt_stock, self.main_store, "100", "repair-c-main")
        self.issue_to_bar("30", "repair-c-issue")
        # 5 bottles are sold first, leaving 25 in the Bar Store.
        self.sell(5)
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("25.0000"))
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[{"menu_item_id": self.malt.pk, "quantity": 26}],
            )
        self.assertIn("Bar Store", str(raised.exception))
        self.assertEqual(_on_hand(self.malt_stock, self.main_store), Decimal("70.0000"))
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("25.0000"))
        self.assertEqual(PosOrder.objects.count(), 1)  # only the one completed sale

    def test_D_repeated_completion_never_deducts_twice(self):
        self.open_stock(self.malt_stock, self.main_store, "100", "repair-d-main")
        self.issue_to_bar("30", "repair-d-issue")
        order = self.deliver_without_auto_processing(5)
        first, _ = consume_stock_for_delivered_order(order=order, actor=self.cashier)
        self.assertEqual(first.status, StockConsumptionRequest.Status.PROCESSED)
        second, created = consume_stock_for_delivered_order(order=order, actor=self.cashier)
        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        # A fresh hand-off lookup through the console path is idempotent too.
        replayed, replayed_created = process_pos_stock_consumption(request=first, actor=self.cashier)
        self.assertFalse(replayed_created)
        self.assertEqual(replayed.pk, first.pk)
        self.assertEqual(
            StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 1,
        )
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("25.0000"))

    def test_E_restaurant_sale_of_an_unconfigured_product_deducts_nothing(self):
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[{"menu_item_id": self.jollof.pk, "quantity": 1}],
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        hand_off = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        # The gap is stated, never invented: nothing is deducted, and the
        # hand-off says why (an explicitly non-stock product, or a product with
        # no configured deduction — never a guessed quantity).
        self.assertEqual(hand_off.status, StockConsumptionRequest.Status.SKIPPED)
        self.assertTrue(
            "non-stock" in hand_off.resolution_note or "No stock deduction is configured" in hand_off.resolution_note,
            hand_off.resolution_note,
        )

    def test_G_additional_issue_increases_only_the_destination_by_the_issued_quantity(self):
        self.open_stock(self.malt_stock, self.main_store, "100", "repair-g-main")
        self.issue_to_bar("30", "repair-g-issue-1")
        self.sell(5)
        self.issue_to_bar("10", "repair-g-issue-2")
        self.assertEqual(_on_hand(self.malt_stock, self.main_store), Decimal("60.0000"))
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("35.0000"))
        self.assertEqual(
            StockMovement.objects.filter(type=StockMovement.Type.TRANSFER_IN).count(), 2,
        )
        # Each transfer is recorded exactly once (source-keyed).
        self.assertEqual(
            StockMovement.objects.filter(source_key="transfer:repair-g-issue-2:in").count(), 1,
        )

    def test_bar_product_configured_against_main_store_is_refused_not_drained(self):
        """A bar item wired to Main Store must not silently consume Main Store."""
        self.malt_stock.direct_sale_location = self.main_store
        self.malt_stock.save(update_fields=["direct_sale_location", "updated_at"])
        self.open_stock(self.malt_stock, self.main_store, "100", "repair-wrong-main")
        self.open_stock(self.malt_stock, self.bar_store, "10", "repair-wrong-bar")

        order = self.deliver_without_auto_processing(2)
        request, _ = request_pos_stock_consumption(order=order, actor=self.cashier)
        resolved, created = process_pos_stock_consumption(request=request, actor=self.cashier)
        self.assertFalse(created)
        self.assertEqual(resolved.status, StockConsumptionRequest.Status.FAILED)
        self.assertIn("Main Store", resolved.resolution_note)
        self.assertIn("Bar Store", resolved.resolution_note)
        # Nothing moved anywhere.
        self.assertFalse(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).exists())
        self.assertEqual(_on_hand(self.malt_stock, self.main_store), Decimal("100.0000"))
        self.assertEqual(_on_hand(self.malt_stock, self.bar_store), Decimal("10.0000"))


class BarStoreConcurrencyTests(TransactionTestCase):
    """Test H: concurrent Bar POS sales cannot oversell or double-deduct."""

    reset_sequences = True

    def setUp(self):
        cache.clear()
        from apps.core.utils import hotel_today

        self.today = hotel_today()
        self.cashier = make_staff("bar-concurrent@staff.dev", role=User.Role.CASHIER)
        self.main_store = StockLocation.objects.create(code="CONC-MAIN", name="Main Store CONC")
        self.bar_store = StockLocation.objects.create(code="CONC-BAR", name="Bar Store CONC")
        category = MenuCategory.objects.create(
            name="Concurrency bar", slug="concurrency-bar", service_area=MenuCategory.ServiceArea.BAR,
        )
        self.malt = MenuItem.objects.create(
            category=category, name="Concurrency Malt", sku="CONC-MALT", base_price=Decimal("500.00"),
        )
        self.malt_stock = StockItem.objects.create(
            sku="CONC-MALT-STOCK", name="Concurrency malt bottle", base_unit="BOTTLE",
            item_type=StockItem.ItemType.SELLABLE_PRODUCT,
            direct_sale_menu_item=self.malt, direct_sale_quantity=Decimal("1"),
            direct_sale_location=self.bar_store,
        )
        record_stock_movement(
            item=self.malt_stock, location=self.bar_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="6", source_key="conc-opening", actor=self.cashier,
        )

    def _deliver(self, quantity, index):
        order = PosOrder.objects.create(
            reference=f"POS-CONC-{index}", mode=PosOrder.Mode.BAR, created_by=self.cashier,
            status=PosOrder.Status.DRAFT,
        )
        PosOrderLine.objects.create(
            order=order, menu_item=self.malt, item_name=self.malt.name, item_sku=self.malt.sku,
            unit_price=Decimal("500.00"), quantity=quantity, line_total=Decimal("500.00") * quantity,
        )
        order.status = PosOrder.Status.DELIVERED
        order.save(update_fields=["status", "updated_at"])
        return order

    def test_simultaneous_bar_sales_never_oversell_the_store(self):
        orders = [self._deliver(4, 1), self._deliver(4, 2)]

        def resolve(order):
            # A worker that loses SQLite's single writer race raises
            # OperationalError ("database table is locked"). On MySQL the row
            # locks make it wait instead; both outcomes are safe, so the
            # invariant below is asserted for every interleaving.
            from django.db import OperationalError

            try:
                return consume_stock_for_delivered_order(order=order, actor=self.cashier)[0].status
            except OperationalError:
                return "LOCKED"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(resolve, orders))

        self.assertIn(StockConsumptionRequest.Status.PROCESSED, statuses)
        self.assertNotIn(StockConsumptionRequest.Status.PROCESSED, statuses[1:]
                         if statuses[0] == StockConsumptionRequest.Status.PROCESSED else statuses[:0])
        on_hand = _on_hand(self.malt_stock, self.bar_store)
        self.assertGreaterEqual(on_hand, Decimal("0.0000"))
        self.assertEqual(on_hand, Decimal("2.0000"))
        self.assertEqual(
            StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 1,
        )
        self.assertEqual(
            StockMovement.objects.get(type=StockMovement.Type.POS_CONSUMPTION).quantity_delta,
            Decimal("-4.0000"),
        )
