"""Menu selling price must cover supplier cost; pack-based stock recording.

Pins the staff-requested rules:

* a bar/POS menu price below the known supplier purchase cost is rejected
  (direct stock item match and recipe-component cost alike), while a price
  equal to or above cost — or any price when no cost is on record — saves;
* stock items can record supplier packaging (pack/bag size + actual pack
  price) and derive the per-piece standard cost, and goods receipts can be
  recorded in packs while the ledger counts pieces;
* the profitability projection prices a directly-sold product (menu item =
  stock item, no recipe yet) so expected revenue/gross profit display;
* processing an already-processed POS hand-off deducts nothing again.
"""
from decimal import Decimal

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import (
    create_stock_recipe, process_pos_stock_consumption, record_stock_movement,
)
from apps.inventory.services.valuation_service import menu_item_cost_basis, product_profitability_projection
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import create_order, submit_order, transition_order

from .base import BaseAPITestCase
from .factories import make_staff


class MenuPriceCostGuardTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_staff("price-manager@staff.dev", role=User.Role.MANAGER)
        self.bar = MenuCategory.objects.create(
            name="Price guard bar", slug="price-guard-bar", service_area=MenuCategory.ServiceArea.BAR,
        )
        self.cola = StockItem.objects.create(
            sku="PG-COLA", name="Cola 50cl", base_unit="PIECE", standard_cost=Decimal("500.00"),
        )

    def create_menu_item(self, *, price, sku="PG-COLA", name="Cola 50cl"):
        self.auth(self.manager)
        return self.client.post("/api/admin/pos/menu/items/", {
            "category": self.bar.pk, "name": name, "sku": sku, "base_price": str(price),
        }, format="json")

    def test_price_below_supplier_cost_is_rejected(self):
        response = self.create_menu_item(price="400.00")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("below the supplier cost", str(response.content))

    def test_price_equal_to_and_above_cost_is_accepted(self):
        self.assertEqual(self.create_menu_item(price="500.00", sku="PG-COLA-EQ", name="Cola equal").status_code, 201)
        response = self.create_menu_item(price="750.00")
        self.assertEqual(response.status_code, 201, response.content)

    def test_receipt_cost_overrides_the_standard_cost_fallback(self):
        location = StockLocation.objects.create(code="PG-BAR", name="Price guard bar store")
        record_stock_movement(
            item=self.cola, location=location, movement_type=StockMovement.Type.RECEIPT,
            quantity_delta="24", unit_cost="800.00", source_key="pg-receipt-1", actor=self.manager,
        )
        basis = menu_item_cost_basis(self.cola_menu_probe())
        self.assertEqual(basis["cost"], Decimal("800.00"))
        response = self.create_menu_item(price="799.99")
        self.assertEqual(response.status_code, 400, response.content)

    def cola_menu_probe(self):
        return MenuItem(category=self.bar, name="Cola 50cl", sku="PG-COLA", base_price=Decimal("1.00"))

    def test_no_cost_on_record_allows_any_price(self):
        StockItem.objects.create(sku="PG-FREE", name="Free Garnish", base_unit="PIECE", standard_cost=Decimal("0.00"))
        response = self.create_menu_item(price="100.00", sku="PG-FREE", name="Free Garnish")
        self.assertEqual(response.status_code, 201, response.content)

    def test_recipe_component_cost_is_enforced_on_price_edit(self):
        gin = StockItem.objects.create(
            sku="PG-GIN", name="Gin", base_unit="BOTTLE", standard_cost=Decimal("9000.00"),
        )
        cocktail = MenuItem.objects.create(
            category=self.bar, name="PG Cocktail", sku="PG-COCKTAIL", base_price=Decimal("5000.00"),
        )
        location = StockLocation.objects.create(code="PG-CELLAR", name="Price guard cellar")
        create_stock_recipe(
            menu_item=cocktail, location=location, tracking_mode="COMPONENTS",
            components=[{"item_id": gin.pk, "quantity_per_menu_unit": "0.1"}],
            actor=self.manager, idempotency_key="pg-recipe-1",
        )
        # One cocktail consumes 0.1 bottle x 9000 = 900.00 of supplier cost.
        self.auth(self.manager)
        below = self.client.patch(f"/api/admin/pos/menu/items/{cocktail.pk}/", {"base_price": "899.99"}, format="json")
        self.assertEqual(below.status_code, 400, below.content)
        ok = self.client.patch(f"/api/admin/pos/menu/items/{cocktail.pk}/", {"base_price": "900.00"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)


class PackRecordingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("pack-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.auth(self.clerk)

    def test_pack_price_and_size_derive_the_piece_cost(self):
        response = self.client.post("/api/admin/inventory/items/", {
            "sku": "PK-COLA", "name": "Packed Cola", "base_unit": "PIECE",
            "units_per_pack": "24", "pack_price": "12000.00", "standard_cost": "0.00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["standard_cost"], "500.00")
        self.assertEqual(data["pack_cost"], "12000.00")
        item = StockItem.objects.get(sku="PK-COLA")
        self.assertEqual(item.standard_cost, Decimal("500.00"))
        self.assertEqual(item.units_per_pack, Decimal("24.00"))

    def test_explicit_piece_price_wins_over_pack_derivation(self):
        response = self.client.post("/api/admin/inventory/items/", {
            "sku": "PK-COLA-2", "name": "Packed Cola 2", "base_unit": "PIECE",
            "units_per_pack": "24", "pack_price": "12000.00", "standard_cost": "450.00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["standard_cost"], "450.00")


class DirectSaleProfitabilityTests(BaseAPITestCase):
    def test_directly_sold_product_shows_expected_revenue_and_gross_profit(self):
        manager = make_staff("profit-manager@staff.dev", role=User.Role.MANAGER)
        location = StockLocation.objects.create(code="DP-BAR", name="Direct profit bar")
        item = StockItem.objects.create(
            sku="DP-COLA", name="Direct Cola", base_unit="PIECE", standard_cost=Decimal("500.00"),
        )
        category = MenuCategory.objects.create(
            name="Direct profit menu", slug="direct-profit-menu", service_area=MenuCategory.ServiceArea.BAR,
        )
        MenuItem.objects.create(category=category, name="Direct Cola", sku="DP-COLA", base_price=Decimal("1500.00"))
        record_stock_movement(
            item=item, location=location, movement_type=StockMovement.Type.OPENING,
            quantity_delta="20", source_key="dp-opening-1", actor=manager,
        )
        rows = product_profitability_projection(item_ids=[item.pk])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["price_basis"], "MENU_ITEM_MATCH")
        self.assertEqual(row["selling_price"], Decimal("1500.00"))
        self.assertEqual(row["expected_sales_revenue"], Decimal("30000.00"))
        self.assertEqual(row["expected_gross_profit"], Decimal("20000.00"))
        self.assertEqual(row["expected_gross_margin_percent"], Decimal("66.67"))


class ProcessTwiceDoesNotDeductTwiceTests(BaseAPITestCase):
    def test_manual_process_on_processed_hand_off_changes_nothing(self):
        clerk = make_staff("twice-clerk@staff.dev", role=User.Role.STOREKEEPER)
        cashier = make_staff("twice-cashier@staff.dev", role=User.Role.CASHIER)
        location = StockLocation.objects.create(code="TW-BAR", name="Twice bar")
        item = StockItem.objects.create(
            sku="TW-WATER", name="Twice Water", base_unit="BOTTLE", standard_cost=Decimal("150.00"),
        )
        category = MenuCategory.objects.create(
            name="Twice menu", slug="twice-menu", service_area=MenuCategory.ServiceArea.BAR,
        )
        menu_item = MenuItem.objects.create(
            category=category, name="Twice Water (menu)", sku="TW-MENU-WATER", base_price=Decimal("300.00"),
        )
        create_stock_recipe(
            menu_item=menu_item, location=location, tracking_mode="COMPONENTS",
            components=[{"item_id": item.pk, "quantity_per_menu_unit": "1"}],
            actor=clerk, idempotency_key="tw-recipe-1",
        )
        record_stock_movement(
            item=item, location=location, movement_type=StockMovement.Type.OPENING,
            quantity_delta="10", source_key="tw-opening-1", actor=clerk,
        )
        order, _ = create_order(
            mode=PosOrder.Mode.BAR, actor=cashier, table_number="T-1", guest_name="Walk-in",
            lines=[{"menu_item_id": menu_item.pk, "quantity": 2}], idempotency_key=None,
        )
        submit_order(order=order, actor=cashier)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=cashier)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=cashier)
        delivered = transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=cashier)
        # Delivery already deducted 2 bottles exactly once.
        self.assertEqual(StockBalance.objects.get(item=item, location=location).quantity_on_hand, Decimal("8.0000"))
        movements_before = StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count()
        from apps.inventory.models import StockConsumptionRequest
        hand_off = StockConsumptionRequest.objects.get(pos_order=delivered)
        self.assertEqual(hand_off.status, "PROCESSED")
        # Pressing Process again must resolve nothing and deduct nothing.
        resolved, created = process_pos_stock_consumption(request=hand_off, actor=clerk)
        self.assertFalse(created)
        self.assertEqual(resolved.status, "PROCESSED")
        self.assertEqual(StockBalance.objects.get(item=item, location=location).quantity_on_hand, Decimal("8.0000"))
        self.assertEqual(StockMovement.objects.filter(type=StockMovement.Type.POS_CONSUMPTION).count(), movements_before)
