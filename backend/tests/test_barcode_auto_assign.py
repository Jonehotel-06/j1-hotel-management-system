"""Automatic SKU barcode assignment for newly created menu items."""
from decimal import Decimal

from apps.accounts.models import User
from apps.pos.models import MenuCategory, MenuItem

from .base import BaseAPITestCase
from .factories import make_staff

EAN13_TAKEN = "5901234123457"


class AutoBarcodeAssignmentTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("auto-barcode-manager@staff.dev", role=User.Role.MANAGER)
        self.category = MenuCategory.objects.create(
            name="Auto barcode bar", slug="auto-barcode-bar",
            service_area=MenuCategory.ServiceArea.BAR,
        )

    def _create(self, name, sku, **kw):
        return MenuItem.objects.create(
            category=self.category, name=name, sku=sku,
            base_price=Decimal("1000.00"), **kw,
        )

    def test_new_item_without_barcode_adopts_sku(self):
        item = self._create("House Cola", "COLA-33CL")
        self.assertEqual(item.barcode, "COLA-33CL")

    def test_typed_barcode_is_never_overwritten(self):
        item = self._create("Branded Cola", "COLA-BRAND", barcode=EAN13_TAKEN)
        self.assertEqual(item.barcode, EAN13_TAKEN)

    def test_sku_already_claimed_as_barcode_leaves_item_unlabelled(self):
        self._create("Branded", "BRAND-1", barcode=EAN13_TAKEN)
        item = self._create("Numeric", EAN13_TAKEN)
        self.assertIsNone(item.barcode)

    def test_unencodable_sku_leaves_item_unlabelled_but_scannable_by_sku(self):
        item = self._create("Odd", "HAS SPACE")
        self.assertIsNone(item.barcode)

    def test_numeric_sku_with_bad_gtin_check_digit_is_not_assigned(self):
        item = self._create("Bad GTIN", "5901234123458")
        self.assertIsNone(item.barcode)

    def test_clearing_barcode_on_edit_stays_cleared(self):
        item = self._create("Clearable", "CLEAR-1")
        self.assertEqual(item.barcode, "CLEAR-1")
        self.auth(self.manager)
        response = self.client.patch(
            f"/api/admin/pos/menu/items/{item.pk}/", {"barcode": ""}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertIsNone(item.barcode)

    def test_create_via_api_returns_the_assigned_barcode(self):
        self.auth(self.manager)
        response = self.client.post("/api/admin/pos/menu/items/", {
            "category": self.category.pk, "name": "API Cola", "sku": "API-COLA",
            "base_price": "900.00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["barcode"], "API-COLA")

    def test_scanned_label_value_resolves_the_item(self):
        item = self._create("Scannable", "SCAN-ME-1")
        self.auth(self.manager)
        response = self.client.get("/api/admin/pos/menu/barcode-lookup/", {"barcode": item.barcode})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["menu_item_id"], item.pk)
        self.assertEqual(response.json()["data"]["matched_by"], "barcode")
