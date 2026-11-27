# tests/test_critical_repair_reception_amenities.py
"""Reception Store -> Guest Amenities repair (2026-11-26).

The reported fault: the Store Keeper issued toiletries from Main Store to the
Reception Store (an ordinary inventory transfer), the Reception balance went up
in Inventory, but the Amenities section showed nothing to hand to guests.

These tests prove the required workflow end to end:

    Main Store 100 -> issue 30 to Reception -> Main 70 / Reception 30
    -> the Amenities list shows 30 available -> the desk issues 4
    -> Reception 26, Main stays 70, the issue is traceable to the stay.
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement, transfer_stock
from apps.guest_services.models import GuestAmenityIssue
from apps.guest_services.services_amenities import issue_guest_amenity

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


def _on_hand(item, location):
    balance = StockBalance.objects.filter(item=item, location=location).first()
    return Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")


class ReceptionAmenityRepairTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from apps.core.utils import hotel_today

        self.today = hotel_today()
        self.keeper = make_staff("repair-amenity-keeper@staff.dev", role=User.Role.INVENTORY_CLERK)
        self.receptionist = make_staff("repair-amenity-desk@staff.dev", role=User.Role.RECEPTIONIST)

        self.main_store = StockLocation.objects.create(code="REP-MAIN", name="Main Store REP")
        self.reception_store = StockLocation.objects.create(
            code="REP-RECEPTION", name="Reception Store", is_reception=True,
        )
        self.soap = StockItem.objects.create(
            sku="REP-SOAP", name="Repair soap", base_unit="BAR",
            item_type=StockItem.ItemType.CONSUMABLE, standard_cost=Decimal("50.00"),
        )
        self.tissue = StockItem.objects.create(
            sku="REP-TISSUE", name="Repair tissue", base_unit="PACK",
            item_type=StockItem.ItemType.CONSUMABLE, standard_cost=Decimal("20.00"),
        )
        record_stock_movement(
            item=self.soap, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="rep-amenity-opening-soap", actor=self.keeper,
        )
        record_stock_movement(
            item=self.tissue, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="rep-amenity-opening-tissue", actor=self.keeper,
        )
        # The Store Keeper's NORMAL route: the inventory transfer screen.
        transfer_stock(
            item=self.soap, from_location=self.main_store, to_location=self.reception_store,
            quantity=Decimal("30"), actor=self.keeper, transfer_key="rep-amenity-issue-soap",
            notes="Issue to reception",
        )
        transfer_stock(
            item=self.tissue, from_location=self.main_store, to_location=self.reception_store,
            quantity=Decimal("30"), actor=self.keeper, transfer_key="rep-amenity-issue-tissue",
            notes="Issue to reception",
        )

        self.guest = make_guest("repair-amenity-guest@example.com")
        room_type = make_room_type("Repair amenity suite", price="15000.00")
        self.booking = make_booking(
            self.guest, room_type, check_in=self.today, check_out=self.today + timedelta(days=2),
            total="30000.00",
        )

    def test_A_transfer_moves_both_balances_and_touches_no_other_store(self):
        self.assertEqual(_on_hand(self.soap, self.main_store), Decimal("70.0000"))
        self.assertEqual(_on_hand(self.soap, self.reception_store), Decimal("30.0000"))
        self.assertFalse(
            StockMovement.objects.filter(
                item=self.soap, type__in=[StockMovement.Type.ISSUE, StockMovement.Type.POS_CONSUMPTION],
            ).exists()
        )

    def test_B_amenities_list_shows_the_transferred_reception_stock(self):
        """The exact reported failure: Reception stock must appear as available."""
        self.auth(self.receptionist)
        response = self.client.get("/api/admin/service-requests/reception/amenities/stock/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIsNotNone(data["location"])
        self.assertEqual(data["location"]["code"], "REP-RECEPTION")
        rows = {row["sku"]: row for row in data["balances"]}
        self.assertIn("REP-SOAP", rows)
        self.assertEqual(Decimal(rows["REP-SOAP"]["quantity"]), Decimal("30.0000"))
        self.assertIn("REP-TISSUE", rows)
        self.assertEqual(Decimal(rows["REP-TISSUE"]["quantity"]), Decimal("30.0000"))
        # Each lot names where the goods actually came from.
        allocation = rows["REP-SOAP"]["allocations"][0]
        self.assertEqual(allocation["source_store"], "REP-MAIN")
        self.assertEqual(Decimal(allocation["allocated_quantity"]), Decimal("30.0000"))

    def test_C_amenities_list_never_reports_the_main_store_balance(self):
        self.auth(self.receptionist)
        rows = {
            row["sku"]: row
            for row in self.client.get("/api/admin/service-requests/reception/amenities/stock/").json()["data"]["balances"]
        }
        # Main Store holds 70, the Reception shelf holds 30: the desk must see 30.
        self.assertEqual(Decimal(rows["REP-SOAP"]["quantity"]), Decimal("30.0000"))
        self.assertNotEqual(Decimal(rows["REP-SOAP"]["quantity"]), _on_hand(self.soap, self.main_store))

    def test_D_issuing_deducts_only_reception_and_records_the_stay(self):
        record, created = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="",
            item=self.soap, quantity=Decimal("4"), actor=self.receptionist,
            notes="Check-in amenities", idempotency_key="rep-checkin-soap",
        )
        self.assertTrue(created)
        self.assertEqual(_on_hand(self.soap, self.reception_store), Decimal("26.0000"))
        self.assertEqual(_on_hand(self.soap, self.main_store), Decimal("70.0000"))
        self.assertEqual(record.booking_id, self.booking.pk)
        self.assertEqual(record.guest_id, self.guest.pk)
        self.assertEqual(record.location_id, self.reception_store.pk)
        self.assertEqual(record.movement.type, StockMovement.Type.ISSUE)

    def test_E_repeated_check_in_request_issues_once(self):
        first, created = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="",
            item=self.soap, quantity=Decimal("4"), actor=self.receptionist,
            idempotency_key="rep-checkin-soap-dupkey",
        )
        self.assertTrue(created)
        second, again = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="",
            item=self.soap, quantity=Decimal("4"), actor=self.receptionist,
            idempotency_key="rep-checkin-soap-dupkey",
        )
        self.assertFalse(again)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(_on_hand(self.soap, self.reception_store), Decimal("26.0000"))
        self.assertEqual(GuestAmenityIssue.objects.filter(guest=self.guest, item=self.soap).count(), 1)

    def test_F_insufficient_reception_stock_is_refused_and_main_untouched(self):
        with self.assertRaises(ValueError):
            issue_guest_amenity(
                guest=self.guest, booking=self.booking, room_number="",
                item=self.soap, quantity=Decimal("31"), actor=self.receptionist,
                idempotency_key="rep-checkin-too-many",
            )
        self.assertEqual(_on_hand(self.soap, self.reception_store), Decimal("30.0000"))
        self.assertEqual(_on_hand(self.soap, self.main_store), Decimal("70.0000"))
        self.assertFalse(GuestAmenityIssue.objects.exists())

    def test_G_extra_reception_stock_becomes_issuable_immediately(self):
        transfer_stock(
            item=self.soap, from_location=self.main_store, to_location=self.reception_store,
            quantity=Decimal("10"), actor=self.keeper, transfer_key="rep-amenity-issue-soap-2",
            notes="Top-up",
        )
        self.auth(self.receptionist)
        rows = {
            row["sku"]: row
            for row in self.client.get("/api/admin/service-requests/reception/amenities/stock/").json()["data"]["balances"]
        }
        self.assertEqual(Decimal(rows["REP-SOAP"]["quantity"]), Decimal("40.0000"))
        self.assertEqual(_on_hand(self.soap, self.main_store), Decimal("60.0000"))
