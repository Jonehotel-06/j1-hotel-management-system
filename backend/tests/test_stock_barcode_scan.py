"""
Task 03 of targeted_fixes_01 — USB HID barcode scanning for INVENTORY.

The rule the task set is narrow and absolute: **a scan identifies an item; it
never moves stock.** These tests pin exactly that:

* a barcode, an SKU and a case-insensitive variant all resolve to one item;
* the response reports identity and current balances, nothing else;
* scanning records NO movement, NO adjustment and NO balance change — this is
  asserted on the database, not on the response shape;
* an unknown code is a clear 404 "not found", never a silent success;
* an ambiguous code is refused rather than guessed;
* a role without inventory visibility cannot use the lookup at all, and even
  an authorised role gains no recording right by scanning.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.stock_barcode_service import (
    AMBIGUOUS_MESSAGE,
    NOT_FOUND_MESSAGE,
    resolve_stock_item,
)

from .base import BaseAPITestCase
from .factories import make_staff

LOOKUP_URL = "/api/admin/inventory/items/barcode-lookup/"


class StockBarcodeScanTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.storekeeper = make_staff("task03-storekeeper@staff.dev", role=User.Role.STOREKEEPER)
        self.main_store = StockLocation.objects.create(code="T03-MAIN", name="Main store")
        self.bar_store = StockLocation.objects.create(code="T03-BAR", name="Bar store")
        self.rice = StockItem.objects.create(
            sku="T03-RICE", barcode="6001234567890", name="Rice 50kg",
            category="GRAINS", base_unit="BAG", standard_cost=Decimal("66000.00"),
        )
        self.malt = StockItem.objects.create(
            sku="T03-MALT", barcode="", name="Malt extract",
            category="BEVERAGE", base_unit="BOTTLE", standard_cost=Decimal("450.00"),
        )
        StockBalance.objects.create(item=self.rice, location=self.main_store, quantity_on_hand=Decimal("12"))
        StockBalance.objects.create(item=self.rice, location=self.bar_store, quantity_on_hand=Decimal("3"))
        StockBalance.objects.create(item=self.malt, location=self.bar_store, quantity_on_hand=Decimal("48"))

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    def test_a_scanned_barcode_identifies_the_item(self):
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "6001234567890"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["item_id"], self.rice.pk)
        self.assertEqual(data["name"], "Rice 50kg")
        self.assertEqual(data["sku"], "T03-RICE")
        self.assertEqual(data["matched_by"], "barcode")
        self.assertTrue(data["is_active"])

    def test_a_scanned_sku_identifies_the_item(self):
        """Labels printed from a SKU keep working (malt has no barcode)."""
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "T03-MALT"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["item_id"], self.malt.pk)
        self.assertEqual(data["matched_by"], "sku")

    def test_a_lower_case_scan_still_resolves(self):
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "t03-rice"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["item_id"], self.rice.pk)

    def test_the_response_reports_current_balances_per_location(self):
        self.auth(self.storekeeper)
        data = self.client.get(LOOKUP_URL, {"barcode": "6001234567890"}).json()["data"]
        by_location = {row["location_code"]: row for row in data["locations"]}
        self.assertEqual(by_location["T03-MAIN"]["quantity_on_hand"], "12.0000")
        self.assertEqual(by_location["T03-BAR"]["quantity_on_hand"], "3.0000")
        self.assertEqual(Decimal(data["total_quantity"]), Decimal("15.0000"))

    def test_code_and_sku_parameters_are_accepted(self):
        self.auth(self.storekeeper)
        for param in ("code", "sku"):
            response = self.client.get(LOOKUP_URL, {param: "6001234567890"})
            self.assertEqual(response.status_code, 200, f"?{param} -> {response.content}")

    # ------------------------------------------------------------------ #
    # The scan must change NOTHING
    # ------------------------------------------------------------------ #

    def test_scanning_records_no_movement_and_leaves_every_balance_alone(self):
        self.auth(self.storekeeper)
        before_movements = StockMovement.objects.count()
        before_balances = {
            (balance.item_id, balance.location_id): balance.quantity_on_hand
            for balance in StockBalance.objects.all()
        }

        for _ in range(5):
            self.assertEqual(self.client.get(LOOKUP_URL, {"barcode": "6001234567890"}).status_code, 200)

        self.assertEqual(StockMovement.objects.count(), before_movements, "a scan must not post a movement")
        after_balances = {
            (balance.item_id, balance.location_id): balance.quantity_on_hand
            for balance in StockBalance.objects.all()
        }
        self.assertEqual(after_balances, before_balances, "a scan must not change stock")

    def test_scanning_creates_no_receipt_issue_or_count(self):
        from apps.inventory.models import GoodsReceipt, StockCount

        self.auth(self.storekeeper)
        receipts = GoodsReceipt.objects.count()
        counts = StockCount.objects.count()
        self.client.get(LOOKUP_URL, {"barcode": "T03-MALT"})
        self.assertEqual(GoodsReceipt.objects.count(), receipts)
        self.assertEqual(StockCount.objects.count(), counts)

    # ------------------------------------------------------------------ #
    # Not found / ambiguous
    # ------------------------------------------------------------------ #

    def test_an_unknown_barcode_is_a_clear_not_found(self):
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "0000000000000"})
        self.assertEqual(response.status_code, 404)
        body = response.json()
        # The operator-facing wording survives the project exception handler,
        # so the screen can say WHAT was not found instead of a generic 404.
        self.assertEqual(body["message"], NOT_FOUND_MESSAGE)
        self.assertIn("No stock item matches", body["message"])
        self.assertEqual(body["code"], "STOCK_BARCODE_NOT_FOUND")

    def test_a_short_code_is_rejected_as_invalid_not_as_not_found(self):
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "12"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_an_empty_scan_is_rejected(self):
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": ""})
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_code_matching_two_items_is_refused_rather_than_guessed(self):
        """One item's SKU may legitimately equal another item's barcode.

        ``barcode`` is unique on its own column, so a duplicate barcode cannot
        exist; what CAN happen is a SKU that collides with a different item's
        barcode (an old label printed from the SKU). The lookup then has two
        honest candidates and must refuse instead of picking one.
        """
        twin = StockItem.objects.create(
            sku="6001234567890", barcode="6009999999999", name="Colliding item",
            category="GRAINS", base_unit="BAG", standard_cost=Decimal("100.00"),
        )
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "6001234567890"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["message"], AMBIGUOUS_MESSAGE)
        self.assertEqual(response.json()["code"], "STOCK_BARCODE_AMBIGUOUS")
        self.assertNotEqual(twin.pk, self.rice.pk)

    def test_an_inactive_item_still_resolves_so_the_store_keeper_is_told(self):
        self.rice.is_active = False
        self.rice.save(update_fields=["is_active"])
        self.auth(self.storekeeper)
        response = self.client.get(LOOKUP_URL, {"barcode": "6001234567890"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["data"]["is_active"])
        self.assertIn("inactive", response.json()["message"].lower())

    # ------------------------------------------------------------------ #
    # Permission: identify only
    # ------------------------------------------------------------------ #

    def test_a_role_without_inventory_visibility_cannot_look_up(self):
        self.auth(make_staff("task03-waiter@staff.dev", role=User.Role.WAITER))
        self.assertEqual(self.client.get(LOOKUP_URL, {"barcode": "6001234567890"}).status_code, 403)

    def test_an_unauthorised_request_does_not_leak_the_item(self):
        self.auth(make_staff("task03-security@staff.dev", role=User.Role.SECURITY))
        response = self.client.get(LOOKUP_URL, {"barcode": "6001234567890"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Rice", response.content.decode())

    def test_scanning_confers_no_recording_authority(self):
        """A procurement officer can scan but still may not record stock."""
        officer = make_staff("task03-officer@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.auth(officer)
        self.assertEqual(self.client.get(LOOKUP_URL, {"barcode": "6001234567890"}).status_code, 200)
        refused = self.client.post("/api/admin/inventory/opening-stock/", {
            "item_id": self.rice.pk, "location_id": self.main_store.pk, "quantity": "1",
            "idempotency_key": "t03-officer-opening",
        }, format="json")
        self.assertEqual(refused.status_code, 403, refused.content)

    def test_the_resolver_is_usable_without_a_request(self):
        """A management command can resolve a scan too."""
        item, matched_by = resolve_stock_item("T03-MALT")
        self.assertEqual(item.pk, self.malt.pk)
        self.assertEqual(matched_by, "sku")
