"""Department-scoped restaurant/bar menu, order, and production API tests."""
from decimal import Decimal

from apps.accounts.models import User
from apps.pos.models import KitchenTicket, MenuCategory, MenuItem, PosOrder

from .base import BaseAPITestCase
from .factories import make_staff


class PosDepartmentApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.restaurant_manager = make_staff("pos-dept-restaurant-manager@staff.dev", role=User.Role.RESTAURANT_MANAGER)
        self.waiter = make_staff("pos-dept-waiter@staff.dev", role=User.Role.WAITER)
        self.bar_manager = make_staff("pos-dept-bar-manager@staff.dev", role=User.Role.BAR_MANAGER)
        self.bartender = make_staff("pos-dept-bartender@staff.dev", role=User.Role.BARTENDER)
        self.chef = make_staff("pos-dept-chef@staff.dev", role=User.Role.CHEF)

        self.restaurant_category = MenuCategory.objects.create(
            name="Restaurant mains", slug="department-restaurant-mains",
            service_area=MenuCategory.ServiceArea.RESTAURANT,
        )
        self.bar_category = MenuCategory.objects.create(
            name="Bar drinks", slug="department-bar-drinks",
            service_area=MenuCategory.ServiceArea.BAR,
        )
        self.restaurant_item = MenuItem.objects.create(
            category=self.restaurant_category, name="Grilled chicken", sku="DEPT-REST-CHICKEN",
            base_price=Decimal("8500.00"),
        )
        self.bar_item = MenuItem.objects.create(
            category=self.bar_category, name="Ginger mocktail", sku="DEPT-BAR-MOCKTAIL",
            base_price=Decimal("2500.00"),
        )

    def _create_order(self, user, *, mode, item, key):
        self.auth(user)
        return self.client.post("/api/admin/pos/orders/", {
            "mode": mode,
            "lines": [{"menu_item_id": item.pk, "quantity": 1}],
            "idempotency_key": key,
        }, format="json")

    def test_menu_reads_and_menu_management_are_scoped_to_service_area(self):
        self.auth(self.waiter)
        restaurant_menu = self.client.get("/api/admin/pos/menu/")
        self.assertEqual(restaurant_menu.status_code, 200, restaurant_menu.content)
        self.assertEqual(
            {row["service_area"] for row in restaurant_menu.json()["data"]},
            {MenuCategory.ServiceArea.RESTAURANT},
        )
        self.assertEqual(self.client.get("/api/admin/pos/menu/?area=BAR").status_code, 403)

        self.auth(self.bartender)
        bar_menu = self.client.get("/api/admin/pos/menu/")
        self.assertEqual(bar_menu.status_code, 200, bar_menu.content)
        self.assertEqual(
            {row["service_area"] for row in bar_menu.json()["data"]},
            {MenuCategory.ServiceArea.BAR},
        )
        self.assertEqual(self.client.get("/api/admin/pos/menu/?area=RESTAURANT").status_code, 403)

        self.auth(self.restaurant_manager)
        categories = self.client.get("/api/admin/pos/menu/categories/")
        self.assertEqual(categories.status_code, 200, categories.content)
        self.assertTrue(all(row["service_area"] == "RESTAURANT" for row in categories.json()["data"]))

        deny_bar_category = self.client.post("/api/admin/pos/menu/categories/", {
            "name": "Restricted bar category", "slug": "restricted-bar-category", "service_area": "BAR",
        }, format="json")
        self.assertEqual(deny_bar_category.status_code, 403, deny_bar_category.content)
        deny_bar_item = self.client.post("/api/admin/pos/menu/items/", {
            "category": self.bar_category.pk, "name": "Restricted bar item", "sku": "DEPT-RESTRICTED-BAR",
            "base_price": "1000.00",
        }, format="json")
        self.assertEqual(deny_bar_item.status_code, 403, deny_bar_item.content)

        modifier = self.client.post("/api/admin/pos/menu/modifiers/", {
            "menu_item": self.restaurant_item.pk, "name": "Extra spice", "price_delta": "150.00",
        }, format="json")
        self.assertEqual(modifier.status_code, 201, modifier.content)
        cross_area_modifier_edit = self.client.patch(
            f"/api/admin/pos/menu/modifiers/{modifier.json()['data']['id']}/",
            {"menu_item": self.bar_item.pk}, format="json",
        )
        self.assertEqual(cross_area_modifier_edit.status_code, 403, cross_area_modifier_edit.content)

        cross_area_category_edit = self.client.patch(
            f"/api/admin/pos/menu/categories/{self.restaurant_category.pk}/",
            {"service_area": "BAR"}, format="json",
        )
        self.assertEqual(cross_area_category_edit.status_code, 403, cross_area_category_edit.content)

        self.auth(self.bar_manager)
        allowed_bar_category = self.client.post("/api/admin/pos/menu/categories/", {
            "name": "Bar specials", "slug": "department-bar-specials", "service_area": "BAR",
        }, format="json")
        self.assertEqual(allowed_bar_category.status_code, 201, allowed_bar_category.content)
        denied_restaurant_category = self.client.post("/api/admin/pos/menu/categories/", {
            "name": "Restricted restaurant category", "slug": "restricted-restaurant-category",
            "service_area": "RESTAURANT",
        }, format="json")
        self.assertEqual(denied_restaurant_category.status_code, 403, denied_restaurant_category.content)

    def test_order_creation_listing_detail_and_production_tickets_are_department_scoped(self):
        restaurant_response = self._create_order(
            self.waiter, mode=PosOrder.Mode.RESTAURANT, item=self.restaurant_item, key="dept-rest-order-1",
        )
        self.assertEqual(restaurant_response.status_code, 201, restaurant_response.content)
        restaurant_ref = restaurant_response.json()["data"]["reference"]

        wrong_area_line = self._create_order(
            self.waiter, mode=PosOrder.Mode.RESTAURANT, item=self.bar_item, key="dept-rest-wrong-area",
        )
        self.assertEqual(wrong_area_line.status_code, 400, wrong_area_line.content)
        forbidden_bar_order = self._create_order(
            self.waiter, mode=PosOrder.Mode.BAR, item=self.bar_item, key="dept-rest-forbidden-bar",
        )
        self.assertEqual(forbidden_bar_order.status_code, 403, forbidden_bar_order.content)

        bar_response = self._create_order(
            self.bartender, mode=PosOrder.Mode.BAR, item=self.bar_item, key="dept-bar-order-1",
        )
        self.assertEqual(bar_response.status_code, 201, bar_response.content)
        bar_ref = bar_response.json()["data"]["reference"]

        # Reusing another department's idempotency key must not disclose its
        # existing order even though the caller may create restaurant orders.
        cross_department_key = self._create_order(
            self.waiter, mode=PosOrder.Mode.RESTAURANT, item=self.restaurant_item, key="dept-bar-order-1",
        )
        self.assertEqual(cross_department_key.status_code, 403, cross_department_key.content)

        self.auth(self.waiter)
        restaurant_orders = self.client.get("/api/admin/pos/orders/")
        self.assertEqual(restaurant_orders.status_code, 200, restaurant_orders.content)
        self.assertTrue(all(row["mode"] in {"RESTAURANT", "TAKEAWAY"} for row in restaurant_orders.json()["data"]))
        self.assertEqual(self.client.get("/api/admin/pos/orders/?mode=BAR").status_code, 403)
        self.assertEqual(self.client.get(f"/api/admin/pos/orders/{bar_ref}/").status_code, 403)

        self.auth(self.bartender)
        bar_orders = self.client.get("/api/admin/pos/orders/")
        self.assertEqual(bar_orders.status_code, 200, bar_orders.content)
        self.assertTrue(all(row["mode"] == "BAR" for row in bar_orders.json()["data"]))
        self.assertEqual(self.client.get(f"/api/admin/pos/orders/{restaurant_ref}/").status_code, 403)

        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{restaurant_ref}/submit/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{bar_ref}/submit/", {}, format="json").status_code, 200)

        self.auth(self.waiter)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{restaurant_ref}/submit/", {}, format="json").status_code, 200)
        restaurant_ticket = KitchenTicket.objects.get(order__reference=restaurant_ref)
        bar_ticket = KitchenTicket.objects.get(order__reference=bar_ref)
        self.assertEqual(restaurant_ticket.station, KitchenTicket.Station.KITCHEN)
        self.assertEqual(bar_ticket.station, KitchenTicket.Station.BAR)

        restaurant_queue = self.client.get("/api/admin/pos/kitchen-tickets/")
        self.assertEqual(restaurant_queue.status_code, 200, restaurant_queue.content)
        self.assertEqual({row["station"] for row in restaurant_queue.json()["data"]}, {"KITCHEN"})
        self.assertEqual(self.client.get("/api/admin/pos/kitchen-tickets/?station=BAR").status_code, 403)

        self.auth(self.bartender)
        bar_queue = self.client.get("/api/admin/pos/kitchen-tickets/")
        self.assertEqual(bar_queue.status_code, 200, bar_queue.content)
        self.assertEqual({row["station"] for row in bar_queue.json()["data"]}, {"BAR"})
        bar_started = self.client.post(
            f"/api/admin/pos/kitchen-tickets/{bar_ticket.pk}/status/", {"status": "PREPARING"}, format="json",
        )
        self.assertEqual(bar_started.status_code, 200, bar_started.content)

        self.auth(self.chef)
        kitchen_queue = self.client.get("/api/admin/pos/kitchen-tickets/")
        self.assertEqual(kitchen_queue.status_code, 200, kitchen_queue.content)
        self.assertEqual({row["station"] for row in kitchen_queue.json()["data"]}, {"KITCHEN"})
        self.assertEqual(self.client.get("/api/admin/pos/kitchen-tickets/?station=BAR").status_code, 403)
        denied_bar_ticket = self.client.post(
            f"/api/admin/pos/kitchen-tickets/{bar_ticket.pk}/status/", {"status": "PREPARING"}, format="json",
        )
        self.assertEqual(denied_bar_ticket.status_code, 403, denied_bar_ticket.content)
        started = self.client.post(
            f"/api/admin/pos/kitchen-tickets/{restaurant_ticket.pk}/status/", {"status": "PREPARING"}, format="json",
        )
        self.assertEqual(started.status_code, 200, started.content)
        self.assertEqual(started.json()["data"]["station"], "KITCHEN")
        ready = self.client.post(
            f"/api/admin/pos/kitchen-tickets/{restaurant_ticket.pk}/status/", {"status": "READY"}, format="json",
        )
        self.assertEqual(ready.status_code, 200, ready.content)
        restaurant_ticket.refresh_from_db()
        self.assertEqual(restaurant_ticket.status, KitchenTicket.Status.READY)
        self.assertEqual(PosOrder.objects.get(reference=restaurant_ref).status, PosOrder.Status.READY)
