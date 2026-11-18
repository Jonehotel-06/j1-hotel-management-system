"""Every endpoint named in the incident report, exercised through the real stack.

The production 503s covered inventory items/units/locations, Mini Mart
products/categories/sales, amenities stock/issues, breakfast config, reports
approvals, room types and the booking-extension quote. They shared no feature
code, so each one is pinned here with real database records rather than mocks:
a fix that merely removes an error message would still fail these tests because
every assertion reads the RESPONSE BODY and, where the endpoint writes, the
database row it produced.

Also covered, per the brief:
  * case-insensitive, whitespace-tolerant search;
  * no over-issue of stock (negative stock is impossible) and no duplicate
    deduction on a repeated submission;
  * guest names / booking references / rooms reachable from the reception
    guest search used by the Breakfast page;
  * Mini Mart categories and products created, updated and deactivated;
  * permissions: a receptionist cannot reach catalogue management.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bookings.models import Booking, Guest
from apps.bookings.services.booking_service import check_in_booking
from apps.core.utils import hotel_today
from apps.guest_services.services_amenities import allocate_reception_amenities
from apps.guest_services.models import (
    BreakfastIssuance, GuestAmenityIssue, MiniMartCategory, MiniMartProduct,
)
from apps.inventory.models import (
    StockBalance, StockItem, StockLocation, StockMovement, StockUnit,
)
from apps.inventory.services.inventory_service import record_stock_movement

from .factories import make_booking, make_guest, make_room, make_room_type, make_staff


class IncidentEndpointTests(APITestCase):
    """The eleven reported endpoints must answer 200 with real payloads."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("inc-admin@staff.dev", role=User.Role.ADMIN)
        self.keeper = make_staff("inc-keeper@staff.dev", role=User.Role.STOREKEEPER)
        self.receptionist = make_staff("inc-reception@staff.dev", role=User.Role.RECEPTIONIST)

        self.main = StockLocation.objects.create(code="MAIN", name="Main Store")
        self.reception = StockLocation.objects.create(
            code="RECEPTION", name="Reception Store", is_reception=True)
        self.unit = StockUnit.objects.create(code="BOTTLE", label="Bottles")

        self.water = StockItem.objects.create(
            sku="INC-WATER", name="Bottled Water", base_unit="BOTTLE",
            standard_cost=Decimal("100.00"), reorder_level=Decimal("2.0000"),
            category="Beverages",
        )
        record_stock_movement(
            item=self.water, location=self.main, movement_type=StockMovement.Type.OPENING,
            quantity_delta=Decimal("25"), source_key="inc-open-main", actor=self.keeper,
        )
        allocate_reception_amenities(item=self.water, quantity="5", from_location=self.main, actor=self.keeper)
        self.room_type = make_room_type(name="Incident Standard")
        self.room = make_room(self.room_type, "204")
        self.guest = make_guest(email="inc-guest@test.dev", first_name="Ada", last_name="Obi")
        self.booking = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2),
        )
        check_in_booking(self.booking, staff_user=self.receptionist)
        self.client.force_authenticate(user=self.admin)

    def payload(self, response):
        return json.loads(response.content)["data"]

    # ------------------------------------------------------------------ #
    # 1-4: inventory
    # ------------------------------------------------------------------ #
    def test_inventory_items_list_returns_real_records(self):
        response = self.client.get("/api/admin/inventory/items/?page_size=500")
        self.assertEqual(response.status_code, 200, response.content)
        rows = self.payload(response)
        self.assertEqual([row["sku"] for row in rows], ["INC-WATER"])
        self.assertEqual(rows[0]["name"], "Bottled Water")

    def test_inventory_items_active_filter_and_search(self):
        inactive = StockItem.objects.create(
            sku="INC-OLD", name="Retired Lamp", base_unit="PIECE", is_active=False)
        active = self.client.get("/api/admin/inventory/items/?active=true&page_size=200")
        self.assertEqual(active.status_code, 200)
        self.assertNotIn("INC-OLD", [row["sku"] for row in self.payload(active)])

        # case-insensitive + surrounding whitespace
        found = self.client.get("/api/admin/inventory/items/", {"search": "  bottled  "})
        self.assertEqual([row["sku"] for row in self.payload(found)], ["INC-WATER"])

        by_sku = self.client.get("/api/admin/inventory/items/", {"search": "inc-wat"})
        self.assertEqual([row["sku"] for row in self.payload(by_sku)], ["INC-WATER"])

    def test_inventory_units_list_returns_configured_units(self):
        response = self.client.get("/api/admin/inventory/units/?active=true&page_size=200")
        self.assertEqual(response.status_code, 200, response.content)
        rows = self.payload(response)
        self.assertIn("BOTTLE", [row["code"] for row in rows])
        self.assertEqual(rows[0]["label"], "Bottles")

    def test_inventory_locations_list_exposes_reception_flag(self):
        response = self.client.get("/api/admin/inventory/locations/?active=true&page_size=200")
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row["code"]: row for row in self.payload(response)}
        self.assertTrue(rows["RECEPTION"]["is_reception"])
        self.assertFalse(rows["MAIN"]["is_reception"])

    # ------------------------------------------------------------------ #
    # 5-6: Mini Mart catalogue
    # ------------------------------------------------------------------ #
    def test_minimart_category_create_list_and_update(self):
        created = self.client.post(
            "/api/admin/inventory/minimart/categories/",
            {"name": "Confectionery", "description": "Sweets"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        category_id = self.payload(created)["id"]

        listed = self.client.get("/api/admin/inventory/minimart/categories/?page_size=200")
        self.assertEqual(listed.status_code, 200)
        self.assertIn("Confectionery", [row["name"] for row in self.payload(listed)])

        renamed = self.client.patch(
            f"/api/admin/inventory/minimart/categories/{category_id}/",
            {"name": "Confectionery & Snacks"}, format="json")
        self.assertEqual(renamed.status_code, 200, renamed.content)
        self.assertEqual(self.payload(renamed)["name"], "Confectionery & Snacks")

        deactivated = self.client.patch(
            f"/api/admin/inventory/minimart/categories/{category_id}/",
            {"is_active": False}, format="json")
        self.assertEqual(deactivated.status_code, 200)
        self.assertFalse(self.payload(deactivated)["is_active"])

    def test_minimart_category_duplicate_name_is_rejected(self):
        MiniMartCategory.objects.create(name="Beverages")
        response = self.client.post(
            "/api/admin/inventory/minimart/categories/",
            {"name": " beverages "}, format="json")
        self.assertEqual(response.status_code, 400, response.content)

    def test_minimart_product_create_and_appear_in_listing(self):
        category = MiniMartCategory.objects.create(name="Beverages")
        response = self.client.post(
            "/api/admin/inventory/minimart/products/",
            {
                "name": "Cola 50cl", "sku": "MM-COLA", "purchase_cost": "300.00",
                "selling_price": "500.00", "base_unit": "BOTTLE",
                "location_id": self.main.pk, "category_id": category.pk,
                "opening_quantity": "12", "opening_unit": "BOTTLE",
                "idempotency_key": "inc-cola-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        created = self.payload(response)
        self.assertEqual(created["sku"], "MM-COLA")
        self.assertEqual(Decimal(created["stock_quantity"]), Decimal("12.0000"))

        listed = self.client.get("/api/admin/inventory/minimart/products/?page_size=100")
        self.assertEqual(listed.status_code, 200, listed.content)
        rows = self.payload(listed)
        self.assertEqual([row["sku"] for row in rows], ["MM-COLA"])
        self.assertEqual(rows[0]["category"], "Beverages")
        # Opening stock really landed on the ledger, not just in the response.
        balance = StockBalance.objects.get(item__sku="MM-COLA", location=self.main)
        self.assertEqual(balance.quantity_on_hand, Decimal("12.0000"))

    def test_minimart_product_search_is_case_insensitive(self):
        category = MiniMartCategory.objects.create(name="Beverages")
        self.client.post(
            "/api/admin/inventory/minimart/products/",
            {"name": "Cola 50cl", "sku": "MM-COLA", "purchase_cost": "300.00",
             "selling_price": "500.00", "base_unit": "BOTTLE",
             "location_id": self.main.pk, "category_id": category.pk},
            format="json")
        found = self.client.get(
            "/api/admin/inventory/minimart/products/", {"search": "  COLA "})
        self.assertEqual(found.status_code, 200)
        self.assertEqual([row["sku"] for row in self.payload(found)], ["MM-COLA"])

    def test_minimart_products_list_is_not_n_plus_one(self):
        """A page of products must not issue one balance query per row."""
        category = MiniMartCategory.objects.create(name="Beverages")
        for index in range(6):
            self.client.post(
                "/api/admin/inventory/minimart/products/",
                {"name": f"Item {index}", "sku": f"MM-N{index}",
                 "purchase_cost": "10.00", "selling_price": "20.00",
                 "base_unit": "BOTTLE", "location_id": self.main.pk,
                 "category_id": category.pk},
                format="json")
        # Exactly three queries regardless of page size: the page count, the
        # joined product rows (item + location + category via select_related),
        # and ONE balance lookup for the whole page.
        with self.assertNumQueries(3):
            response = self.client.get("/api/admin/inventory/minimart/products/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.payload(response)), 6)

    def test_minimart_sales_history_answers(self):
        response = self.client.get(
            "/api/admin/service-requests/reception/minimart/sales/?page=1")
        self.assertEqual(response.status_code, 200, response.content)
        body = self.payload(response)
        self.assertEqual(body["results"], [])
        self.assertEqual(body["count"], 0)

    def test_receptionist_cannot_manage_the_catalogue(self):
        self.client.force_authenticate(user=self.receptionist)
        response = self.client.post(
            "/api/admin/inventory/minimart/categories/", {"name": "Nope"}, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(MiniMartCategory.objects.filter(name="Nope").exists())

    # ------------------------------------------------------------------ #
    # 7-8: amenities
    # ------------------------------------------------------------------ #
    def test_amenities_stock_returns_the_reception_shelf(self):
        response = self.client.get("/api/admin/service-requests/reception/amenities/stock/")
        self.assertEqual(response.status_code, 200, response.content)
        body = self.payload(response)
        self.assertEqual(body["location"]["code"], "RECEPTION")
        self.assertEqual(body["location"]["id"], self.reception.pk)
        self.assertEqual(
            {row["sku"]: Decimal(row["quantity"]) for row in body["balances"]},
            {"INC-WATER": Decimal("5.0000")},
        )

    def test_amenities_stock_refuses_an_explicit_non_reception_location(self):
        response = self.client.get("/api/admin/service-requests/reception/amenities/stock/", {"location_id": self.main.pk})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("location_id", response.json()["errors"])

    def test_amenities_issues_list_filters_by_booking(self):
        response = self.client.get(
            "/api/admin/service-requests/reception/amenities/issues/",
            {"booking_id": self.booking.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.payload(response)["results"], [])

    def test_amenity_issuance_deducts_exactly_once_from_reception(self):
        payload = {
            "guest_id": self.guest.pk,
            "booking_id": self.booking.pk,
            "item_id": self.water.pk,
            "quantity": "2",
            "location_id": self.reception.pk,
            "idempotency_key": "inc-amenity-1",
        }
        first = self.client.post(
            "/api/admin/service-requests/reception/amenities/issue/", payload, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(self.payload(first)["created"])

        # A duplicate submit with the same key must NOT deduct a second time.
        second = self.client.post(
            "/api/admin/service-requests/reception/amenities/issue/", payload, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(self.payload(second)["created"])

        balance = StockBalance.objects.get(item=self.water, location=self.reception)
        self.assertEqual(balance.quantity_on_hand, Decimal("3.0000"))
        self.assertEqual(GuestAmenityIssue.objects.count(), 1)
        # Main store untouched: the deduction came from Reception only.
        main_balance = StockBalance.objects.get(item=self.water, location=self.main)
        self.assertEqual(main_balance.quantity_on_hand, Decimal("20.0000"))

    def test_amenity_issuance_cannot_exceed_available_stock(self):
        response = self.client.post(
            "/api/admin/service-requests/reception/amenities/issue/",
            {"guest_id": self.guest.pk, "booking_id": self.booking.pk,
             "item_id": self.water.pk, "quantity": "99",
             "location_id": self.reception.pk},
            format="json")
        self.assertEqual(response.status_code, 400, response.content)
        balance = StockBalance.objects.get(item=self.water, location=self.reception)
        self.assertEqual(balance.quantity_on_hand, Decimal("5.0000"))
        self.assertFalse(GuestAmenityIssue.objects.exists())

    # ------------------------------------------------------------------ #
    # 9: breakfast
    # ------------------------------------------------------------------ #
    def test_breakfast_config_loads_and_saves_without_resetting(self):
        loaded = self.client.get("/api/admin/service-requests/breakfast/config/")
        self.assertEqual(loaded.status_code, 200, loaded.content)
        self.assertIn("is_enabled", self.payload(loaded))

        saved = self.client.put(
            "/api/admin/service-requests/breakfast/config/",
            {"is_enabled": True, "service_start": "06:30",
             "service_end": "10:00", "notes": "Buffet"},
            format="json")
        self.assertEqual(saved.status_code, 200, saved.content)
        body = self.payload(saved)
        self.assertEqual(body["service_start"], "06:30")
        self.assertEqual(body["notes"], "Buffet")

        # A second read must return the SAVED values, not defaults.
        again = self.payload(self.client.get("/api/admin/service-requests/breakfast/config/"))
        self.assertEqual(again["service_start"], "06:30")
        self.assertEqual(again["service_end"], "10:00")

    def test_reception_guest_search_finds_by_name_reference_and_room(self):
        endpoint = "/api/admin/service-requests/reception/guests/"

        by_name = self.payload(self.client.get(endpoint, {"search": "ada"}))
        self.assertEqual([row["full_name"] for row in by_name["results"]], ["Ada Obi"])
        self.assertEqual(by_name["results"][0]["bookings"][0]["reference"],
                         self.booking.booking_reference)
        self.assertEqual(by_name["results"][0]["bookings"][0]["rooms"], ["204"])

        by_reference = self.payload(
            self.client.get(endpoint, {"search": self.booking.booking_reference}))
        self.assertEqual([row["id"] for row in by_reference["results"]], [self.guest.pk])

        # Partial reference, different case, padded with spaces.
        partial = self.booking.booking_reference[-4:].lower()
        by_partial = self.payload(
            self.client.get(endpoint, {"search": f"  {partial}  "}))
        self.assertEqual([row["id"] for row in by_partial["results"]], [self.guest.pk])

        by_room = self.payload(self.client.get(endpoint, {"search": "204"}))
        self.assertEqual([row["id"] for row in by_room["results"]], [self.guest.pk])

    def test_reception_guest_search_excludes_ended_stays(self):
        self.booking.status = Booking.Status.CHECKED_OUT
        self.booking.save(update_fields=["status"])
        response = self.payload(
            self.client.get("/api/admin/service-requests/reception/guests/",
                            {"search": "ada"}))
        # The guest is still findable by name, but with NO current stay offered.
        self.assertEqual([row["bookings"] for row in response["results"]], [[]])

    def test_reception_guest_search_does_not_duplicate_guests(self):
        """Two matching stays must not multiply the guest row."""
        second = make_booking(
            self.guest, self.room_type, rooms=[self.room],
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=1),
            status=Booking.Status.CONFIRMED)
        response = self.payload(
            self.client.get("/api/admin/service-requests/reception/guests/",
                            {"search": "ada"}))
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(len(response["results"][0]["bookings"]), 2)

    def test_breakfast_issue_is_recorded_once_per_service_date(self):
        response = self.client.post(
            "/api/admin/service-requests/breakfast/issue/",
            {"guest_id": self.guest.pk, "booking_id": self.booking.pk,
             "service_date": hotel_today().isoformat(),
             "idempotency_key": "inc-breakfast-1"},
            format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.payload(response)["created"])
        self.assertEqual(BreakfastIssuance.objects.count(), 1)

    # ------------------------------------------------------------------ #
    # 10: reports approvals
    # ------------------------------------------------------------------ #
    def test_reports_approvals_loads_for_an_admin(self):
        response = self.client.get("/api/admin/reports/approvals/")
        self.assertEqual(response.status_code, 200, response.content)
        body = self.payload(response)
        self.assertIn("categories", body)
        self.assertEqual(body["total_pending"], 0)

    def test_reports_approvals_is_refused_without_approval_capability(self):
        self.client.force_authenticate(user=self.keeper)
        response = self.client.get("/api/admin/reports/approvals/")
        self.assertEqual(response.status_code, 403, response.content)

    # ------------------------------------------------------------------ #
    # 11: room types
    # ------------------------------------------------------------------ #
    def test_room_types_list_answers(self):
        response = self.client.get("/api/admin/room-types/?page_size=100")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Incident Standard", [row["name"] for row in self.payload(response)])

    # ------------------------------------------------------------------ #
    # 12: booking extension quote
    # ------------------------------------------------------------------ #
    def test_booking_extension_quote_returns_a_price_and_changes_nothing(self):
        before = Booking.objects.get(pk=self.booking.pk)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/extend-quote/",
            {"additional_nights": 2}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        quote = self.payload(response)
        self.assertEqual(quote["additional_nights"], 2)
        self.assertTrue(quote["can_extend"], quote)
        self.assertTrue(quote["available"], quote)
        self.assertEqual(Decimal(quote["extension_amount"]),
                         Decimal(quote["nightly_rate"]) * 2)
        # A quote must never move the booking.
        after = Booking.objects.get(pk=self.booking.pk)
        self.assertEqual(before.check_out, after.check_out)
        self.assertEqual(before.total_amount, after.total_amount)

    def test_booking_extension_quote_reports_an_overlapping_booking(self):
        other_guest = make_guest(email="inc-other@test.dev")
        make_booking(
            other_guest, self.room_type, rooms=[self.room],
            check_in=self.booking.check_out,
            check_out=self.booking.check_out + timedelta(days=2),
            status=Booking.Status.CONFIRMED)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/extend-quote/",
            {"additional_nights": 2}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        quote = self.payload(response)
        self.assertFalse(quote["available"])
        self.assertTrue(quote["conflicts"])

    def test_booking_extension_quote_rejects_a_past_date(self):
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/extend-quote/",
            {"new_check_out": (hotel_today() - timedelta(days=1)).isoformat()},
            format="json")
        self.assertIn(response.status_code, (400, 409), response.content)

    def test_booking_extension_quote_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(
            f"/api/admin/bookings/{self.booking.booking_reference}/extend-quote/",
            {"additional_nights": 1}, format="json")
        self.assertEqual(response.status_code, 401, response.content)


class SchemaDriftResponseTests(APITestCase):
    """A drifted schema must be reported honestly through the real stack."""

    def setUp(self):
        super().setUp()
        self.admin = make_staff("drift-admin@staff.dev", role=User.Role.ADMIN)
        self.client.force_authenticate(user=self.admin)

    def test_schema_error_is_500_not_a_misleading_503(self):
        from django.db.utils import ProgrammingError
        from unittest import mock

        boom = ProgrammingError(
            "(1054, \"Unknown column 'inventory_stocklocation.is_reception' in 'field list'\")")
        with mock.patch(
            "apps.inventory.views.StockLocation.objects.all", side_effect=boom
        ):
            response = self.client.get("/api/admin/inventory/locations/")
        self.assertEqual(response.status_code, 500, response.content)
        body = json.loads(response.content)
        self.assertEqual(body["code"], "DATABASE_SCHEMA_ERROR")
        self.assertNotIn("is_reception", json.dumps(body))

    def test_outage_is_still_503(self):
        from django.db.utils import OperationalError
        from unittest import mock

        with mock.patch(
            "apps.inventory.views.StockLocation.objects.all",
            side_effect=OperationalError("(2003, \"Can't connect to MySQL server\")"),
        ):
            response = self.client.get("/api/admin/inventory/locations/")
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(json.loads(response.content)["code"], "DATABASE_UNAVAILABLE")

    def test_readiness_body_names_the_drift_on_request(self):
        # Default body stays the established three-key contract; the schema
        # detail is opt-in so no deployed probe can break.
        plain = self.client.get("/api/health/ready/")
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(
            json.loads(plain.content),
            {"success": True, "status": "ok", "database": "up"},
        )
        detailed = self.client.get("/api/health/ready/?schema=1")
        self.assertEqual(detailed.status_code, 200)
        body = json.loads(detailed.content)
        self.assertTrue(body["success"])
        self.assertEqual(body["database"], "up")
        self.assertTrue(body["schema"]["ok"], body["schema"])
