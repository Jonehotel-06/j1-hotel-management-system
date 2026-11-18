"""Acceptance: warehouse routing, actual occupancy and exact unit handling."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.bookings.models import Booking, RoomOccupant
from apps.bookings.services.booking_service import assign_room, check_in_booking_room, check_out_booking
from apps.core.utils import hotel_today, parse_whole_count
from apps.finance.models import FinancialTransaction
from apps.guest_services.models import BreakfastIssuance, GuestAmenityIssue, MiniMartProduct
from apps.guest_services.services.laundry_service import create_laundry_order
from apps.guest_services.services_amenities import allocate_reception_amenities, issue_guest_amenity
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement, transfer_stock
from apps.pos.models import PosOrder
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room, make_room_type, make_staff

INV = "/api/admin/inventory/"
BASE = "/api/admin/service-requests/"
AMENITIES = BASE + "reception/amenities/"
ROSTER = BASE + "reception/guests/"


def opening(item, location, quantity):
    return record_stock_movement(item=item, location=location, movement_type="OPENING", quantity_delta=quantity,
                                 source_key=f"nov18-opening:{item.pk}:{location.pk}")[0]


def balance(item, location):
    return StockBalance.objects.get(item=item, location=location).quantity_on_hand


class StoreOperationsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.keeper = make_staff("nov18-keeper@example.test", User.Role.STOREKEEPER)
        self.desk = make_staff("nov18-desk@example.test", User.Role.RECEPTIONIST)
        self.admin = make_staff("nov18-stockadmin@example.test", User.Role.ADMIN)
        self.guest = make_guest()
        self.main = StockLocation.objects.create(code="CENTRAL", name="Central", operational_role="MAIN")
        self.reception = StockLocation.objects.create(code="GUEST-SHELF", name="Guest shelf", is_reception=True)
        self.kitchen = StockLocation.objects.create(code="PREP", name="Preparation", operational_role="KITCHEN")
        self.other = StockLocation.objects.create(code="SIDE", name="Side store", operational_role="GENERAL")
        self.soap = StockItem.objects.create(sku="COUNT-SOAP", name="Guest Soap", base_unit="PC", category=" Guest Care ", units_per_pack="24")
        self.grain = StockItem.objects.create(sku="WEIGH-GRAIN", name="Grain", base_unit="KG", category="guest care")
        self.retail = StockItem.objects.create(sku="RETAIL-1", name="Retail", item_type="SELLABLE_PRODUCT", base_unit="BOTTLE")
        MiniMartProduct.objects.create(item=self.retail, location=self.reception, selling_price=Decimal("150"))
        for item, location, qty in ((self.soap, self.main, "20"), (self.soap, self.reception, "6"),
                                    (self.soap, self.other, "4"), (self.grain, self.main, "5.2500"),
                                    (self.retail, self.main, "10"), (self.retail, self.reception, "10")):
            opening(item, location, qty)

    def post(self, url, body, expected):
        response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, expected, response.content)
        return response.json().get("data")

    def get(self, url, expected=200, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, expected, response.content)
        return response.json().get("data")

    def test_only_traced_main_store_issuance_is_eligible(self):
        self.auth(self.keeper)
        transfer_stock(item=self.soap, from_location=self.other, to_location=self.reception,
                       quantity="2", actor=self.keeper, transfer_key="not-an-allocation")
        self.assertEqual(self.get(AMENITIES + "stock/")["balances"], [])
        src = self.get(AMENITIES + "source-stock/", search="soap")
        self.assertEqual([r["id"] for r in src["locations"]], [self.main.pk])
        self.assertEqual([r["id"] for r in src["items"]], [self.soap.pk])
        req = {"item_id": self.soap.pk, "quantity": "3", "from_location_id": self.main.pk, "idempotency_key": "main-allocation"}
        self.post(AMENITIES + "allocate/", dict(req, from_location_id=self.other.pk), 400)
        self.post(AMENITIES + "allocate/", dict(req, location_id=self.other.pk), 400)
        self.post(AMENITIES + "allocate/", dict(req, item_id=self.retail.pk), 400)
        self.assertTrue(self.post(AMENITIES + "allocate/", req, 200)["created"])
        self.assertFalse(self.post(AMENITIES + "allocate/", req, 200)["created"])
        self.get(AMENITIES + "stock/", 400, location_id=self.main.pk)
        issue = {"guest_id": self.guest.pk, "item_id": self.soap.pk, "quantity": "2", "idempotency_key": "desk-handover"}
        self.post(AMENITIES + "issue/", issue, 403)
        self.auth(self.desk)
        self.post(AMENITIES + "allocate/", req, 403)
        self.get(AMENITIES + "source-stock/", 403)
        [shelf] = self.get(AMENITIES + "stock/")["balances"]
        self.assertEqual(shelf["quantity"], "3.0000")
        self.assertEqual(shelf["allocations"][0]["source_store"], "CENTRAL")
        self.post(AMENITIES + "issue/", dict(issue, item_id=self.retail.pk), 400)
        self.post(AMENITIES + "issue/", dict(issue, quantity="4"), 400)
        self.post(AMENITIES + "issue/", dict(issue, quantity="1.5"), 400)
        created = self.post(AMENITIES + "issue/", issue, 200)
        self.assertTrue(created["created"])
        self.assertEqual(Decimal(created["issue"]["departmental_balance_after"]), 9)
        self.assertEqual(Decimal(created["issue"]["eligible_balance_after"]), 1)
        [source] = created["issue"]["main_store_issuances"]
        self.assertEqual(source["issued_by"], self.keeper.full_name)
        self.assertEqual(source["issued_movement"], shelf["allocations"][0]["issued_movement"])
        self.assertEqual(Decimal(source["quantity"]), 2)
        self.assertFalse(self.post(AMENITIES + "issue/", issue, 200)["created"])
        self.assertEqual(self.get(AMENITIES + "stock/")["balances"][0]["allocations"][0]["remaining_quantity"], "1.0000")
        self.assertEqual(balance(self.soap, self.reception), 9)
        self.assertEqual(balance(self.soap, self.main), 17)
        self.assertEqual(balance(self.retail, self.reception), 10)
        self.assertEqual(GuestAmenityIssue.objects.count(), 1)

    def test_other_movements_do_not_create_or_replenish_eligible_lots(self):
        allocate_reception_amenities(item=self.soap, from_location=self.main, quantity="2", actor=self.keeper)
        record_stock_movement(item=self.soap, location=self.reception, movement_type="ISSUE",
                              quantity_delta="-2", source_key="other-shelf-use")
        record_stock_movement(item=self.soap, location=self.reception, movement_type="ADJUSTMENT",
                              quantity_delta="3", source_key="unproven-stock-adjustment")
        self.auth(self.desk)
        self.assertGreater(balance(self.soap, self.reception), 0)
        self.assertEqual(self.get(AMENITIES + "stock/")["balances"], [])

    def test_check_in_change_preserves_legacy_single_room_idempotency(self):
        rt = make_room_type()
        room = make_room(rt, "808")
        booking = make_booking(self.guest, rt, rooms=[room])
        allocate_reception_amenities(item=self.soap, from_location=self.main, quantity="2", actor=self.keeper)
        self.auth(self.desk)
        data = {"guest_id": self.guest.pk, "booking_id": booking.pk, "room_number": "808", "item_id": self.soap.pk, "quantity": "1"}
        old = f"checkin:{booking.booking_reference}:{self.soap.pk}:{hotel_today()}"
        new = f"checkin-room:{booking.booking_reference}:808:{self.soap.pk}:{hotel_today()}"
        first = self.post(AMENITIES + "issue/", dict(data, idempotency_key=old), 200)
        retry = self.post(AMENITIES + "issue/", dict(data, idempotency_key=new), 200)
        self.assertFalse(retry["created"])
        self.assertEqual(first["issue"]["reference"], retry["issue"]["reference"])
        self.assertEqual(balance(self.soap, self.reception), 7)

    def test_kitchen_transfer_and_direct_issue_are_separate_and_idempotent(self):
        self.auth(self.keeper)
        issue = {"item_id": self.soap.pk, "source_location_id": self.main.pk, "quantity": "20",
                 "recipient": "Test Cook", "idempotency_key": "kitchen-retry"}
        moved = dict(issue, destination_location_id=self.kitchen.pk, reason="KITCHEN")
        self.post(INV + "manual-stock-out/", dict(moved, reason="GENERAL"), 400)
        self.post(INV + "manual-stock-out/", dict(issue, reason="GENERAL", destination="Main Kitchen"), 400)
        self.post(INV + "kitchen-issues/", dict(moved, destination_location_id=self.other.pk), 400)
        self.post(INV + "transfers/", {"item_id": self.soap.pk, "from_location_id": self.main.pk,
            "to_location_id": self.kitchen.pk, "quantity": "1", "idempotency_key": "alternate"}, 400)
        self.post(INV + "issues/", {"item_id": self.soap.pk, "location_id": self.main.pk,
            "quantity": "1", "idempotency_key": "generic", "department": "KITCHEN"}, 400)
        self.post(INV + "returns/", {"item_id": self.soap.pk, "location_id": self.main.pk,
            "quantity": "1", "idempotency_key": "alternate-return", "department": "KITCHEN", "direction": "OUT"}, 400)
        self.assertEqual([row["id"] for row in self.get(INV+"locations/", kitchen="true")], [self.kitchen.pk])
        self.assertNotIn(self.kitchen.pk, {row["id"] for row in self.get(INV+"locations/", kitchen="false")})
        first = self.post(INV + "kitchen-issues/", moved, 201)
        retry = self.post(INV + "kitchen-issues/", moved, 200)
        self.assertFalse(retry["created"])
        ref = first["transaction"]["reference"]
        self.assertTrue(ref.startswith("KIS-"))
        self.assertEqual(retry["transaction"]["reference"], ref)
        self.post(INV + "kitchen-issues/", dict(moved, quantity="19"), 400)
        self.assertEqual(balance(self.soap, self.main), 0)
        self.assertEqual(balance(self.soap, self.kitchen), 20)
        self.assertEqual(set(StockMovement.objects.filter(source_reference=ref).values_list("type", flat=True)),
                         {"TRANSFER_OUT", "TRANSFER_IN"})
        direct = self.post(INV + "kitchen-issues/", dict(issue, source_location_id=self.kitchen.pk, quantity="2",
                           idempotency_key="direct-kitchen", reason="KITCHEN"), 201)
        self.assertEqual(direct["transaction"]["type"], "ISSUE")
        self.post(INV + "manual-stock-out/", dict(issue, source_location_id=self.other.pk, quantity="1",
                       idempotency_key="general-issue", reason="GENERAL"), 201)
        self.assertEqual(balance(self.soap, self.kitchen), 18)
        self.assertEqual(len(self.get(INV + "kitchen-issues/")), 2)
        self.assertEqual(len(self.get(INV + "manual-stock-out/")), 1)
        self.assertEqual(len(self.get(INV + "movements/", workflow="KITCHEN_ISSUE")), 3)
        self.assertEqual(len(self.get(INV + "movements/", workflow="MANUAL_STOCK_OUT")), 1)
        self.assertEqual(FinancialTransaction.objects.count(), 0)
        self.assertEqual(PosOrder.objects.count(), 0)

    def test_counts_validate_converted_base_and_leave_measurements_and_history_intact(self):
        self.auth(self.admin)
        new_store = StockLocation.objects.create(code="NEW-COUNT", name="New Counted Store")
        payload = {"item_id": self.soap.pk, "location_id": new_store.pk, "idempotency_key": "count-opening"}
        self.post(INV + "opening-stock/", dict(payload, quantity="1.25"), 400)
        self.post(INV + "opening-stock/", dict(payload, quantity="0.1", unit="PACK", conversion_factor="24"), 400)
        self.post(INV + "opening-stock/", dict(payload, quantity="0.50001", unit="PACK", conversion_factor="24"), 400)
        self.post(INV + "opening-stock/", dict(payload, quantity="0.5", unit="PACK", conversion_factor="24"), 201)
        self.assertEqual(balance(self.soap, new_store), 12)
        bal = StockBalance.objects.get(item=self.soap, location=new_store)
        self.post(INV + f"balances/{bal.pk}/adjust/", {"quantity": "15.5", "reason": "Counted stock"}, 400)
        self.assertEqual(balance(self.soap, new_store), 12)
        self.post(INV + "issues/", {"item_id": self.grain.pk, "location_id": self.main.pk, "quantity": "1.125",
                                  "idempotency_key": "weighed-issue"}, 201)
        self.assertEqual(balance(self.grain, self.main), Decimal("4.125"))
        opening_before = StockMovement.objects.get(source_key=f"nov18-opening:{self.grain.pk}:{self.main.pk}")
        self.assertEqual(opening_before.quantity_delta, Decimal("5.25"))

    def test_category_location_search_and_paginated_mixed_units(self):
        self.auth(self.keeper)
        req = {"include_categories": "true", "category": "GUEST care", "location_id": self.main.pk,
               "page_size": 1, "search": "central"}
        page1 = self.get(INV + "balances/", **req)
        page2 = self.get(INV + "balances/", **req, page=2)
        self.assertNotEqual(page1["results"][0]["id"], page2["results"][0]["id"])
        self.assertEqual(page1["categories"], page2["categories"])
        f = next(c for c in page1["categories"] if c["key"] == "guest care")
        self.assertEqual({x["unit"]: Decimal(x["quantity"]) for x in f["units"]}, {"PC": Decimal(20), "KG": Decimal("5.25")})
        self.assertIsInstance(self.get(INV + "balances/"), list)
        uncategorized = self.get(INV + "balances/", include_categories="true", uncategorized="true", location_id=self.main.pk)
        self.assertEqual([row["item_sku"] for row in uncategorized["results"]], [self.retail.sku])

    def test_commercial_quantities_do_not_truncate_fraction_or_boolean(self):
        for value in (True, False, "NaN", "Infinity", "3.5", 1.5, 0, -1):
            with self.assertRaises(ValueError):
                parse_whole_count(value)
        self.assertEqual(parse_whole_count("10.0000"), 10)
        with self.assertRaises(ValidationError):
            create_laundry_order(actor=self.admin, customer_name="Test Walk In", items=[{
                "description": "Coat", "quantity": 2.5, "unit_price": "500",
            }])


class BreakfastRosterTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff("nov18-breakfast@example.test", User.Role.RECEPTIONIST)
        self.auth(self.staff)
        self.today = hotel_today()
        self.rt = make_room_type()
        self.rooms = [make_room(self.rt, n) for n in ("401", "402", "403")]
        self.guest = make_guest()
        self.booking = make_booking(self.guest, self.rt, rooms=self.rooms[:2], number_of_rooms=2,
                                    check_in=self.today, check_out=self.today+timedelta(days=2), amount_paid="15000")
        self.assignments = list(self.booking.room_assignments.order_by("room__room_number"))
        RoomOccupant.objects.create(booking_room=self.assignments[0], full_name="Present Person", email="present@example.test")
        RoomOccupant.objects.create(booking_room=self.assignments[1], full_name="Waiting Person", email="waiting@example.test")
        self.future = make_booking(make_guest("future@example.test"), self.rt, rooms=[self.rooms[2]])

    def roster(self, **params):
        response = self.client.get(ROSTER, {"scope": "breakfast", **params})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["results"]

    def issue(self, expected, key="nov18-breakfast"):
        res = self.client.post(BASE + "breakfast/issue/", {"booking_id": self.booking.pk,
            "guest_id": self.guest.pk, "service_date": str(self.today), "idempotency_key": key}, format="json")
        self.assertEqual(res.status_code, expected, res.content)
        return res.json().get("data")

    def test_real_partial_arrival_room_reassignment_checkout_and_issuance_snapshot(self):
        self.assertFalse(self.roster())
        self.issue(400)
        check_in_booking_room(self.booking, assignment_id=self.assignments[0].pk, staff_user=self.staff)
        [row] = self.roster()
        self.assertEqual(row["id"], self.guest.pk)
        self.assertEqual(row["bookings"][0]["rooms"], ["401"])
        self.assertEqual(self.roster(search="Present Person")[0]["id"], self.guest.pk)
        self.assertFalse(self.roster(search="Waiting Person"))
        self.assertFalse(self.roster(search="402"))
        self.assertFalse(self.roster(service_date=str(self.today+timedelta(days=1))))
        self.assertFalse(self.roster(service_date=str(self.today-timedelta(days=1))))
        self.assertTrue(self.issue(200)["created"])
        self.assertFalse(self.issue(200)["created"])
        check_in_booking_room(self.booking, assignment_id=self.assignments[1].pk, staff_user=self.staff)
        self.assertEqual(self.roster()[0]["bookings"][0]["rooms"], ["401", "402"])
        self.assertTrue(self.roster(search="Waiting Person"))
        self.future.delete()
        assign_room(self.assignments[0], new_room=self.rooms[2], staff_user=self.staff)
        self.assertEqual(self.roster()[0]["bookings"][0]["rooms"], ["402", "403"])
        self.issue(400, key="distinct-key-same-day")
        check_out_booking(self.booking, staff_user=self.staff, allow_balance_due=True)
        self.assertFalse(self.roster())
        self.assertEqual(BreakfastIssuance.objects.get().room_numbers, ["401"])
        self.assertEqual(PosOrder.objects.count(), 0)

    def test_legacy_fallback_requires_room_evidence_and_permissions(self):
        self.booking.status = Booking.Status.CHECKED_IN
        self.booking.save(update_fields=["status"])
        self.assertFalse(self.roster())
        self.assignments[0].checked_in_at = timezone.now()
        self.assignments[0].save(update_fields=["checked_in_at"])
        self.assertEqual(self.roster()[0]["bookings"][0]["rooms"], ["401"])
        self.auth(make_staff("no-roster@example.test", User.Role.HOUSEKEEPER))
        self.assertEqual(self.client.get(ROSTER, {"scope": "breakfast"}).status_code, 403)


class AmenityLastUnitTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_last_eligible_unit_cannot_be_issued_twice(self):
        keeper = make_staff("race-keeper@example.test", User.Role.STOREKEEPER)
        desk = make_staff("race-desk@example.test", User.Role.RECEPTIONIST)
        guest = make_guest()
        main = StockLocation.objects.create(code="MAIN", name="Main Store")
        shelf = StockLocation.objects.create(code="RECEPTION", name="Reception", is_reception=True)
        item = StockItem.objects.create(sku="LAST-SOAP", name="Last bar of soap", base_unit="BAR")
        opening(item, main, "1")
        opening(item, shelf, "5")  # Plenty of physical stock, only one eligible unit.
        allocate_reception_amenities(item=item, from_location=main, quantity="1", actor=keeper)
        gate = Barrier(2)

        def issue(index):
            close_old_connections()
            try:
                gate.wait(timeout=8)
                _, created = issue_guest_amenity(guest=guest, booking=None, room_number="", item=item,
                                                quantity="1", actor=desk, idempotency_key=f"last-unit-{index}")
                return "issued" if created else "unexpected replay"
            except ValueError as exc:
                return str(exc)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(issue, range(2)))
        self.assertEqual(results.count("issued"), 1, results)
        self.assertTrue(any("remain from Main Store" in result for result in results), results)
        self.assertEqual(balance(item, shelf), 5)
        self.assertEqual(GuestAmenityIssue.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(type="ISSUE", item=item, location=shelf).count(), 1)
