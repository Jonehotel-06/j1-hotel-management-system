"""Focused runtime evidence for the critical-repair integration surfaces.

These are deliberately small API/service checks, not a replacement for the
full regression suite. They prove that the new browser-facing endpoint map
resolves, authorisation is enforced, configuration is server-owned, and
idempotent writes do not duplicate stock or revenue evidence.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bookings.models import BookingExtension
from apps.bookings.services.extension_service import extend_booking, quote_extension
from apps.core.utils import hotel_today
from apps.guest_services.models import MiniMartCategory, MiniMartProduct
from apps.guest_services.services_amenities import record_breakfast
from apps.inventory.models import (
    StockBalance, StockItem, StockLocation, StockMovement, StockUnit,
)
from apps.inventory.services.inventory_service import record_stock_movement

from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class CriticalRepairsRuntimeTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("critical-admin@staff.dev", role=User.Role.ADMIN)
        self.keeper = make_staff("critical-keeper@staff.dev", role=User.Role.STOREKEEPER)
        self.receptionist = make_staff("critical-reception@staff.dev", role=User.Role.RECEPTIONIST)
        self.main = StockLocation.objects.create(code="MAIN", name="Main Store")
        self.reception = StockLocation.objects.create(code="FRONT-DESK", name="Front Desk", is_reception=True)
        self.item = StockItem.objects.create(
            sku="CRIT-WATER", name="Bottled water", base_unit="BOTTLE",
            standard_cost=Decimal("100.00"), reorder_level=Decimal("2.0000"),
        )
        record_stock_movement(
            item=self.item, location=self.main, movement_type=StockMovement.Type.OPENING,
            quantity_delta=Decimal("20.0000"), source_key="critical-opening", actor=self.keeper,
        )
        record_stock_movement(
            item=self.item, location=self.reception, movement_type=StockMovement.Type.OPENING,
            quantity_delta=Decimal("5.0000"), source_key="critical-reception-opening", actor=self.keeper,
        )
        self.client.force_authenticate(user=self.admin)

    def payload(self, response):
        body = response.json()
        return body.get("data", body)

    def test_configured_units_are_server_owned_and_endpoint_rejects_anonymous_writes(self):
        created = self.client.post("/api/admin/inventory/units/", {
            "code": "PORTION", "label": "Kitchen portion", "description": "One plated portion",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["data"]["code"], "PORTION")
        listed = self.client.get("/api/admin/inventory/units/?active=true")
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertTrue(any(row["code"] == "PORTION" for row in self.payload(listed)))
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post("/api/admin/inventory/units/", {"code": "CUP"}, format="json").status_code, 401)

    def test_manual_stock_out_and_kitchen_issue_are_idempotent_and_do_not_create_pos_orders(self):
        from apps.pos.models import PosOrder

        for path, body in (
            ("/api/admin/inventory/manual-stock-out/", {
                "item_id": self.item.pk, "source_location_id": self.main.pk,
                "quantity": "2", "unit": "BOTTLE", "reason": "GENERAL",
                "department": "HOUSEKEEPING", "recipient": "Test Housekeeper",
                "idempotency_key": "critical-manual-1",
            }),
            ("/api/admin/inventory/kitchen-issues/", {
                "item_id": self.item.pk, "source_location_id": self.main.pk,
                "quantity": "1", "unit": "BOTTLE", "reason": "KITCHEN",
                "department": "KITCHEN", "recipient": "Test Chef",
                "idempotency_key": "critical-kitchen-1",
            }),
        ):
            first = self.client.post(path, body, format="json")
            self.assertEqual(first.status_code, 201, first.content)
            second = self.client.post(path, body, format="json")
            self.assertEqual(second.status_code, 200, second.content)
            movement_reference = first.json()["data"]["transaction"]["movement_reference"]
            self.assertEqual(second.json()["data"]["transaction"]["movement_reference"], movement_reference)
            self.assertEqual(StockMovement.objects.filter(reference=movement_reference).count(), 1)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.main).quantity_on_hand, Decimal("17.0000"))
        self.assertEqual(PosOrder.objects.count(), 0)

    def test_minimart_catalogue_admin_creates_one_stock_item_and_activation_edits(self):
        unit = self.client.post("/api/admin/inventory/units/", {"code": "CARTON", "label": "Carton"}, format="json")
        self.assertEqual(unit.status_code, 201, unit.content)
        category = self.client.post("/api/admin/inventory/minimart/categories/", {
            "name": "Chilled drinks", "description": "Fridge",
        }, format="json")
        self.assertEqual(category.status_code, 201, category.content)
        category_id = category.json()["data"]["id"]
        product = self.client.post("/api/admin/inventory/minimart/products/", {
            "name": "Test Juice", "sku": "CRIT-JUICE", "category_id": category_id,
            "purchase_cost": "200.00", "selling_price": "350.00",
            "base_unit": "CARTON", "reorder_level": "1",
            "location_id": self.main.pk, "description": "One-litre carton",
            "idempotency_key": "critical-product-1",
        }, format="json")
        self.assertEqual(product.status_code, 201, product.content)
        product_id = product.json()["data"]["id"]
        self.assertEqual(StockItem.objects.filter(sku="CRIT-JUICE").count(), 1)
        self.assertEqual(MiniMartProduct.objects.get(pk=product_id).category_id, category_id)
        active = self.client.get("/api/admin/service-requests/reception/minimart/products/?search=juice")
        self.assertEqual(active.status_code, 200, active.content)
        catalogue = self.payload(active)
        self.assertTrue(any(row["sku"] == "CRIT-JUICE" for row in catalogue["results"]))
        patched = self.client.patch(f"/api/admin/inventory/minimart/products/{product_id}/", {
            "selling_price": "400.00", "is_active": False,
        }, format="json")
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(MiniMartProduct.objects.get(pk=product_id).selling_price, Decimal("400.00"))
        self.assertFalse(MiniMartProduct.objects.get(pk=product_id).is_active)
        self.client.force_authenticate(user=self.receptionist)
        forbidden = self.client.post("/api/admin/inventory/minimart/categories/", {"name": "Nope"}, format="json")
        self.assertIn(forbidden.status_code, (401, 403), forbidden.content)

    def test_configured_reception_location_drives_allocation_and_amenity_stock(self):
        self.client.force_authenticate(user=self.keeper)
        allocated = self.client.post("/api/admin/service-requests/reception/amenities/allocate/", {
            "item_id": self.item.pk, "from_location_id": self.main.pk,
            "quantity": "3", "notes": "Front desk allocation",
            "idempotency_key": "critical-allocation-1",
        }, format="json")
        self.assertEqual(allocated.status_code, 200, allocated.content)
        self.assertTrue(allocated.json()["data"]["created"])
        self.client.force_authenticate(user=self.receptionist)
        stock = self.client.get("/api/admin/service-requests/reception/amenities/stock/?location_id=" + str(self.reception.pk))
        self.assertEqual(stock.status_code, 200, stock.content)
        self.assertEqual(stock.json()["data"]["location"]["id"], self.reception.pk)
        self.assertTrue(stock.json()["data"]["location"]["is_reception"])
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.reception).quantity_on_hand, Decimal("8.0000"))

    def test_breakfast_optional_stock_consumption_is_single_ledger_issue_without_finance_posting(self):
        from apps.finance.models import FinancialLine
        from apps.pos.models import PosOrder

        today = hotel_today()
        guest = make_guest("critical-breakfast@guest.dev")
        from apps.bookings.services.booking_service import check_in_booking
        from tests.factories import make_room

        room_type = make_room_type(name="Critical Suite")
        booking = make_booking(guest, room_type, rooms=[make_room(room_type, "811")], check_in=today, check_out=today + timedelta(days=1))
        check_in_booking(booking, staff_user=self.receptionist)
        record, created = record_breakfast(
            guest=guest, booking=booking, service_date=today,
            items=[{"item_id": self.item.pk, "quantity": "2", "unit": "BOTTLE"}],
            location_id=self.main.pk, actor=self.receptionist,
            idempotency_key="critical-breakfast-1",
        )
        self.assertTrue(created)
        self.assertEqual(record.stock_movement_count, 1)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.main).quantity_on_hand, Decimal("18.0000"))
        self.assertEqual(PosOrder.objects.count(), 0)
        self.assertEqual(FinancialLine.objects.count(), 0)
        replay, replay_created = record_breakfast(
            guest=guest, booking=booking, service_date=today,
            items=[{"item_id": self.item.pk, "quantity": "2", "unit": "BOTTLE"}],
            location_id=self.main.pk, actor=self.receptionist,
            idempotency_key="critical-breakfast-1",
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, record.pk)
        self.assertEqual(StockBalance.objects.get(item=self.item, location=self.main).quantity_on_hand, Decimal("18.0000"))

    def test_stay_extension_persists_server_derived_breakdown_and_replays_idempotently(self):
        from apps.bookings.models import Booking

        today = hotel_today()
        guest = make_guest("critical-extension@guest.dev")
        room_type = make_room_type(name="Extension Suite")
        room = make_room(room_type, "901")
        booking = make_booking(
            guest, room_type, rooms=[room], check_in=today - timedelta(days=1),
            check_out=today + timedelta(days=1), status=Booking.Status.CHECKED_IN,
            amount_paid="0.00", total="15000.00",
        )
        quote = quote_extension(booking=booking, additional_nights=2, nightly_rate=Decimal("12000.00"))
        self.assertEqual(len(quote["nightly_breakdown"]), 2)
        self.assertEqual(Decimal(quote["extension_amount"]), Decimal("24000.00"))
        result = extend_booking(
            booking=booking, staff_user=self.admin, additional_nights=2,
            nightly_rate=Decimal("12000.00"), amount_paid=Decimal("24000.00"),
            provider="CASH", source_key="critical-extension-1",
        )
        self.assertIsInstance(result, BookingExtension)
        extension = BookingExtension.objects.get(source_key="critical-extension-1")
        self.assertEqual(len(extension.breakdown), 2)
        self.assertEqual(sum(Decimal(row["amount"]) for row in extension.breakdown), Decimal("24000.00"))
        replay = extend_booking(
            booking=booking, staff_user=self.admin, additional_nights=2,
            nightly_rate=Decimal("12000.00"), amount_paid=Decimal("24000.00"),
            provider="CASH", source_key="critical-extension-1",
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(BookingExtension.objects.filter(source_key="critical-extension-1").count(), 1)

    def test_endpoint_map_resolves_all_new_browser_resources(self):
        from django.urls import resolve

        for path in (
            "/api/admin/inventory/units/",
            "/api/admin/inventory/manual-stock-out/",
            "/api/admin/inventory/kitchen-issues/",
            "/api/admin/inventory/minimart/categories/",
            "/api/admin/inventory/minimart/products/",
        ):
            self.assertTrue(resolve(path).func, path)
        self.assertEqual(reverse("admin-service-requests:breakfast-issue"), "/api/admin/service-requests/breakfast/issue/")
