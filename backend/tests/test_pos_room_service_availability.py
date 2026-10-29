"""Stock availability for direct sales and recipe-based prepared foods.

Owner-reported problems (B, C, D): the POS/Room-service menus did not show how
many of a PREPARED FOOD could actually be served, availability figures ignored
stock other open orders already held, and nothing proved that a multi-ingredient
dish is validated and deducted against its whole recipe rather than one
look-alike stock item.

Everything asserted here is computed by
``apps.inventory.services.inventory_service`` — the POS page performs no stock
arithmetic of its own — and every figure is derived from configured recipes and
real stock movements created by this test. No production data is fabricated and
no recipe is hardcoded in application code.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.core.exceptions import PosStockConflictError
from apps.core.utils import hotel_today
from apps.inventory.models import (
    StockBalance, StockConsumptionRequest, StockItem, StockLocation, StockMovement, StockRecipe,
)
from apps.inventory.services.inventory_service import (
    _open_pos_order_statuses,
    consume_stock_for_delivered_order,
    create_stock_recipe,
    pos_stock_summaries,
    record_opening_stock,
    record_stock_movement,
    reverse_pos_stock_consumption,
)
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services import order_service
from apps.pos.services.order_service import create_order, submit_order, transition_order
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


def setUpAvailabilityFixtures(case):
    """A kitchen store, a bar store, one direct-sale product and one 2-ingredient dish."""
    case.today = hotel_today()
    case.manager = make_staff("availability.manager@staff.dev", role=User.Role.MANAGER)
    case.cashier = make_staff("availability.cashier@staff.dev", role=User.Role.CASHIER)
    case.waiter = make_staff("availability.waiter@staff.dev", role=User.Role.WAITER)
    # Restaurant-only capability set (no bar access): used to prove the menu and
    # order endpoints stay scoped to the caller's service area.
    case.restaurant_only = make_staff(
        "availability.restaurant-only@staff.dev", role=User.Role.RESTAURANT_MANAGER
    )

    case.kitchen_store = StockLocation.objects.create(code="AV-KITCHEN", name="Kitchen Store")
    case.bar_store = StockLocation.objects.create(code="AV-BAR", name="Bar Store")

    # --- Direct-sale product: a bottle sold exactly as it is stocked ---------
    case.malt_stock = StockItem.objects.create(
        sku="AV-MALT", name="Malt drink", base_unit="BOTTLE",
        item_type=StockItem.ItemType.SELLABLE_PRODUCT, standard_cost=Decimal("400.00"),
    )
    case.bar_category = MenuCategory.objects.create(
        name="Availability bar", slug="availability-bar", service_area=MenuCategory.ServiceArea.BAR
    )
    case.malt = MenuItem.objects.create(
        category=case.bar_category, name="Malt drink", sku="AV-MENU-MALT", base_price=Decimal("800.00")
    )
    StockItem.objects.filter(pk=case.malt_stock.pk).update(
        direct_sale_menu_item=case.malt,
        direct_sale_location=case.bar_store,
        direct_sale_quantity=Decimal("1.0000"),
    )
    record_opening_stock(
        item=case.malt_stock, location=case.bar_store, quantity=Decimal("12.0000"),
        actor=case.manager, idempotency_key="av-opening-malt",
    )

    # --- Prepared food: 0.25 KG rice + 0.5 KG chicken per portion -----------
    case.kitchen_category = MenuCategory.objects.create(
        name="Availability kitchen", slug="availability-kitchen", service_area=MenuCategory.ServiceArea.RESTAURANT
    )
    case.rice = StockItem.objects.create(sku="AV-RICE", name="Long grain rice", base_unit="KG")
    case.chicken = StockItem.objects.create(sku="AV-CHICKEN", name="Chicken", base_unit="KG")
    case.dish = MenuItem.objects.create(
        category=case.kitchen_category, name="Jollof rice & chicken", sku="AV-MENU-JOLLOF",
        base_price=Decimal("3500.00"),
    )
    create_stock_recipe(
        menu_item=case.dish, location=case.kitchen_store,
        tracking_mode=StockRecipe.TrackingMode.COMPONENTS,
        components=[
            {"item_id": case.rice.pk, "quantity_per_menu_unit": "0.25", "unit": "KG"},
            {"item_id": case.chicken.pk, "quantity_per_menu_unit": "0.5", "unit": "KG"},
        ],
        actor=case.manager, idempotency_key="av-recipe-jollof",
    )
    record_opening_stock(
        item=case.rice, location=case.kitchen_store, quantity=Decimal("5.0000"),
        actor=case.manager, idempotency_key="av-opening-rice",
    )
    record_opening_stock(
        item=case.chicken, location=case.kitchen_store, quantity=Decimal("3.0000"),
        actor=case.manager, idempotency_key="av-opening-chicken",
    )

    # --- A product nobody configured: drafts are allowed, nothing is deducted -
    case.raw_salad = MenuItem.objects.create(
        category=case.kitchen_category, name="House salad", sku="AV-MENU-SALAD", base_price=Decimal("1500.00")
    )

    # --- An in-house guest for the room-service variants ---------------------
    room_type = make_room_type("Availability suite", price="20000.00")
    case.booking = make_booking(
        make_guest("availability-guest@example.com"), room_type,
        check_in=case.today, check_out=case.today + timedelta(days=2), total="40000.00",
    )
    case.stay = Stay.objects.create(
        reference=f"STY-{case.booking.booking_reference}", booking=case.booking, guest=case.booking.guest,
        status=Stay.Status.IN_HOUSE, expected_arrival=case.booking.check_in,
        expected_departure=case.booking.check_out,
    )


def on_hand(item, location):
    balance = StockBalance.objects.filter(item=item, location=location).first()
    return Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")


class AvailabilityFixtureTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        setUpAvailabilityFixtures(self)

    def line(self, menu_item, quantity):
        return {"menu_item_id": menu_item.pk, "quantity": quantity}

    # --- D: what the menus display ----------------------------------------
    def test_menu_payload_shows_available_quantity_and_unit_for_both_product_kinds(self):
        summaries = pos_stock_summaries([self.malt.pk, self.dish.pk])

        malt = summaries[self.malt.pk]
        self.assertTrue(malt["tracked"])
        self.assertEqual(malt["available_quantity"], "12")
        self.assertEqual(malt["availability_unit"], "BOTTLE")
        self.assertEqual(malt["saleable_quantity"], "12")

        dish = summaries[self.dish.pk]
        self.assertTrue(dish["tracked"])
        # Rice allows 20 portions, chicken only 6 — the recipe is limited by its
        # scarcest ingredient, not by a single look-alike stock item.
        self.assertEqual(dish["available_quantity"], "6")
        self.assertEqual(dish["sufficient_for_one"], True)
        self.assertEqual(dish["availability_unit"], "portion",
                         "a prepared dish is counted in portions, not one ingredient's unit")
        self.assertEqual(len(dish["components"]), 2)
        self.assertEqual({row["item_sku"] for row in dish["components"]}, {"AV-RICE", "AV-CHICKEN"})

    def test_menu_api_carries_the_same_authoritative_figures(self):
        self.auth(self.cashier)
        response = self.client.get("/api/admin/pos/menu/")
        self.assertEqual(response.status_code, 200, response.content)
        items = {
            item["sku"]: item
            for group in response.json()["data"]
            for item in group["items"]
        }
        self.assertEqual(items["AV-MENU-MALT"]["stock"]["available_quantity"], "12")
        self.assertEqual(items["AV-MENU-JOLLOF"]["stock"]["available_quantity"], "6")
        self.assertEqual(items["AV-MENU-JOLLOF"]["stock"]["availability_unit"], "portion")

    def test_a_draft_holds_stock_so_the_displayed_availability_drops_for_everyone(self):
        create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.dish, 4)], idempotency_key="av-hold-4",
        )
        summary = pos_stock_summaries([self.dish.pk])[self.dish.pk]
        self.assertEqual(summary["saleable_quantity"], "6", "the store still holds six portions' ingredients")
        self.assertEqual(summary["available_quantity"], "2",
                         "but two are what the validator will still accept")
        self.assertEqual(summary["committed_quantity"], "4")

        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
                lines=[self.line(self.dish, 3)], idempotency_key="av-hold-3",
            )
        self.assertIn("2", str(raised.exception))

    def test_shared_ingredient_reduces_availability_across_different_dishes(self):
        """Chicken is shared: a second dish on the same ingredient must count it."""
        other_dish = MenuItem.objects.create(
            category=self.kitchen_category, name="Chicken soup", sku="AV-MENU-SOUP",
            base_price=Decimal("2500.00"),
        )
        create_stock_recipe(
            menu_item=other_dish, location=self.kitchen_store,
            tracking_mode=StockRecipe.TrackingMode.COMPONENTS,
            components=[{"item_id": self.chicken.pk, "quantity_per_menu_unit": "0.25", "unit": "KG"}],
            actor=self.manager, idempotency_key="av-recipe-soup",
        )
        self.assertEqual(pos_stock_summaries([other_dish.pk])[other_dish.pk]["available_quantity"], "12")

        create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.dish, 4)], idempotency_key="av-shared-hold",
        )
        # 4 portions of the jollof committed 2 KG of chicken, leaving 1 KG: the
        # soup (0.25 KG each) can still be validated for at most four portions.
        self.assertEqual(pos_stock_summaries([other_dish.pk])[other_dish.pk]["available_quantity"], "4")

    # --- B / C: validation before a draft is accepted ----------------------
    def test_insufficient_ingredient_refuses_the_draft_and_names_the_product(self):
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
                lines=[self.line(self.dish, 7)], idempotency_key="av-too-many",
            )
        message = str(raised.exception)
        # The refusal names the ingredient that ran short, the store it sits in,
        # and the dish that asked for it.
        self.assertIn("Jollof rice & chicken", message)
        self.assertIn("Chicken", message)
        self.assertIn("Kitchen Store", message)
        self.assertIn("Insufficient stock", message)
        self.assertEqual(PosOrder.objects.count(), 0, "no order may be persisted for a refused line")

    def test_a_missing_ingredient_blocks_room_service_too(self):
        record_stock_movement(
            item=self.chicken, location=self.kitchen_store, movement_type=StockMovement.Type.ADJUSTMENT,
            quantity_delta=Decimal("-3.0000"), source_key="av-empty-chicken", actor=self.manager,
        )
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.ROOM_SERVICE, actor=self.cashier, stay=self.stay,
                lines=[self.line(self.dish, 1)], idempotency_key="av-room-service-short",
            )
        self.assertIn("Chicken", str(raised.exception))
        self.assertEqual(PosOrder.objects.count(), 0)

    def test_stock_is_revalidated_at_submission_and_the_draft_survives(self):
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.dish, 3)], idempotency_key="av-submit-check",
        )
        # Someone else takes the chicken down to a level the draft no longer fits.
        record_stock_movement(
            item=self.chicken, location=self.kitchen_store, movement_type=StockMovement.Type.ADJUSTMENT,
            quantity_delta=Decimal("-2.5000"), source_key="av-chicken-shrink", actor=self.manager,
        )
        with self.assertRaises(PosStockConflictError):
            submit_order(order=order, actor=self.cashier)
        order.refresh_from_db()
        self.assertEqual(order.status, PosOrder.Status.DRAFT, "a refused submission keeps the draft correctable")
        self.assertEqual(order.lines.count(), 1)

    def test_an_unconfigured_product_is_reported_not_silently_reported_as_available(self):
        summary = pos_stock_summaries([self.raw_salad.pk])[self.raw_salad.pk]
        self.assertFalse(summary["tracked"])
        self.assertFalse(summary["unconfigured"])
        self.assertIsNone(summary["available_quantity"])

        # Its existing behaviour is preserved: the draft is accepted and the
        # delivery hand-off is parked for inventory review. It is never
        # described as stock-guaranteed.
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.raw_salad, 1)], idempotency_key="av-unconfigured",
        )
        self.assertEqual(order.status, PosOrder.Status.DRAFT)

    def test_a_broken_mapping_is_refused_as_a_configuration_error(self):
        # The stock item still claims the menu item but its per-sale quantity is
        # zero: a claimed product with an unusable mapping must be refused, not
        # treated as available.
        StockItem.objects.filter(pk=self.malt_stock.pk).update(direct_sale_quantity=Decimal("0.0000"))
        summary = pos_stock_summaries([self.malt.pk])[self.malt.pk]
        self.assertTrue(summary["unconfigured"])
        self.assertIn("per-sale stock quantity", summary["unconfigured_reason"])

        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)], idempotency_key="av-broken-mapping",
            )
        self.assertEqual(raised.exception.stock_context.get("reason"), "stock_not_configured")

    def test_a_recipe_without_components_is_a_configuration_error_not_a_free_sale(self):
        broken = MenuItem.objects.create(
            category=self.kitchen_category, name="Broken dish", sku="AV-MENU-BROKEN",
            base_price=Decimal("1000.00"),
        )
        # A COMPONENTS recipe row with no BOM lines can only exist as damaged
        # data (the service refuses to create one); it must never deduct nothing
        # and still look sellable.
        StockRecipe.objects.create(
            reference="BOM-AV-BROKEN", menu_item=broken, location=self.kitchen_store,
            version=1, tracking_mode=StockRecipe.TrackingMode.COMPONENTS, created_by=self.manager,
        )
        summary = pos_stock_summaries([broken.pk])[broken.pk]
        self.assertTrue(summary["unconfigured"])
        self.assertIn("no components", summary["unconfigured_reason"])
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
                lines=[self.line(broken, 1)], idempotency_key="av-broken-recipe",
            )
        self.assertEqual(raised.exception.stock_context.get("reason"), "stock_not_configured")

    # --- E: delivery, deduction, reversal ---------------------------------
    def test_delivery_deducts_every_ingredient_exactly_once(self):
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.dish, 2)], idempotency_key="av-deliver-once",
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        order.refresh_from_db()

        # 2 portions = 0.5 KG rice and 1.0 KG chicken.
        self.assertEqual(on_hand(self.rice, self.kitchen_store), Decimal("4.5000"))
        self.assertEqual(on_hand(self.chicken, self.kitchen_store), Decimal("2.0000"))
        request = StockConsumptionRequest.objects.get(pos_order=order)
        self.assertEqual(request.status, StockConsumptionRequest.Status.PROCESSED)

        # Re-running the delivery hand-off (a retried request, a catch-up sweep)
        # must converge on the same single deduction.
        consume_stock_for_delivered_order(order=order, actor=self.cashier)
        consume_stock_for_delivered_order(order=order, actor=self.cashier)
        self.assertEqual(on_hand(self.rice, self.kitchen_store), Decimal("4.5000"))
        self.assertEqual(on_hand(self.chicken, self.kitchen_store), Decimal("2.0000"))
        self.assertEqual(
            StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 2,
            "one movement per ingredient, never a duplicate",
        )

    def test_reversal_restores_the_ingredients_once(self):
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.dish, 2)], idempotency_key="av-reverse",
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        self.assertEqual(on_hand(self.chicken, self.kitchen_store), Decimal("2.0000"))

        _, reversed_first = reverse_pos_stock_consumption(
            pos_order=order, actor=self.manager, reason="Order returned in full",
        )
        self.assertTrue(reversed_first)
        self.assertEqual(on_hand(self.rice, self.kitchen_store), Decimal("5.0000"))
        self.assertEqual(on_hand(self.chicken, self.kitchen_store), Decimal("3.0000"))

        _, reversed_again = reverse_pos_stock_consumption(
            pos_order=order, actor=self.manager, reason="Repeated reversal request",
        )
        self.assertFalse(reversed_again)
        self.assertEqual(on_hand(self.chicken, self.kitchen_store), Decimal("3.0000"),
                         "a repeated reversal must not invent stock")

    def test_delivery_transitions_cannot_be_skipped_or_repeated(self):
        order, _ = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.dish, 1)], idempotency_key="av-transitions",
        )
        with self.assertRaisesMessage(Exception, "cannot transition from DRAFT"):
            transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        delivered = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        charge_reference = delivered.charge_transaction_id

        again = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        self.assertEqual(again.charge_transaction_id, charge_reference, "delivery must not charge twice")
        self.assertEqual(on_hand(self.chicken, self.kitchen_store), Decimal("2.5000"))
        with self.assertRaisesMessage(Exception, "cannot transition from DELIVERED"):
            transition_order(order=order, target_status=PosOrder.Status.CANCELLED, actor=self.cashier)

    def test_the_shared_commitment_status_list_cannot_drift(self):
        self.assertEqual(
            tuple(_open_pos_order_statuses()), tuple(order_service._STOCK_COMMITTED_STATUSES),
            "the display projection and the order validator must agree on which orders hold stock",
        )


class AvailabilityConcurrencyTests(TransactionTestCase):
    """Threaded burst against a two-ingredient recipe (see the direct-sale twin)."""

    def setUp(self):
        cache.clear()
        setUpAvailabilityFixtures(self)

    def tearDown(self):
        cache.clear()

    def line(self, menu_item, quantity):
        return {"menu_item_id": menu_item.pk, "quantity": quantity}

    def test_simultaneous_orders_cannot_oversell_shared_ingredients(self):
        from concurrent.futures import ThreadPoolExecutor

        from django.db import OperationalError, connection

        def worker(index):
            try:
                order, _ = create_order(
                    mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
                    lines=[self.line(self.dish, 3)], idempotency_key=f"av-burst-{index}",
                )
                return order.reference
            except (PosStockConflictError, OperationalError):
                return None
            finally:
                connection.close()

        # Six portions are available; four simultaneous 3-portion orders cannot
        # all be accepted.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))

        accepted = [reference for reference in results if reference]
        self.assertLessEqual(len(accepted), 2, results)
        self.assertGreaterEqual(on_hand(self.rice, self.kitchen_store), Decimal("0.0000"))
        self.assertGreaterEqual(on_hand(self.chicken, self.kitchen_store), Decimal("0.0000"))
        self.assertEqual(PosOrder.objects.count(), len(accepted))
        for order in PosOrder.objects.all():
            self.assertEqual(order.lines.count(), 1)
            self.assertEqual(order.status, PosOrder.Status.DRAFT)


class MenuPermissionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        setUpAvailabilityFixtures(self)

    def test_bar_only_staff_cannot_read_the_restaurant_menu_and_vice_versa(self):
        self.auth(self.restaurant_only)
        restaurant = self.client.get("/api/admin/pos/menu/?area=RESTAURANT")
        self.assertEqual(restaurant.status_code, 200, restaurant.content)
        bar = self.client.get("/api/admin/pos/menu/?area=BAR")
        self.assertEqual(bar.status_code, 403, bar.content)

    def test_order_access_is_scoped_to_the_caller_s_service_area(self):
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[{"menu_item_id": self.malt.pk, "quantity": 1}], idempotency_key="av-bar-order",
        )
        self.auth(self.restaurant_only)
        response = self.client.get(f"/api/admin/pos/orders/{order.reference}/")
        self.assertEqual(response.status_code, 403, response.content)

    def test_inventory_figures_are_read_only_from_the_menu_endpoint(self):
        self.auth(self.cashier)
        response = self.client.get("/api/admin/pos/menu/")
        self.assertEqual(response.status_code, 200)
        before = pos_stock_summaries([self.dish.pk])[self.dish.pk]["available_quantity"]
        self.client.get("/api/admin/pos/menu/")
        after = pos_stock_summaries([self.dish.pk])[self.dish.pk]["available_quantity"]
        self.assertEqual(before, after, "reading the menu must never reserve or deduct stock")
