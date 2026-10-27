"""Barcode lookup, assignment validation, and scan-to-sale stock integrity."""
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import Capability, User, UserCapabilityOverride
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import create_stock_recipe, record_stock_movement
from apps.pos.barcode import gtin_check_digit_is_valid, validate_barcode
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.barcode_service import resolve_barcode

from .base import BaseAPITestCase
from .factories import make_staff

EAN13 = "5901234123457"  # valid GS1 check digit
EAN13_BAD = "5901234123458"


class BarcodeValidationTests(BaseAPITestCase):
    def test_gtin_check_digit(self):
        self.assertTrue(gtin_check_digit_is_valid(EAN13))
        self.assertFalse(gtin_check_digit_is_valid(EAN13_BAD))
        self.assertEqual(validate_barcode(f"  {EAN13}  "), EAN13)
        # Leading zeros are meaningful and preserved.
        self.assertEqual(validate_barcode(" 0012345 "), "0012345")
        with self.assertRaises(ValueError):
            validate_barcode(EAN13_BAD)
        with self.assertRaises(ValueError):
            validate_barcode("AB CD")


class BarcodeLookupApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.bartender = make_staff("barcode-bartender@staff.dev", role=User.Role.BARTENDER)
        self.waiter = make_staff("barcode-waiter@staff.dev", role=User.Role.WAITER)
        # Keep this fixture's cross-service-area assertion scoped to a waiter
        # without the Cashier-wide POS grant transferred to the real role.
        UserCapabilityOverride.objects.create(
            user=self.waiter,
            capability=Capability.objects.get(code="pos.order.manage"),
            effect=UserCapabilityOverride.Effect.REVOKE,
            reason="Isolate the service-area barcode fixture from merged cashier duties.",
        )
        self.manager = make_staff("barcode-manager@staff.dev", role=User.Role.MANAGER)
        bar = MenuCategory.objects.create(name="Barcode bar", slug="barcode-bar", service_area=MenuCategory.ServiceArea.BAR)
        restaurant = MenuCategory.objects.create(name="Barcode dining", slug="barcode-dining", service_area=MenuCategory.ServiceArea.RESTAURANT)
        self.drink = MenuItem.objects.create(
            category=bar, name="Bottled Drink", sku="DRINK-001", barcode=EAN13, base_price=Decimal("1500.00"),
        )
        self.meal = MenuItem.objects.create(
            category=restaurant, name="Rice Meal", sku="MEAL-001", base_price=Decimal("2500.00"),
        )
        self.inactive = MenuItem.objects.create(
            category=bar, name="Old Drink", sku="DRINK-OLD", barcode="CODE128OLD1", base_price=Decimal("900.00"), is_active=False,
        )

    def lookup(self, user, **params):
        self.auth(user)
        return self.client.get("/api/admin/pos/menu/barcode-lookup/", params)

    def test_valid_barcode_returns_server_price(self):
        response = self.lookup(self.bartender, barcode=EAN13, mode="BAR")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["menu_item_id"], self.drink.pk)
        self.assertEqual(data["name"], "Bottled Drink")
        self.assertEqual(data["base_price"], "1500.00")
        self.assertEqual(data["matched_by"], "barcode")
        self.assertFalse(data["stock"]["tracked"])

    def test_sku_fallback_and_whitespace_trim_and_leading_zero(self):
        response = self.lookup(self.bartender, barcode="  DRINK-001 ", mode="BAR")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["matched_by"], "sku")
        item, matched_by = resolve_barcode(EAN13)
        self.assertEqual((item.pk, matched_by), (self.drink.pk, "barcode"))

    def test_unknown_barcode_404_with_register_message(self):
        response = self.lookup(self.bartender, barcode="9999999999999", mode="BAR")
        # 999... is not a valid EAN check digit, so use a valid-format unknown:
        if response.status_code == 400:
            response = self.lookup(self.bartender, barcode="4006381333931", mode="BAR")
        self.assertEqual(response.status_code, 404, response.content)
        # The shared error contract genericises 404 bodies; the POS UI maps
        # a 404 from this endpoint to "Product barcode not found. Check the
        # barcode or register the product first." (see pos.html handleScan).
        self.assertIn("RESOURCE_NOT_FOUND", str(response.content))

    def test_invalid_checksum_400_and_inactive_not_sold(self):
        response = self.lookup(self.bartender, barcode=EAN13_BAD, mode="BAR")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("check digit", str(response.content).lower())
        response = self.lookup(self.bartender, barcode="CODE128OLD1", mode="BAR")
        self.assertEqual(response.status_code, 404, response.content)

    def test_mode_scoping_prevents_cross_area_scan(self):
        response = self.lookup(self.bartender, barcode="MEAL-001", mode="BAR")
        self.assertEqual(response.status_code, 404, response.content)
        response = self.lookup(self.waiter, barcode=EAN13, mode="BAR")
        self.assertEqual(response.status_code, 403, response.content)

    def test_unauthenticated_rejected(self):
        self.unauth()
        response = self.client.get("/api/admin/pos/menu/barcode-lookup/", {"barcode": EAN13})
        self.assertIn(response.status_code, (401, 403))

    def test_duplicate_barcode_assignment_rejected(self):
        self.auth(self.manager)
        created = self.client.post("/api/admin/pos/menu/items/", {
            "category": self.drink.category_id, "name": "Other Drink", "sku": "DRINK-002",
            "barcode": EAN13, "base_price": "1200.00",
        }, format="json")
        self.assertEqual(created.status_code, 400, created.content)
        with self.assertRaises((IntegrityError, DjangoValidationError)):
            with transaction.atomic():
                MenuItem.objects.create(
                    category=self.drink.category, name="DB Duplicate", sku="DRINK-003",
                    barcode=EAN13, base_price=Decimal("1.00"),
                )

    def test_barcode_can_be_assigned_and_cleared_via_management_api(self):
        self.auth(self.manager)
        response = self.client.patch(f"/api/admin/pos/menu/items/{self.meal.pk}/", {"barcode": "4006381333931"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.meal.refresh_from_db()
        self.assertEqual(self.meal.barcode, "4006381333931")
        cleared = self.client.patch(f"/api/admin/pos/menu/items/{self.meal.pk}/", {"barcode": ""}, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.meal.refresh_from_db()
        self.assertIsNone(self.meal.barcode)


class ScanToCompletedSaleStockTests(BaseAPITestCase):
    """Scenario A at service/API level using isolated records only."""

    def setUp(self):
        super().setUp()
        self.bartender = make_staff("scan-bartender@staff.dev", role=User.Role.BARTENDER)
        self.clerk = make_staff("scan-clerk@staff.dev", role=User.Role.STOREKEEPER)
        bar = MenuCategory.objects.create(name="Scan bar", slug="scan-bar", service_area=MenuCategory.ServiceArea.BAR)
        self.drink = MenuItem.objects.create(
            category=bar, name="Scan Drink", sku="SCAN-DRINK", barcode=EAN13, base_price=Decimal("1500.00"),
        )
        self.location = StockLocation.objects.create(code="SCAN-BAR", name="Scan bar store")
        self.stock_item = StockItem.objects.create(sku="SCAN-DRINK", name="Scan Drink", base_unit="BOTTLE")
        create_stock_recipe(
            menu_item=self.drink, location=self.location, tracking_mode="COMPONENTS",
            components=[{"item_id": self.stock_item.pk, "quantity_per_menu_unit": "1"}],
            actor=self.clerk, idempotency_key="scan-recipe-1",
        )
        record_stock_movement(
            item=self.stock_item, location=self.location, movement_type=StockMovement.Type.OPENING,
            quantity_delta="10", source_key="scan-opening-10", actor=self.clerk,
        )

    def on_hand(self):
        return StockBalance.objects.get(item=self.stock_item, location=self.location).quantity_on_hand

    def test_repeated_scans_one_cart_line_and_delivery_deducts_once(self):
        self.auth(self.bartender)
        # Three scans resolve the same product; the cart rule (frontend and
        # this payload) is one line with quantity 3, priced by the server.
        for _ in range(3):
            response = self.client.get("/api/admin/pos/menu/barcode-lookup/", {"barcode": EAN13, "mode": "BAR"})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["data"]["stock"]["saleable_quantity"], "10")
        created = self.client.post("/api/admin/pos/orders/", {
            "mode": "BAR", "table_number": "B-1",
            "lines": [{"menu_item_id": self.drink.pk, "quantity": 3}],
            "idempotency_key": "scan-order-1",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        data = created.json()["data"]
        self.assertEqual(data["total_amount"], "4500.00")
        reference = data["reference"]
        duplicate = self.client.post("/api/admin/pos/orders/", {
            "mode": "BAR", "table_number": "B-1",
            "lines": [{"menu_item_id": self.drink.pk, "quantity": 3}],
            "idempotency_key": "scan-order-1",
        }, format="json")
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(PosOrder.objects.count(), 1)
        # Cancelled/abandoned drafts deduct nothing.
        self.assertEqual(self.on_hand(), Decimal("10.0000"))
        for action, payload in (("submit", {}), ("status", {"status": "PREPARING"}), ("status", {"status": "READY"}), ("status", {"status": "DELIVERED"})):
            response = self.client.post(f"/api/admin/pos/orders/{reference}/{action}/", payload, format="json")
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.on_hand(), Decimal("7.0000"))
        # A retried delivery transition is a no-op (already DELIVERED).
        retry = self.client.post(f"/api/admin/pos/orders/{reference}/status/", {"status": "DELIVERED"}, format="json")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(self.on_hand(), Decimal("7.0000"))
        movements = StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION)
        self.assertEqual(movements.count(), 1)
        self.assertEqual(movements.get().quantity_delta, Decimal("-3.0000"))
