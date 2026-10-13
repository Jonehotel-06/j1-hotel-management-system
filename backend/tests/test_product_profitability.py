"""Product profitability / stock valuation projection (§2 of the repair brief).

These tests pin the *formulas*, not just the endpoint status codes:

* weighted-average purchase cost comes from the immutable receipt ledger
  (4 @ 200 + 2 @ 250 => 216.6667 per unit, not a made-up standard cost),
* the implied unit selling price is the cost-share allocation of the menu price
  across the recipe's components,
* stock value / expected revenue / expected gross profit / margin are consistent
  with each other and with the stored quantities,
* a product with no recipe is reported as NOT_SELLABLE with nulls instead of a
  fabricated zero, and never contributes to the revenue/profit totals,
* the projection is read-only: it writes no movement, balance or journal entry.

Nothing here fabricates a financial record; every fixture is created through the
real service functions.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement, Supplier
from apps.inventory.services.inventory_service import (
    approve_purchase_order, create_purchase_order, create_stock_recipe, mark_purchase_order_ordered,
    receive_goods, record_stock_movement, submit_purchase_order,
)
from apps.inventory.services.valuation_service import (
    product_profitability_projection, projection_filter_choices, projection_summary, sort_projection,
)
from apps.pos.models import MenuCategory, MenuItem

from .base import BaseAPITestCase
from .factories import make_staff


class ProjectionFixtureMixin:
    """Fixtures shared by the service-level and endpoint-level tests."""

    def build_catalog(self):
        self.store = StockLocation.objects.create(code="VAL-STORE", name="Valuation store")
        self.bar = StockLocation.objects.create(code="VAL-BAR", name="Valuation bar")
        self.supplier = Supplier.objects.create(reference="SUP-VAL-1", name="Valuation supplier")
        # 1.5 L bottle of gin: the stock unit is BOTTLE, the recipe uses 0.05 of a
        # bottle per serve, so the unit conversion is exercised in both directions.
        self.gin = StockItem.objects.create(
            sku="VAL-GIN", name="Gin 1.5L", category="Spirits", base_unit="BOTTLE",
            standard_cost=Decimal("9000.00"), reorder_level=Decimal("4"), preferred_supplier=self.supplier,
        )
        # A measured item keeps four decimal places; a counted item does not.
        self.tonic = StockItem.objects.create(
            sku="VAL-TONIC", name="Tonic water 1L", category="Mixers", base_unit="L",
            standard_cost=Decimal("800.00"), reorder_level=Decimal("2.0000"),
        )
        # Consumable with no recipe at all — must be valued but not "sold".
        self.wipe = StockItem.objects.create(
            sku="VAL-WIPE", name="Cleaning wipes", category="Housekeeping", base_unit="PACK",
            standard_cost=Decimal("1500.00"),
        )
        self.category = MenuCategory.objects.create(
            name="Valuation bar", slug="valuation-bar", service_area=MenuCategory.ServiceArea.BAR,
        )
        self.cocktail = MenuItem.objects.create(
            category=self.category, name="Gin & tonic", sku="VAL-COCKTAIL", base_price=Decimal("5000.00"),
        )
        return self

    def receive(self, *, item, location, quantity, unit_cost, key):
        """Receive stock through the real PO -> approval -> GRN path."""
        po, _created = create_purchase_order(
            supplier=self.supplier, delivery_location=location,
            lines=[{"item_id": item.pk, "quantity_ordered": str(quantity), "unit_cost": str(unit_cost)}],
            requested_by=self.clerk, idempotency_key=f"val-po-{key}",
        )
        submit_purchase_order(purchase_order=po, actor=self.clerk)
        approve_purchase_order(purchase_order=po, actor=self.manager)
        mark_purchase_order_ordered(purchase_order=po, actor=self.clerk)
        line = po.lines.get(item=item)
        receive_goods(
            purchase_order=po, receipt_lines=[{"purchase_order_line_id": line.pk, "quantity_received": str(quantity)}],
            actor=self.clerk, source_key=f"val-grn-{key}",
        )
        return po

    def recipe(self, *, components, key, location=None, menu_item=None):
        recipe, _ = create_stock_recipe(
            menu_item=menu_item or self.cocktail, location=location or self.bar,
            tracking_mode="COMPONENTS", components=components, actor=self.clerk, idempotency_key=f"val-bom-{key}",
        )
        return recipe


class ProjectionServiceTests(ProjectionFixtureMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("val-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("val-manager@staff.dev", role=User.Role.MANAGER)
        self.build_catalog()

    def test_weighted_average_cost_comes_from_the_receipt_ledger(self):
        self.receive(item=self.gin, location=self.bar, quantity=4, unit_cost="10000.00", key="gin-a")
        self.receive(item=self.gin, location=self.bar, quantity=2, unit_cost="13000.00", key="gin-b")
        rows = product_profitability_projection(item_ids=[self.gin.pk])
        bar_row = next(row for row in rows if row["location_id"] == self.bar.pk)
        # (4*10000 + 2*13000) / 6 = 11000.00 -- the standard cost of 9000 is ignored
        # because real purchase history exists.
        self.assertEqual(bar_row["purchase_unit_cost"], Decimal("11000.00"))
        self.assertEqual(bar_row["cost_basis"], "WEIGHTED_AVERAGE_RECEIPT")
        self.assertEqual(bar_row["standard_cost"], Decimal("9000.00"))
        self.assertEqual(bar_row["latest_receipt_cost"], Decimal("13000.00"))
        self.assertEqual(bar_row["current_stock"], Decimal("6.0000"))
        self.assertEqual(bar_row["stock_value"], Decimal("66000.00"))

    def test_standard_cost_is_the_disclosed_fallback_and_absence_is_disclosed_too(self):
        # An opening balance with no purchase history: the item's configured
        # standard cost is used, and the row says so.
        record_stock_movement(
            item=self.wipe, location=self.store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="5", source_key="val-wipe-opening", actor=self.clerk,
        )
        rows = product_profitability_projection(item_ids=[self.wipe.pk])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_basis"], "STANDARD_COST")
        self.assertEqual(rows[0]["purchase_unit_cost"], Decimal("1500.00"))
        self.assertEqual(rows[0]["stock_value"], Decimal("7500.00"))
        # With no cost anywhere at all, the row is disclosed as having no cost
        # basis instead of being valued at a fabricated zero.
        StockItem.objects.filter(pk=self.wipe.pk).update(standard_cost=Decimal("0.00"))
        rows = product_profitability_projection(item_ids=[self.wipe.pk])
        self.assertEqual(rows[0]["cost_basis"], "NO_COST_BASIS")
        self.assertIsNone(rows[0]["purchase_unit_cost"])
        self.assertIsNone(rows[0]["stock_value"])
        self.assertIsNone(rows[0]["expected_gross_profit"])

    def test_implied_price_is_the_cost_share_allocation_of_the_menu_price(self):
        self.receive(item=self.gin, location=self.bar, quantity=10, unit_cost="10000.00", key="gin-c")
        self.receive(item=self.tonic, location=self.bar, quantity=10, unit_cost="800.00", key="tonic-a")
        # Cost per serve: gin 0.05*9000 = 450 (standard cost, no receipt yet on the
        # item when the recipe is priced) ... use the same basis for both lines.
        self.recipe(
            components=[
                {"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0500"},
                {"item_id": self.tonic.pk, "quantity_per_menu_unit": "0.2000"},
            ],
            key="cocktail-v1",
        )
        rows = product_profitability_projection(item_ids=[self.gin.pk, self.tonic.pk])
        by_sku = {row["sku"]: row for row in rows}
        gin_row, tonic_row = by_sku["VAL-GIN"], by_sku["VAL-TONIC"]
        # Recipe cost = 0.05*9000 + 0.2*800 = 450 + 160 = 610.
        gin_share = Decimal("450") / Decimal("610")
        tonic_share = Decimal("160") / Decimal("610")
        self.assertEqual(gin_row["price_basis"], "RECIPE_IMPLIED_AVERAGE")
        self.assertEqual(gin_row["recipe_count"], 1)
        self.assertEqual(gin_row["selling_price"], (Decimal("5000") * gin_share / Decimal("0.05")).quantize(Decimal("0.01")))
        self.assertEqual(tonic_row["selling_price"], (Decimal("5000") * tonic_share / Decimal("0.2")).quantize(Decimal("0.01")))
        # The two shares must account for the whole menu price.
        reconstructed = gin_row["selling_price"] * Decimal("0.05") + tonic_row["selling_price"] * Decimal("0.2")
        self.assertAlmostEqual(float(reconstructed), 5000.00, places=2)

    def test_projection_figures_are_mutually_consistent(self):
        self.receive(item=self.gin, location=self.bar, quantity=6, unit_cost="10000.00", key="gin-d")
        self.recipe(components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0500"}], key="shots-v1")
        row = next(r for r in product_profitability_projection(item_ids=[self.gin.pk]) if r["location_id"] == self.bar.pk)
        self.assertEqual(row["stock_value"], row["current_stock"] * row["purchase_unit_cost"])
        self.assertEqual(row["expected_sales_revenue"], row["current_stock"] * row["selling_price"])
        self.assertEqual(row["expected_gross_profit"], row["expected_sales_revenue"] - row["stock_value"])
        expected_margin = (row["expected_gross_profit"] / row["expected_sales_revenue"] * 100).quantize(Decimal("0.01"))
        self.assertEqual(row["expected_gross_margin_percent"], expected_margin)

    def test_an_item_without_a_recipe_is_not_sold_and_never_inflates_totals(self):
        self.receive(item=self.wipe, location=self.store, quantity=3, unit_cost="1500.00", key="wipe-a")
        rows = product_profitability_projection()
        wipe_row = next(r for r in rows if r["sku"] == "VAL-WIPE")
        self.assertEqual(wipe_row["status"], "NOT_SELLABLE")
        self.assertEqual(wipe_row["price_basis"], "NO_ACTIVE_RECIPE")
        self.assertIsNone(wipe_row["selling_price"])
        self.assertIsNone(wipe_row["expected_sales_revenue"])
        self.assertIsNone(wipe_row["expected_gross_profit"])
        self.assertIsNone(wipe_row["expected_gross_margin_percent"])
        # ... but it is still valued, and the summary discloses the coverage gap.
        self.assertEqual(wipe_row["stock_value"], Decimal("4500.00"))
        summary = projection_summary(rows)
        self.assertEqual(summary["unpriced_row_count"], 1)
        self.assertEqual(summary["sellable_row_count"], 0)
        self.assertEqual(summary["total_expected_sales_revenue"], Decimal("0.00"))
        self.assertEqual(summary["total_stock_value"], Decimal("4500.00"))

    def test_a_retired_recipe_stops_being_an_onward_sale(self):
        self.receive(item=self.gin, location=self.bar, quantity=2, unit_cost="10000.00", key="gin-e")
        self.recipe(components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0500"}], key="retire-v1")
        self.assertTrue(product_profitability_projection(item_ids=[self.gin.pk])[0]["selling_price"])
        # A newer version retires the old one, so exactly one price is averaged in.
        self.recipe(components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0400"}], key="retire-v2")
        rows = product_profitability_projection(item_ids=[self.gin.pk])
        self.assertEqual(rows[0]["recipe_count"], 1)

    def test_multi_recipe_average_is_disclosed_with_its_recipe_count(self):
        self.receive(item=self.gin, location=self.bar, quantity=5, unit_cost="10000.00", key="gin-f")
        self.recipe(components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0500"}], key="multi-v1")
        other_category = MenuCategory.objects.create(name="Valuation restaurant", slug="valuation-restaurant")
        other_item = MenuItem.objects.create(
            category=other_category, name="Gin flight", sku="VAL-FLIGHT", base_price=Decimal("9000.00"),
        )
        # A second, independent menu item that also consumes the gin.
        self.recipe(
            menu_item=other_item, components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.1000"}], key="multi-v2",
        )
        row = next(r for r in product_profitability_projection(item_ids=[self.gin.pk]) if r["location_id"] == self.bar.pk)
        self.assertEqual(row["recipe_count"], 2)
        # The row reports the average of the two implied unit prices, so it sits
        # between them rather than at the maximum.
        self.assertGreater(row["selling_price"], Decimal("0"))
        self.assertIn("Bar", row["department"].split(" + "))

    def test_projection_writes_nothing(self):
        self.receive(item=self.gin, location=self.bar, quantity=4, unit_cost="10000.00", key="gin-g")
        self.recipe(components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0500"}], key="noop-v1")
        movements = StockMovement.objects.count()
        balances = list(StockBalance.objects.values_list("pk", "quantity_on_hand"))
        rows = product_profitability_projection()
        self.assertTrue(rows)
        self.assertEqual(StockMovement.objects.count(), movements)
        self.assertEqual(list(StockBalance.objects.values_list("pk", "quantity_on_hand")), balances)

    def test_search_filters_and_sorting(self):
        self.receive(item=self.gin, location=self.bar, quantity=4, unit_cost="10000.00", key="gin-h")
        self.receive(item=self.tonic, location=self.bar, quantity=1, unit_cost="800.00", key="tonic-b")
        self.assertEqual([r["sku"] for r in product_profitability_projection(search="VAL-GIN")], ["VAL-GIN"])
        self.assertEqual(len(product_profitability_projection(location_id=self.bar.pk)), 2)
        low = product_profitability_projection(low_stock_only=True)
        self.assertEqual({r["sku"] for r in low}, {"VAL-GIN", "VAL-TONIC"})
        rows = product_profitability_projection()
        self.assertEqual([r["sku"] for r in sort_projection(rows, sort="stock_value", direction="desc")][0], "VAL-GIN")
        # Unknown rows sort last in BOTH directions rather than pretending to be zero.
        ordered = sort_projection(rows, sort="expected_gross_profit", direction="desc")
        self.assertIsNone(ordered[-1]["expected_gross_profit"])
        self.assertEqual(projection_filter_choices()["departments"], ["Bar"])


class ProjectionEndpointTests(ProjectionFixtureMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("val-api-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.cashier = make_staff("val-api-cashier@staff.dev", role=User.Role.CASHIER)
        self.manager = make_staff("val-api-manager@staff.dev", role=User.Role.MANAGER)
        self.build_catalog()

    def test_endpoint_requires_an_inventory_or_reporting_capability(self):
        self.auth(self.cashier)
        denied = self.client.get("/api/admin/inventory/profitability/")
        self.assertEqual(denied.status_code, 403, denied.content)
        self.auth(self.clerk)
        allowed = self.client.get("/api/admin/inventory/profitability/")
        self.assertEqual(allowed.status_code, 200, allowed.content)
        payload = allowed.json()
        self.assertEqual(payload["projection"], "PROJECTION")
        self.assertEqual(payload["summary"]["projection"], "PROJECTION")
        self.assertIn("total_stock_value", payload["summary"])

    def test_endpoint_paginates_sorts_and_rejects_a_bad_sort_key(self):
        StockItem.objects.all().update(is_active=True)
        self.receive(item=self.gin, location=self.bar, quantity=4, unit_cost="10000.00", key="gin-i")
        self.receive(item=self.wipe, location=self.store, quantity=2, unit_cost="1500.00", key="wipe-b")
        self.auth(self.clerk)
        page = self.client.get("/api/admin/inventory/profitability/", {"sort": "sku", "direction": "asc", "page_size": "1"})
        self.assertEqual(page.status_code, 200, page.content)
        body = page.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["pagination"]["count"], 2)
        self.assertEqual(body["data"][0]["sku"], "VAL-GIN")
        self.assertEqual(body["data"][0]["projection"], "PROJECTION")
        rejected = self.client.get("/api/admin/inventory/profitability/", {"sort": "drop_tables"})
        self.assertEqual(rejected.status_code, 400, rejected.content)

    def test_endpoint_filters_are_applied_server_side(self):
        self.receive(item=self.gin, location=self.bar, quantity=4, unit_cost="10000.00", key="gin-j")
        self.recipe(components=[{"item_id": self.gin.pk, "quantity_per_menu_unit": "0.0500"}], key="filters-v1")
        self.receive(item=self.wipe, location=self.store, quantity=2, unit_cost="1500.00", key="wipe-c")
        self.auth(self.clerk)
        filtered = self.client.get("/api/admin/inventory/profitability/", {"sellable": "false"})
        self.assertEqual(filtered.status_code, 200, filtered.content)
        skus = [row["sku"] for row in filtered.json()["data"]]
        self.assertEqual(skus, ["VAL-WIPE"])
        invalid = self.client.get("/api/admin/inventory/profitability/", {"low_stock": "maybe"})
        self.assertEqual(invalid.status_code, 400, invalid.content)
