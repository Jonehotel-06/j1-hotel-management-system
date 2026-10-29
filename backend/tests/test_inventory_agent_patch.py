"""Regression tests for the isolated inventory patch; uses Django's test database."""
from decimal import Decimal
from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from .base import BaseAPITestCase
from .factories import make_staff


class InventoryAgentPatchTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user = make_staff("agent-inventory@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.auth(self.user)
        self.loc = StockLocation.objects.create(code="AGENT", name="Agent store")
        self.item = StockItem.objects.create(sku="AGENT-BOTTLE", name="Bottle", base_unit="BOTTLE", standard_cost=Decimal("10.00"), reorder_level=2)
        self.url = "/api/admin/inventory/opening-stock/"

    def payload(self, **kwargs):
        return dict({"item_id": self.item.pk, "location_id": self.loc.pk, "quantity": "2", "unit": "CARTON", "conversion_factor": "12", "idempotency_key": "agent-key"}, **kwargs)

    def test_opening_explicit_factor_audit_and_idempotency(self):
        payload = self.payload()
        r = self.client.post(self.url, payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, 200)
        m = StockMovement.objects.get(item=self.item)
        self.assertEqual(m.quantity_delta, Decimal("24"))
        self.assertEqual(m.unit_cost, Decimal("10"))
        self.assertEqual(m.metadata["entered_unit"], "CARTON")
        self.assertEqual(m.metadata["conversion_factor"], "12.0000")
        self.assertEqual(m.actor_id, self.user.pk)
        self.assertEqual(StockBalance.objects.get(item=self.item).quantity_on_hand, Decimal("24"))
        changed = self.client.post(self.url, self.payload(quantity="3"), format="json")
        self.assertEqual(changed.status_code, 400, changed.content)
        repeated = self.client.post(self.url, self.payload(idempotency_key="different"), format="json")
        self.assertEqual(repeated.status_code, 400, repeated.content)
        self.assertEqual(StockMovement.objects.filter(item=self.item).count(), 1)

    def test_conversion_rejected_without_factor_even_if_item_has_pack_size(self):
        self.item.units_per_pack = 12
        self.item.save()
        r = self.client.post(self.url, self.payload(conversion_factor=None), format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(StockMovement.objects.exists())

    def test_invalid_quantity_cost_factor_and_location(self):
        for updates in ({"quantity":"0"}, {"conversion_factor":"0"}, {"unit_cost":"-1"}, {"location_id":99999}):
            r = self.client.post(self.url, self.payload(**updates), format="json")
            self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(StockMovement.objects.exists())

    def test_item_creation_opening_is_atomic_and_historical_cost_stays_fixed(self):
        url = "/api/admin/inventory/items/"
        payload = {"sku":"AGENT-NEW", "name":"New", "base_unit":"BOTTLE", "standard_cost":"4.00", "opening_quantity":"2", "opening_location_id":self.loc.pk, "opening_unit":"CARTON", "opening_conversion_factor":"6"}
        bad = dict(payload, opening_location_id=99999)
        r = self.client.post(url, bad, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(StockItem.objects.filter(sku="AGENT-NEW").exists())
        r = self.client.post(url, payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        item = StockItem.objects.get(sku="AGENT-NEW")
        m = StockMovement.objects.get(item=item)
        self.assertEqual(m.quantity_delta, Decimal("12"))
        self.assertEqual(m.unit_cost, Decimal("4"))
        item.standard_cost = Decimal("9")
        item.save()
        m.refresh_from_db()
        self.assertEqual(m.unit_cost, Decimal("4"))
        self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)
        self.assertEqual(StockMovement.objects.filter(item=item).count(), 1)

    def test_stock_state_threshold(self):
        for qty, expected in (("0", "OUT_OF_STOCK"), ("2", "LOW_STOCK"), ("3", "ADEQUATE")):
            StockBalance.objects.update_or_create(item=self.item, location=self.loc, defaults={"quantity_on_hand":qty})
            r = self.client.get("/api/admin/inventory/balances/")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.json()["data"][0]["stock_state"], expected)

    def test_unauthorised_opening(self):
        self.auth(make_staff("agent-cashier@staff.dev", role=User.Role.CASHIER))
        self.assertEqual(self.client.post(self.url, self.payload(), format="json").status_code, 403)

    def test_ledger_filters_use_actual_transfer_and_return_values(self):
        from apps.inventory.services.inventory_service import record_stock_movement
        for i, kind in enumerate(("TRANSFER_OUT", "TRANSFER_IN", "POS_RETURN", "MAINTENANCE_RETURN")):
            record_stock_movement(item=self.item, location=self.loc, movement_type=kind,
                                  quantity_delta="1", source_key=f"filter-{i}", actor=self.user)
        for kind in ("TRANSFER_OUT", "TRANSFER_IN", "POS_RETURN", "MAINTENANCE_RETURN"):
            r = self.client.get("/api/admin/inventory/movements/", {"type": kind, "item_id": self.item.pk, "location_id": self.loc.pk})
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual([x["type"] for x in r.json()["data"]], [kind])

    def test_multiple_ingredients_scarcest_limits_servings(self):
        from apps.inventory.services.inventory_service import create_stock_recipe, pos_stock_summaries
        from apps.pos.models import MenuCategory, MenuItem
        ingredient = StockItem.objects.create(sku="AGENT-RICE", name="Rice", base_unit="KG")
        category = MenuCategory.objects.create(name="Agent kitchen", slug="agent-kitchen")
        menu = MenuItem.objects.create(category=category, name="Agent dish", sku="AGENT-DISH", base_price="100")
        create_stock_recipe(menu_item=menu, location=self.loc, tracking_mode="COMPONENTS", actor=self.user,
            idempotency_key="agent-recipe", components=[
                {"item_id": ingredient.pk, "quantity_per_menu_unit": "250", "unit": "g"},
                {"item_id": self.item.pk, "quantity_per_menu_unit": "2"},
            ])
        StockBalance.objects.create(item=ingredient, location=self.loc, quantity_on_hand="5")
        StockBalance.objects.create(item=self.item, location=self.loc, quantity_on_hand="6")
        self.assertEqual(pos_stock_summaries([menu.pk])[menu.pk]["saleable_quantity"], "3")
        StockBalance.objects.filter(item=self.item, location=self.loc).update(quantity_on_hand=0)
        self.assertEqual(pos_stock_summaries([menu.pk])[menu.pk]["saleable_quantity"], "0")
