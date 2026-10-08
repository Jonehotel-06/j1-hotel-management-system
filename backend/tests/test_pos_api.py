"""Capability-scoped POS API integration coverage."""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.stays.models import Stay

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class PosApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.cashier = make_staff("pos-api-cashier@staff.dev", role=User.Role.CASHIER)
        self.receptionist = make_staff("pos-api-reception@staff.dev", role=User.Role.RECEPTIONIST)
        room_type = make_room_type("POS API suite", price="10000.00")
        booking = make_booking(
            make_guest("pos-api-guest@example.com"), room_type,
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2), total="20000.00",
        )
        self.stay = Stay.objects.create(
            reference=f"STY-{booking.booking_reference}", booking=booking, guest=booking.guest,
            status=Stay.Status.IN_HOUSE, expected_arrival=booking.check_in, expected_departure=booking.check_out,
        )
        category = MenuCategory.objects.create(name="API menu", slug="api-menu")
        self.item = MenuItem.objects.create(
            category=category, name="Pepper soup", sku="API-SOUP", base_price=Decimal("1500.00"),
        )

    def test_cashier_can_create_and_advance_room_service_order_via_capability_api(self):
        self.auth(self.cashier)
        menu = self.client.get("/api/admin/pos/menu/")
        self.assertEqual(menu.status_code, 200, menu.content)
        response = self.client.post("/api/admin/pos/orders/", {
            "mode": "ROOM_SERVICE", "stay_id": self.stay.pk,
            "lines": [{"menu_item_id": self.item.pk, "quantity": 1}],
            "idempotency_key": "pos-api-key-1",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        reference = response.json()["data"]["reference"]
        duplicate = self.client.post("/api/admin/pos/orders/", {
            "mode": "ROOM_SERVICE", "stay_id": self.stay.pk,
            "lines": [{"menu_item_id": self.item.pk, "quantity": 1}],
            "idempotency_key": "pos-api-key-1",
        }, format="json")
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(duplicate.json()["data"]["reference"], reference)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{reference}/submit/", {}).status_code, 200)
        kitchen = self.client.get("/api/admin/pos/kitchen-tickets/")
        self.assertEqual(kitchen.status_code, 200, kitchen.content)
        ticket = kitchen.json()["data"][0]
        self.assertEqual(ticket["order_reference"], reference)
        self.assertEqual(ticket["order_mode"], "ROOM_SERVICE")
        self.assertEqual(ticket["order_guest_name"], self.stay.guest.full_name)
        self.assertIn(ticket["order_delivery_location"], ("", None))
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{reference}/status/", {"status": "PREPARING"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/admin/pos/orders/{reference}/status/", {"status": "READY"}, format="json").status_code, 200)
        finish = self.client.post(f"/api/admin/pos/orders/{reference}/status/", {"status": "DELIVERED"}, format="json")
        self.assertEqual(finish.status_code, 200, finish.content)
        self.assertEqual(PosOrder.objects.get(reference=reference).status, PosOrder.Status.DELIVERED)

    def test_cashier_can_search_bounded_in_house_room_service_stays(self):
        self.auth(self.cashier)
        response = self.client.get("/api/admin/pos/room-service-stays/", {"search": "pos-api-guest"})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.stay.pk)
        self.assertEqual(rows[0]["booking_reference"], self.stay.booking.booking_reference)
        self.assertEqual(rows[0]["guest_name"], self.stay.guest.full_name)

        self.stay.status = Stay.Status.CHECKED_OUT
        self.stay.save(update_fields=["status", "updated_at"])
        empty = self.client.get("/api/admin/pos/room-service-stays/")
        self.assertEqual(empty.status_code, 200, empty.content)
        self.assertEqual(empty.json()["data"], [])

    def test_only_manager_capability_can_manage_menu_catalog(self):
        manager = make_staff("pos-api-manager@staff.dev", role=User.Role.MANAGER)
        self.auth(self.cashier)
        forbidden = self.client.post("/api/admin/pos/menu/categories/", {
            "name": "Cashier should not create", "slug": "no-access",
        }, format="json")
        self.assertEqual(forbidden.status_code, 403, forbidden.content)
        self.auth(manager)
        created = self.client.post("/api/admin/pos/menu/categories/", {
            "name": "Manager specials", "slug": "manager-specials",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)

    def test_receptionist_is_not_granted_pos_endpoint_access(self):
        self.auth(self.receptionist)
        response = self.client.get("/api/admin/pos/orders/")
        self.assertEqual(response.status_code, 403, response.content)
