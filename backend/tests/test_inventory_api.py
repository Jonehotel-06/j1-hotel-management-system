"""Capability-scoped inventory/procurement API lifecycle coverage."""
from apps.accounts.models import User
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import create_order, submit_order, transition_order

from .base import BaseAPITestCase
from .factories import make_staff


class InventoryApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.clerk = make_staff("inventory-api-clerk@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.manager = make_staff("inventory-api-manager@staff.dev", role=User.Role.MANAGER)

    def test_clerk_procures_and_receives_while_manager_is_separate_approver(self):
        self.auth(self.clerk)
        supplier = self.client.post("/api/admin/inventory/suppliers/", {"name": "API Supplier"}, format="json")
        self.assertEqual(supplier.status_code, 201, supplier.content)
        supplier_detail = self.client.get(f"/api/admin/inventory/suppliers/{supplier.json()['data']['id']}/")
        self.assertEqual(supplier_detail.status_code, 200, supplier_detail.content)
        self.assertEqual(supplier_detail.json()["data"]["name"], "API Supplier")
        location = self.client.post("/api/admin/inventory/locations/", {"code": "API-STORE", "name": "API Store"}, format="json")
        self.assertEqual(location.status_code, 201, location.content)
        location_search = self.client.get("/api/admin/inventory/locations/", {"search": "API-STORE", "active": "true"})
        self.assertEqual(location_search.status_code, 200, location_search.content)
        self.assertEqual(location_search.json()["data"][0]["id"], location.json()["data"]["id"])
        item = self.client.post("/api/admin/inventory/items/", {
            "sku": "API-WATER", "name": "Bottled water", "base_unit": "BOTTLE", "standard_cost": "200.00",
        }, format="json")
        self.assertEqual(item.status_code, 201, item.content)
        po = self.client.post("/api/admin/inventory/purchase-orders/", {
            "supplier_id": supplier.json()["data"]["id"],
            "delivery_location_id": location.json()["data"]["id"],
            "idempotency_key": "api-po-one",
            "lines": [{"item_id": item.json()["data"]["id"], "quantity_ordered": "4", "unit_cost": "220"}],
        }, format="json")
        self.assertEqual(po.status_code, 201, po.content)
        reference = po.json()["data"]["reference"]
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/submit/", {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/approve/", {}, format="json").status_code, 403)

        self.auth(self.manager)
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/approve/", {}, format="json").status_code, 200)
        self.auth(self.clerk)
        self.assertEqual(self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/ordered/", {}, format="json").status_code, 200)
        detail = self.client.get(f"/api/admin/inventory/purchase-orders/{reference}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        line_id = detail.json()["data"]["lines"][0]["id"]
        receipt = self.client.post(f"/api/admin/inventory/purchase-orders/{reference}/receipts/", {
            "idempotency_key": "api-receipt-one",
            "lines": [{"purchase_order_line_id": line_id, "quantity_received": "4"}],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.content)
        balances = self.client.get("/api/admin/inventory/balances/")
        self.assertEqual(balances.status_code, 200, balances.content)
        self.assertEqual(balances.json()["data"][0]["quantity_on_hand"], "4.0000")

        stock_count = self.client.post("/api/admin/inventory/stock-counts/", {
            "location_id": location.json()["data"]["id"], "item_ids": [item.json()["data"]["id"]],
        }, format="json")
        self.assertEqual(stock_count.status_code, 201, stock_count.content)
        count_reference = stock_count.json()["data"]["reference"]
        count_line_id = stock_count.json()["data"]["lines"][0]["id"]
        submitted = self.client.post(f"/api/admin/inventory/stock-counts/{count_reference}/submit/", {
            "lines": [{"line_id": count_line_id, "counted_quantity": "3"}],
        }, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.assertEqual(submitted.json()["data"]["status"], "PENDING_APPROVAL")
        self.assertEqual(self.client.post(f"/api/admin/inventory/stock-counts/{count_reference}/approve/", {}, format="json").status_code, 403)
        self.auth(self.manager)
        approved = self.client.post(f"/api/admin/inventory/stock-counts/{count_reference}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertEqual(approved.json()["data"]["status"], "POSTED")
        balances = self.client.get("/api/admin/inventory/balances/")
        self.assertEqual(balances.json()["data"][0]["quantity_on_hand"], "3.0000")

    def test_clerk_configures_recipe_and_processes_inventory_owned_pos_handoff(self):
        from apps.inventory.models import StockItem, StockLocation, StockMovement
        from apps.inventory.services.inventory_service import record_stock_movement

        self.auth(self.clerk)
        location = self.client.post("/api/admin/inventory/locations/", {"code": "POS-STORE", "name": "POS Store"}, format="json")
        item = self.client.post("/api/admin/inventory/items/", {
            "sku": "POS-INGREDIENT", "name": "POS ingredient", "base_unit": "UNIT", "standard_cost": "50.00",
        }, format="json")
        self.assertEqual(location.status_code, 201, location.content)
        self.assertEqual(item.status_code, 201, item.content)
        category = MenuCategory.objects.create(name="API POS", slug="api-pos")
        menu_item = MenuItem.objects.create(category=category, name="API Drink", sku="API-POS-DRINK", base_price="200.00")
        menu_directory = self.client.get("/api/admin/inventory/menu-items/", {"search": "API-POS"})
        self.assertEqual(menu_directory.status_code, 200, menu_directory.content)
        self.assertEqual(menu_directory.json()["data"][0]["id"], menu_item.pk)
        self.assertEqual(menu_directory.json()["data"][0]["category_name"], category.name)
        cashier = make_staff("inventory-menu-directory-denied@staff.dev", role=User.Role.CASHIER)
        self.auth(cashier)
        self.assertEqual(self.client.get("/api/admin/inventory/menu-items/").status_code, 403)
        self.auth(self.clerk)
        recipe = self.client.post("/api/admin/inventory/recipes/", {
            "menu_item_id": menu_item.pk, "location_id": location.json()["data"]["id"],
            "components": [{"item_id": item.json()["data"]["id"], "quantity_per_menu_unit": "1"}],
            "idempotency_key": "api-recipe-one",
        }, format="json")
        self.assertEqual(recipe.status_code, 201, recipe.content)
        self.assertEqual(recipe.json()["data"]["version"], 1)

        stock_item = StockItem.objects.get(pk=item.json()["data"]["id"])
        stock_location = StockLocation.objects.get(pk=location.json()["data"]["id"])
        record_stock_movement(item=stock_item, location=stock_location, movement_type=StockMovement.Type.OPENING,
                              quantity_delta="2", source_key="api-pos-opening", actor=self.clerk)
        order, _ = create_order(mode=PosOrder.Mode.TAKEAWAY, actor=self.clerk, lines=[{"menu_item_id": menu_item.pk, "quantity": 1}])
        submit_order(order=order, actor=self.clerk)
        transition_order(order=order, target_status=PosOrder.Status.PREPARING, actor=self.clerk)
        transition_order(order=order, target_status=PosOrder.Status.READY, actor=self.clerk)
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.clerk)

        queue = self.client.get("/api/admin/inventory/consumption-requests/?status=PENDING")
        self.assertEqual(queue.status_code, 200, queue.content)
        consumption_reference = queue.json()["data"][0]["reference"]
        processed = self.client.post(f"/api/admin/inventory/consumption-requests/{consumption_reference}/process/", {}, format="json")
        self.assertEqual(processed.status_code, 200, processed.content)
        self.assertEqual(processed.json()["data"]["status"], "PROCESSED")
        self.assertEqual(self.client.get("/api/admin/inventory/balances/").json()["data"][0]["quantity_on_hand"], "1.0000")
