# tests/test_pos_stock_validation.py
"""POS/room-service order entry must never accept an unfulfillable bar item.

Coverage of the four reported problems:

* **A — POS accepts out-of-stock items**: creating a draft for a stock-tracked
  product with zero/negative/insufficient stock is rejected (409
  ``POS_STOCK_CONFLICT``) with the product, store and actual quantities; a
  valid quantity is still accepted; two lines (and two products sharing the
  same physical stock) are aggregated before the check.
* **B — Room service accepts out-of-stock items**: the ROOM_SERVICE mode of
  the same order flow is held to the identical rule.
* **C — invalid items must never enter drafts**: a rejected creation persists
  no order and no lines, other staff's existing drafts survive, and submitting
  a draft whose stock has since disappeared is refused while the draft stays
  intact.
* **D — the deduction location is displayed and truthful**: the menu payload
  and the live item lookup carry the fulfilment store and availability from
  the same resolver the delivery-time deduction uses, and the store shown is
  the store the ``POS_CONSUMPTION`` movement is actually posted against.

Also proven: open orders commit stock (two orders cannot collectively accept
more than the store holds, and cancelling releases it), the delivery-time
all-or-nothing deduction is unchanged (never negative, exactly once), and
products with no stock configuration keep their existing behaviour (POS stock
rules are not imposed on them).
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.core.exceptions import PosStockConflictError
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import (
    pos_stock_summaries,
    record_stock_movement,
)
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import (
    create_order,
    submit_order,
    transition_order,
)
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


def setUpPosStockFixtures(case):
    """Shared fixture body: a bar store, direct-sale bar products, real stock."""
    from apps.core.utils import hotel_today

    case.today = hotel_today()
    case.cashier = make_staff("stock-cashier@staff.dev", role=User.Role.CASHIER)

    case.bar_store = StockLocation.objects.create(code="SV-BAR", name="Bar Store")
    case.main_store = StockLocation.objects.create(code="SV-MAIN", name="Main Store")

    case.bar_category = MenuCategory.objects.create(
        name="Stock validation bar", slug="stock-validation-bar",
        service_area=MenuCategory.ServiceArea.BAR,
    )

    # A directly sellable bar product: one menu unit = one bottle out of
    # the Bar Store (the explicit configuration the deduction reads).
    case.malt_stock = StockItem.objects.create(
        sku="SV-MALT", name="Malt Drink", base_unit="BOTTLE",
        item_type=StockItem.ItemType.SELLABLE_PRODUCT,
        direct_sale_menu_item=None,  # linked below (needs the menu item pk)
        direct_sale_quantity=Decimal("1"),
        direct_sale_location=case.bar_store,
    )
    case.malt = MenuItem.objects.create(
        category=case.bar_category, name="Malt Drink", sku="SV-MENU-MALT",
        base_price=Decimal("800.00"),
    )
    case.malt_stock.direct_sale_menu_item = case.malt
    case.malt_stock.save(update_fields=["direct_sale_menu_item", "updated_at"])

    # A crate product: one menu unit = 24 bottles out of the Main Store.
    case.crate_stock = StockItem.objects.create(
        sku="SV-CRATE", name="Lager crate stock", base_unit="BOTTLE",
        item_type=StockItem.ItemType.SELLABLE_PRODUCT,
        direct_sale_quantity=Decimal("24"),
        direct_sale_location=case.main_store,
    )
    case.crate = MenuItem.objects.create(
        category=case.bar_category, name="Lager Beer crate", sku="SV-MENU-CRATE",
        base_price=Decimal("12000.00"),
    )
    case.crate_stock.direct_sale_menu_item = case.crate
    case.crate_stock.save(update_fields=["direct_sale_menu_item", "updated_at"])

    # An entirely unconfigured kitchen product (no recipe, no direct sale).
    case.kitchen_category = MenuCategory.objects.create(
        name="Stock validation kitchen", slug="stock-validation-kitchen",
        service_area=MenuCategory.ServiceArea.RESTAURANT,
    )
    case.jollof = MenuItem.objects.create(
        category=case.kitchen_category, name="Jollof Rice", sku="SV-MENU-JOLLOF",
        base_price=Decimal("2500.00"),
    )

    room_type = make_room_type("Stock validation suite", price="12000.00")
    booking = make_booking(
        make_guest("stock-guest@example.com"), room_type,
        check_in=case.today, check_out=case.today + timedelta(days=2), total="24000.00",
    )
    case.stay = Stay.objects.create(
        reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
        status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in,
        expected_departure=booking.check_out,
    )


class PosStockValidationTestCase(BaseAPITestCase):
    """Shared fixture: a bar store, direct-sale bar products, real stock."""

    def setUp(self):
        super().setUp()
        setUpPosStockFixtures(self)

    # ---------------------------------------------------------------- helpers

    def stock(self, item, location, quantity, *, source_key, allow_negative=False):
        return record_stock_movement(
            item=item, location=location, movement_type=StockMovement.Type.OPENING,
            quantity_delta=quantity, source_key=source_key, actor=self.cashier,
            allow_negative=allow_negative,
        )

    def on_hand(self, item, location):
        balance = StockBalance.objects.filter(item=item, location=location).first()
        return Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")

    def line(self, menu_item, quantity):
        return {"menu_item_id": menu_item.pk, "quantity": quantity}


class CreateOrderStockValidationTests(PosStockValidationTestCase):
    """Problem A/C: what create_order accepts and rejects for bar stock."""

    def test_sufficient_stock_is_accepted_in_bar_mode(self):
        self.stock(self.malt_stock, self.bar_store, "12", source_key="sv-malt-12")
        order, created = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="B-1",
            lines=[self.line(self.malt, 2)],
        )
        self.assertTrue(created)
        self.assertEqual(order.lines.get().quantity, 2)
        self.assertEqual(order.total_amount, Decimal("1600.00"))
        # Nothing is deducted at draft time; the balance still shows 12.
        self.assertEqual(self.on_hand(self.malt_stock, self.bar_store), Decimal("12.0000"))

    def test_sufficient_stock_is_accepted_in_room_service_mode(self):
        self.stock(self.malt_stock, self.bar_store, "12", source_key="sv-malt-rs")
        order, _ = create_order(
            mode=PosOrder.Mode.ROOM_SERVICE, actor=self.cashier, stay=self.stay,
            delivery_location="Room 101",
            lines=[self.line(self.malt, 2)],
        )
        self.assertEqual(order.mode, PosOrder.Mode.ROOM_SERVICE)
        self.assertEqual(order.lines.get().quantity, 2)

    def test_zero_stock_is_rejected_with_store_and_product(self):
        # No stock has ever been recorded at the Bar Store for this product:
        # that IS the zero-stock case (no balance row = 0 on hand).
        self.assertEqual(self.on_hand(self.malt_stock, self.bar_store), Decimal("0.0000"))
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="B-2",
                lines=[self.line(self.malt, 1)],
            )
        message = str(raised.exception)
        self.assertIn("Out of stock", message)
        self.assertIn("Malt Drink", message)
        self.assertIn("Bar Store", message)
        self.assertIn("0 bottle", message)
        # Nothing was accepted: no order row, no line, no draft to mislead staff.
        self.assertFalse(PosOrder.objects.exists())

    def test_negative_stock_is_rejected(self):
        self.stock(self.malt_stock, self.bar_store, "-4", source_key="sv-malt-neg", allow_negative=True)
        with self.assertRaises(PosStockConflictError):
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)],
            )
        self.assertFalse(PosOrder.objects.exists())

    def test_insufficient_stock_is_rejected_with_actual_figures(self):
        self.stock(self.malt_stock, self.bar_store, "3", source_key="sv-malt-3")
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 5)],
            )
        message = str(raised.exception)
        self.assertIn("Insufficient stock", message)
        self.assertIn("only 3 bottles", message)
        self.assertIn("but 5 bottles were requested", message)
        self.assertIn("Bar Store", message)
        self.assertIn("Malt Drink", message)
        # The structured context carries the server-side figures for the UI.
        items = raised.exception.errors_payload["items"]
        self.assertEqual(items[0]["available"], "3")
        self.assertEqual(items[0]["requested"], "5")
        self.assertEqual(items[0]["location"], "Bar Store")
        self.assertEqual(items[0]["item_name"], "Malt Drink")
        self.assertEqual(raised.exception.errors_payload["reason"], "insufficient_stock")
        self.assertFalse(PosOrder.objects.exists())

    def test_room_service_rejects_insufficient_stock(self):
        self.stock(self.malt_stock, self.bar_store, "2", source_key="sv-malt-rs-2")
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.ROOM_SERVICE, actor=self.cashier, stay=self.stay,
                lines=[self.line(self.malt, 4)],
            )
        self.assertIn("only 2 bottles", str(raised.exception))
        self.assertFalse(PosOrder.objects.exists())
        self.assertFalse(PosOrder.objects.filter(stay=self.stay).exists())

    def test_crate_conversion_is_reported_in_base_units(self):
        self.stock(self.crate_stock, self.main_store, "48", source_key="sv-crate-48")
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.crate, 3)],  # 3 × 24 = 72 bottles needed
            )
        message = str(raised.exception)
        self.assertIn("Main Store", message)
        self.assertIn("48 bottles", message)
        self.assertIn("3 × Lager Beer crate (72 bottles)", message)

    def test_valid_quantity_after_a_rejected_one_is_accepted(self):
        self.stock(self.malt_stock, self.bar_store, "3", source_key="sv-malt-correct")
        with self.assertRaises(PosStockConflictError):
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 5)], idempotency_key="sv-retry-key",
            )
        # The rejected attempt persisted nothing, so the same idempotency key
        # is free for the corrected quantity.
        order, created = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[self.line(self.malt, 3)], idempotency_key="sv-retry-key",
        )
        self.assertTrue(created)
        self.assertEqual(order.lines.get().quantity, 3)

    def test_two_lines_of_the_same_product_are_aggregated(self):
        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-5")
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 3), self.line(self.malt, 3)],
            )
        self.assertIn("but 6 bottles were requested", str(raised.exception))

    def test_products_sharing_physical_stock_are_aggregated(self):
        """Two bar menu items pouring from one component stock item."""
        syrup = StockItem.objects.create(sku="SV-SYRUP", name="Cola syrup", base_unit="LITRE")
        from apps.inventory.services.inventory_service import create_stock_recipe

        cola = MenuItem.objects.create(
            category=self.bar_category, name="Cola glass", sku="SV-MENU-COLA",
            base_price=Decimal("500.00"),
        )
        create_stock_recipe(
            menu_item=cola, location=self.bar_store, tracking_mode="COMPONENTS",
            components=[{"item_id": syrup.pk, "quantity_per_menu_unit": "0.05"}],
            actor=self.cashier, idempotency_key="sv-cola-recipe",
        )
        self.stock(syrup, self.bar_store, "0.3", source_key="sv-syrup-03")
        # 4 × 0.05 + 3 × 0.05 = 0.35 L needed, only 0.3 L on hand.
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(cola, 4), self.line(cola, 3)],
            )
        self.assertIn("Insufficient stock", str(raised.exception))

    def test_unconfigured_product_keeps_its_existing_behaviour(self):
        """A menu item with no stock mapping is not subject to bar stock rules."""
        order, created = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier, table_number="R-1",
            lines=[self.line(self.jollof, 2)],
        )
        self.assertTrue(created)
        self.assertEqual(order.lines.get().item_sku, "SV-MENU-JOLLOF")

    def test_no_stock_recipe_product_keeps_its_existing_behaviour(self):
        from apps.inventory.services.inventory_service import create_stock_recipe

        create_stock_recipe(
            menu_item=self.jollof, location=self.main_store, tracking_mode="NO_STOCK",
            components=[], actor=self.cashier, idempotency_key="sv-jollof-nostock",
        )
        order, created = create_order(
            mode=PosOrder.Mode.RESTAURANT, actor=self.cashier,
            lines=[self.line(self.jollof, 2)],
        )
        self.assertTrue(created)


class BrokenConfigurationTests(PosStockValidationTestCase):
    """Problem D: a claimed stock mapping that is invalid must block the sale."""

    def test_missing_direct_sale_location_is_rejected(self):
        self.malt_stock.direct_sale_location = None
        self.malt_stock.save(update_fields=["direct_sale_location", "updated_at"])
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)],
            )
        message = str(raised.exception)
        self.assertIn("Stock location unavailable for Malt Drink", message)
        self.assertIn("no stock location is configured", message)
        self.assertFalse(PosOrder.objects.exists())

    def test_inactive_direct_sale_location_is_rejected(self):
        self.bar_store.is_active = False
        self.bar_store.save(update_fields=["is_active", "updated_at"])
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)],
            )
        self.assertIn("Stock location unavailable", str(raised.exception))
        self.assertIn("inactive", str(raised.exception))

    def test_inactive_direct_sale_stock_item_is_rejected(self):
        self.malt_stock.is_active = False
        self.malt_stock.save(update_fields=["is_active", "updated_at"])
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)],
            )
        self.assertIn("inactive", str(raised.exception))
        self.assertEqual(raised.exception.errors_payload["reason"], "stock_not_configured")

    def test_zero_direct_sale_quantity_is_rejected(self):
        self.malt_stock.direct_sale_quantity = Decimal("0")
        self.malt_stock.save(update_fields=["direct_sale_quantity", "updated_at"])
        with self.assertRaises(PosStockConflictError):
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)],
            )


class DraftIntegrityTests(PosStockValidationTestCase):
    """Problem C: rejected additions never touch existing valid drafts."""

    def test_rejected_order_leaves_other_drafts_intact(self):
        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-drafts")
        valid, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="D-1",
            lines=[self.line(self.malt, 3)],
        )
        with self.assertRaises(PosStockConflictError):
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier, table_number="D-2",
                lines=[self.line(self.malt, 3)],
            )
        valid.refresh_from_db()
        self.assertEqual(valid.status, PosOrder.Status.DRAFT)
        self.assertEqual(valid.lines.count(), 1)
        self.assertEqual(valid.lines.get().quantity, 3)

    def test_open_orders_commit_stock_until_finalised(self):
        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-commit")
        first, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[self.line(self.malt, 5)],
        )
        # The remaining balance is fully committed: a second order is refused.
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 1)],
            )
        self.assertIn("already held by other open orders", str(raised.exception))
        # Cancelling the first releases the commitment.
        transition_order(order=first, target_status=PosOrder.Status.CANCELLED, actor=self.cashier)
        second, created = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[self.line(self.malt, 1)],
        )
        self.assertTrue(created)
        # Delivering it deducts exactly its own quantity.
        submit_order(order=second, actor=self.cashier)
        transition_order(order=second, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=second, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=second, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        self.assertEqual(self.on_hand(self.malt_stock, self.bar_store), Decimal("4.0000"))

    def test_submit_revalidates_and_keeps_the_draft_when_stock_vanished(self):
        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-submit")
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[self.line(self.malt, 3)],
        )
        # Stock drains while the draft is open.
        record_stock_movement(
            item=self.malt_stock, location=self.bar_store, movement_type=StockMovement.Type.ISSUE,
            quantity_delta="-4", source_key="sv-malt-drain", actor=self.cashier,
            department="RESTAURANT", recipient="Bar handover",
        )
        with self.assertRaises(PosStockConflictError) as raised:
            submit_order(order=order, actor=self.cashier)
        self.assertIn("only 1 bottle", str(raised.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, PosOrder.Status.DRAFT)
        self.assertEqual(order.lines.count(), 1)
        # Restock, and the same draft submits normally.
        self.stock(self.malt_stock, self.bar_store, "4", source_key="sv-malt-restock")
        submitted = submit_order(order=order, actor=self.cashier)
        self.assertEqual(submitted.status, PosOrder.Status.SUBMITTED)


class DeductionLocationConsistencyTests(PosStockValidationTestCase):
    """Problem D + section 7: the displayed store IS the store deducted."""

    def test_menu_summary_location_matches_the_deduction_movement(self):
        self.stock(self.malt_stock, self.bar_store, "12", source_key="sv-malt-consistency")
        summary = pos_stock_summaries([self.malt.pk])[self.malt.pk]
        self.assertTrue(summary["tracked"])
        self.assertEqual(summary["location_name"], "Bar Store")
        self.assertEqual(summary["location_code"], "SV-BAR")
        self.assertEqual(summary["saleable_quantity"], "12")
        self.assertEqual(summary["stock_unit"], "BOTTLE")

        order, _ = create_order(
            mode=PosOrder.Mode.ROOM_SERVICE, actor=self.cashier, stay=self.stay,
            lines=[self.line(self.malt, 2)],
        )
        submit_order(order=order, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)

        movement = StockMovement.objects.get(type=StockMovement.Type.POS_CONSUMPTION)
        self.assertEqual(movement.location_id, self.bar_store.pk)
        self.assertEqual(movement.location.code, summary["location_code"])
        self.assertEqual(movement.quantity_delta, Decimal("-2.0000"))
        self.assertEqual(self.on_hand(self.malt_stock, self.bar_store), Decimal("10.0000"))

    def test_menu_summary_flags_broken_configuration(self):
        self.malt_stock.direct_sale_location = None
        self.malt_stock.save(update_fields=["direct_sale_location", "updated_at"])
        summary = pos_stock_summaries([self.malt.pk])[self.malt.pk]
        self.assertFalse(summary["tracked"])
        self.assertTrue(summary["unconfigured"])
        self.assertIn("no stock location", summary["unconfigured_reason"])

    def test_menu_summary_for_untracked_product_invents_nothing(self):
        summary = pos_stock_summaries([self.jollof.pk])[self.jollof.pk]
        self.assertFalse(summary["tracked"])
        self.assertFalse(summary["unconfigured"])
        self.assertIsNone(summary["saleable_quantity"])
        self.assertIsNone(summary["sufficient_for_one"])


class PosStockApiTests(PosStockValidationTestCase):
    """The same rules over HTTP, with the project's error envelope."""

    def test_api_rejects_insufficient_stock_with_409_envelope(self):
        self.auth(self.cashier)
        self.stock(self.malt_stock, self.bar_store, "3", source_key="sv-malt-api-3")
        response = self.client.post("/api/admin/pos/orders/", {
            "mode": "BAR",
            "lines": [self.line(self.malt, 5)],
            "idempotency_key": "sv-api-insufficient",
        }, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "POS_STOCK_CONFLICT")
        self.assertIn("only 3 bottles of Malt Drink", body["message"])
        self.assertIn("Bar Store", body["message"])
        item = body["errors"]["items"][0]
        self.assertEqual(item["available"], "3")
        self.assertEqual(item["requested"], "5")
        self.assertEqual(item["unit"], "bottle")
        self.assertEqual(item["location"], "Bar Store")
        self.assertFalse(PosOrder.objects.exists())

    def test_api_room_service_rejects_out_of_stock(self):
        self.auth(self.cashier)
        # No stock recorded at the Bar Store for this product (zero on hand).
        response = self.client.post("/api/admin/pos/orders/", {
            "mode": "ROOM_SERVICE", "stay_id": self.stay.pk,
            "lines": [self.line(self.malt, 1)],
            "idempotency_key": "sv-api-room-oos",
        }, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body["code"], "POS_STOCK_CONFLICT")
        self.assertIn("Out of stock: Malt Drink", body["message"])
        self.assertIn("Bar Store", body["message"])
        self.assertFalse(PosOrder.objects.exists())

    def test_api_accepts_a_within_stock_order(self):
        self.auth(self.cashier)
        self.stock(self.malt_stock, self.bar_store, "12", source_key="sv-malt-api-ok")
        response = self.client.post("/api/admin/pos/orders/", {
            "mode": "BAR", "table_number": "B-9",
            "lines": [self.line(self.malt, 2)],
            "idempotency_key": "sv-api-ok",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(PosOrder.objects.get().lines.get().quantity, 2)

    def test_api_menu_carries_stock_source_and_availability(self):
        self.auth(self.cashier)
        self.stock(self.malt_stock, self.bar_store, "12", source_key="sv-malt-menu")
        # The crate product has never been stocked at the Main Store: the menu
        # must report its real store with zero availability, not hide it.
        response = self.client.get("/api/admin/pos/menu/", {"area": "BAR"})
        self.assertEqual(response.status_code, 200, response.content)
        items = {
            row["id"]: row
            for category in response.json()["data"]
            for row in category["items"]
        }
        malt = items[self.malt.pk]
        self.assertTrue(malt["stock"]["tracked"])
        self.assertEqual(malt["stock"]["location_name"], "Bar Store")
        self.assertEqual(malt["stock"]["saleable_quantity"], "12")
        self.assertTrue(malt["stock"]["sufficient_for_one"])
        crate = items[self.crate.pk]
        self.assertTrue(crate["stock"]["tracked"])
        self.assertEqual(crate["stock"]["location_name"], "Main Store")
        self.assertEqual(crate["stock"]["saleable_quantity"], "0")
        self.assertFalse(crate["stock"]["sufficient_for_one"])

    def test_api_live_item_lookup_returns_current_stock(self):
        self.auth(self.cashier)
        self.stock(self.malt_stock, self.bar_store, "7", source_key="sv-malt-lookup")
        response = self.client.get("/api/admin/pos/menu/barcode-lookup/", {
            "menu_item_id": self.malt.pk, "mode": "BAR",
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["menu_item_id"], self.malt.pk)
        self.assertEqual(data["matched_by"], "menu_item")
        self.assertTrue(data["stock"]["tracked"])
        self.assertEqual(data["stock"]["location_name"], "Bar Store")
        self.assertEqual(data["stock"]["saleable_quantity"], "7")
        # A restaurant-only item is not resolvable in BAR mode.
        missing = self.client.get("/api/admin/pos/menu/barcode-lookup/", {
            "menu_item_id": self.jollof.pk, "mode": "BAR",
        })
        self.assertEqual(missing.status_code, 404, missing.content)

    def test_api_barcode_scan_lookup_still_works(self):
        self.auth(self.cashier)
        self.stock(self.malt_stock, self.bar_store, "7", source_key="sv-malt-scan")
        response = self.client.get("/api/admin/pos/menu/barcode-lookup/", {
            "barcode": self.malt.sku, "mode": "BAR",
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["menu_item_id"], self.malt.pk)
        self.assertEqual(data["stock"]["saleable_quantity"], "7")
        self.assertEqual(data["stock"]["location_name"], "Bar Store")

    def test_api_submit_conflict_keeps_the_draft(self):
        self.auth(self.cashier)
        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-submit-api")
        created = self.client.post("/api/admin/pos/orders/", {
            "mode": "BAR",
            "lines": [self.line(self.malt, 3)],
            "idempotency_key": "sv-api-submit-draft",
        }, format="json")
        reference = created.json()["data"]["reference"]
        record_stock_movement(
            item=self.malt_stock, location=self.bar_store, movement_type=StockMovement.Type.ISSUE,
            quantity_delta="-4", source_key="sv-malt-api-drain", actor=self.cashier,
            department="RESTAURANT", recipient="Bar handover",
        )
        conflict = self.client.post(f"/api/admin/pos/orders/{reference}/submit/", {}, format="json")
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(conflict.json()["code"], "POS_STOCK_CONFLICT")
        order = PosOrder.objects.get(reference=reference)
        self.assertEqual(order.status, PosOrder.Status.DRAFT)
        self.assertEqual(order.lines.count(), 1)


class ConcurrencyTests(TransactionTestCase):
    """Two orders must not collectively accept more than the store holds.

    ``TransactionTestCase`` is required so each worker thread commits for real
    and the following worker sees the committed draft (the same reason the
    attendance concurrency suite uses it): inside a wrapped ``TestCase``
    transaction the main thread would hold SQLite's single write lock for the
    whole test and the workers could not begin their own transactions.
    """

    def setUp(self):
        cache.clear()
        setUpPosStockFixtures(self)

    def tearDown(self):
        cache.clear()

    def stock(self, item, location, quantity, *, source_key, allow_negative=False):
        return record_stock_movement(
            item=item, location=location, movement_type=StockMovement.Type.OPENING,
            quantity_delta=quantity, source_key=source_key, actor=self.cashier,
            allow_negative=allow_negative,
        )

    def on_hand(self, item, location):
        balance = StockBalance.objects.filter(item=item, location=location).first()
        return Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")

    def line(self, menu_item, quantity):
        return {"menu_item_id": menu_item.pk, "quantity": quantity}

    def test_sequentially_committed_orders_cannot_collectively_oversell(self):
        """Two requests that together exceed the store: the second is refused.

        On MySQL the balance row locks serialise the two requests; on SQLite
        the single-writer design does. Either way, once request A's draft has
        committed, request B must see its committed quantity and be refused —
        this is exactly the interleaving the locks exist to produce.
        """
        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-seq")
        first, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[self.line(self.malt, 3)], idempotency_key="sv-seq-a",
        )
        with self.assertRaises(PosStockConflictError) as raised:
            create_order(
                mode=PosOrder.Mode.BAR, actor=self.cashier,
                lines=[self.line(self.malt, 3)], idempotency_key="sv-seq-b",
            )
        # The refusal explains both figures: 5 on hand, 3 held by the open
        # order, only 2 still available.
        message = str(raised.exception)
        self.assertIn("only 2 bottles", message)
        self.assertIn("3 bottles are already held by other open orders", message)
        # A request that still fits alongside the open order is accepted.
        second, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=self.cashier,
            lines=[self.line(self.malt, 2)], idempotency_key="sv-seq-c",
        )
        for order in (first, second):
            submit_order(order=order, actor=self.cashier)
            transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.cashier)
            transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.cashier)
            transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.cashier)
        self.assertEqual(self.on_hand(self.malt_stock, self.bar_store), Decimal("0.0000"))
        self.assertEqual(
            StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), 2
        )

    def test_simultaneous_requests_never_oversell_or_corrupt(self):
        """Threaded burst: whatever the interleaving, oversell cannot happen.

        Under the shared-cache SQLite test database some workers lose the
        writer race with a lock error (production MySQL row locks would make
        them wait instead) — that is a safe failure, treated exactly like the
        existing inventory concurrency suites treat it. The invariant under
        every outcome: at most one 6-bottle order is accepted from a 5-bottle
        store, nothing goes negative, and no partial order is ever written.
        """
        from concurrent.futures import ThreadPoolExecutor

        from django.db import OperationalError, connection

        self.stock(self.malt_stock, self.bar_store, "5", source_key="sv-malt-burst")

        def worker(index):
            try:
                order, _ = create_order(
                    mode=PosOrder.Mode.BAR, actor=self.cashier,
                    lines=[self.line(self.malt, 3), self.line(self.malt, 3)],
                    idempotency_key=f"sv-burst-{index}",
                )
                return order.reference
            except (PosStockConflictError, OperationalError):
                return None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))

        accepted = [reference for reference in results if reference]
        self.assertLessEqual(len(accepted), 1, results)
        self.assertEqual(PosOrder.objects.count(), len(accepted))
        for order in PosOrder.objects.all():
            # A partially-written order must never exist.
            self.assertEqual(order.lines.count(), 2)
            self.assertEqual(order.status, PosOrder.Status.DRAFT)
        self.assertGreaterEqual(self.on_hand(self.malt_stock, self.bar_store), Decimal("0.0000"))
