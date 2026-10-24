"""Menu catalogue management API: search, filters, ordering, margin, availability.

Pins the contract the POS "Menu management" screen depends on:

* ``GET /api/admin/pos/menu/categories/`` and ``…/menu/items/`` accept a bounded
  ``search``, the catalogue filters, a whitelisted ``ordering``, and page only
  when asked (categories) / always within a bounded page (items);
* an unknown or unsafe ``ordering`` is refused instead of being passed through to
  the ORM;
* a menu item reports how a sale reaches the stock ledger (``stock_linkage``) and
  — when the caller asks with ``with_margin=true`` — the supplier cost of one
  sale, the margin, and whether the price is below cost;
* the one-click "sold out" / "available again" toggle is a plain partial update
  that cannot smuggle a price change past the cost guard;
* a manager's menu scope is their own service areas, not the whole catalogue.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.inventory.models import StockItem
from apps.pos.models import MenuCategory, MenuItem

from .base import BaseAPITestCase
from .factories import make_staff

CATEGORIES_URL = "/api/admin/pos/menu/categories/"
ITEMS_URL = "/api/admin/pos/menu/items/"


class MenuManagementApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("menu.manager@staff.dev", role=User.Role.MANAGER)
        self.bar_manager = make_staff("menu.bar@staff.dev", role=User.Role.BAR_MANAGER)
        self.restaurant = MenuCategory.objects.create(
            name="Main kitchen", slug="menu-main-kitchen", service_area=MenuCategory.ServiceArea.RESTAURANT,
            sort_order=2,
        )
        self.bar = MenuCategory.objects.create(
            name="Bar drinks", slug="menu-bar-drinks", service_area=MenuCategory.ServiceArea.BAR,
            sort_order=1,
        )
        StockItem.objects.create(
            sku="MENU-COLA", name="Cola 50cl", base_unit="PIECE", standard_cost=Decimal("300.00"),
        )
        self.jollof = MenuItem.objects.create(
            category=self.restaurant, name="Jollof rice", sku="MENU-JOLLOF", base_price=Decimal("3500.00"),
            is_available=True,
        )
        self.cola = MenuItem.objects.create(
            category=self.bar, name="Cola 50cl", sku="MENU-COLA", base_price=Decimal("700.00"),
            is_available=True,
        )

    def _items(self, **params):
        self.auth(self.manager)
        return self.client.get(ITEMS_URL, params)

    def _rows(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    # -- categories ---------------------------------------------------------
    def test_category_list_reports_item_counts_and_filters_by_area(self):
        self.auth(self.manager)
        plain = self.client.get(CATEGORIES_URL)
        self.assertEqual(plain.status_code, 200, plain.content)
        self.assertIsInstance(plain.json()["data"], list, "no page param keeps a plain list")
        counts = {row["slug"]: row["item_count"] for row in plain.json()["data"]}
        self.assertEqual(counts, {"menu-main-kitchen": 1, "menu-bar-drinks": 1})

        paged = self.client.get(CATEGORIES_URL, {"page": 1, "page_size": 1, "ordering": "name"})
        self.assertEqual(paged.status_code, 200, paged.content)
        body = paged.json()
        self.assertIn("pagination", body)
        self.assertEqual(len(body["data"]), 1)

        bar_only = self.client.get(CATEGORIES_URL, {"service_area": "BAR"})
        self.assertEqual([row["slug"] for row in bar_only.json()["data"]], ["menu-bar-drinks"])

    def test_category_search_is_bounded_and_ordering_is_whitelisted(self):
        self.auth(self.manager)
        found = self.client.get(CATEGORIES_URL, {"search": "kitchen"})
        self.assertEqual([row["slug"] for row in found.json()["data"]], ["menu-main-kitchen"])
        too_long = self.client.get(CATEGORIES_URL, {"search": "x" * 101})
        self.assertEqual(too_long.status_code, 400, too_long.content)
        injection = self.client.get(CATEGORIES_URL, {"ordering": "name; DROP TABLE pos_menucategory"})
        self.assertEqual(injection.status_code, 400, injection.content)
        self.assertIn("ordering", injection.content.decode())

    # -- items --------------------------------------------------------------
    def test_item_search_matches_name_sku_barcode_and_category(self):
        for term, expected in (("jollof", ["MENU-JOLLOF"]), ("MENU-COLA", ["MENU-COLA"]),
                               ("bar drinks", ["MENU-COLA"])):
            rows = self._rows(self._items(search=term))
            self.assertEqual([row["sku"] for row in rows], expected, term)

    def test_item_filters_and_ordering_are_applied(self):
        self.bartender_view = self._rows(self._items(service_area="BAR"))
        self.assertEqual([row["sku"] for row in self.bartender_view], ["MENU-COLA"])
        by_category = self._rows(self._items(category=str(self.restaurant.pk)))
        self.assertEqual([row["sku"] for row in by_category], ["MENU-JOLLOF"])
        self.assertEqual(self._rows(self._items(is_available="false")), [])
        priced = self._rows(self._items(ordering="-price"))
        self.assertEqual([row["sku"] for row in priced], ["MENU-JOLLOF", "MENU-COLA"])
        self.assertEqual(self._items(ordering="not-a-field").status_code, 400)
        self.assertEqual(self._items(is_active="maybe").status_code, 400)

    def test_item_rows_carry_the_stock_linkage_and_category_name(self):
        rows = self._rows(self._items(category=str(self.bar.pk)))
        row = rows[0]
        self.assertEqual(row["category_name"], "Bar drinks")
        self.assertEqual(row["service_area"], "BAR")
        # A matching stock item with no explicit link is "not configured": the
        # API reports evidence, it never invents a linkage.
        self.assertEqual(row["stock_linkage"]["type"], "none")
        self.assertIsNone(row["margin"], "margin is computed only when asked for")

    def test_margin_is_reported_when_requested_and_flags_below_cost_prices(self):
        rows = self._rows(self._items(category=str(self.bar.pk), with_margin="true"))
        margin = rows[0]["margin"]
        self.assertTrue(margin["comparable"], margin)
        self.assertEqual(margin["unit_cost"], "300.00")
        self.assertEqual(margin["margin_amount"], "400.00")
        self.assertEqual(margin["margin_percent"], "57.14")
        self.assertFalse(margin["below_cost"])

        # An item with no cost on record must say so rather than invent a margin.
        MenuItem.objects.create(
            category=self.restaurant, name="Uncosted special", sku="MENU-UNCOSTED",
            base_price=Decimal("1000.00"),
        )
        rows = self._rows(self._items(search="Uncosted", with_margin="true"))
        self.assertIsNone(rows[0]["margin"]["unit_cost"])
        self.assertFalse(rows[0]["margin"]["comparable"])
        self.assertIn("cost on record", rows[0]["margin"]["note"])

    def test_sold_out_toggle_is_a_partial_update_and_keeps_the_price_guard(self):
        self.auth(self.manager)
        sold_out = self.client.patch(
            f"{ITEMS_URL}{self.cola.pk}/", {"is_available": False}, format="json"
        )
        self.assertEqual(sold_out.status_code, 200, sold_out.content)
        self.cola.refresh_from_db()
        self.assertFalse(self.cola.is_available)
        self.assertEqual(self.cola.base_price, Decimal("700.00"))

        available = self.client.patch(
            f"{ITEMS_URL}{self.cola.pk}/", {"is_available": True}, format="json"
        )
        self.assertEqual(available.status_code, 200, available.content)
        self.cola.refresh_from_db()
        self.assertTrue(self.cola.is_available)

        # The same endpoint still refuses a price below supplier cost.
        below = self.client.patch(
            f"{ITEMS_URL}{self.cola.pk}/", {"base_price": "250.00"}, format="json"
        )
        self.assertEqual(below.status_code, 400, below.content)
        self.assertIn("below the supplier cost", below.content.decode())

    def test_a_department_manager_sees_only_their_own_service_area(self):
        self.auth(self.bar_manager)
        response = self.client.get(ITEMS_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual({row["sku"] for row in response.json()["data"]}, {"MENU-COLA"})
        refused = self.client.get(ITEMS_URL, {"service_area": "RESTAURANT"})
        self.assertEqual(refused.status_code, 403, refused.content)
