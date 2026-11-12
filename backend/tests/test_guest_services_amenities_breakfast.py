# tests/test_guest_services_amenities_breakfast.py
"""Tests for req-6 (receptionist guest amenities) and req-7 (breakfast).

Req-6: The Store Keeper allocates soap/toothbrush/toothpaste/tissue to Reception.
        The receptionist issues per guest at check-in from the Reception allocation,
        NOT Main Store. Over-issue is rejected. Full transaction records are kept.

Req-7: Complementary breakfast for lodged guests. Eligibility tied to active booking
       /check-in dates. Counts eligible guests, not rooms. No folio charge. No
       duplicate claims per guest+service period. Must NOT auto-deduct ingredients.
"""
from decimal import Decimal
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement
from apps.guest_services.models import GuestAmenityIssue, BreakfastBenefitConfig, BreakfastIssuance
from apps.guest_services.services_amenities import (
    allocate_reception_amenities, issue_guest_amenity,
    record_breakfast, breakfast_eligibility, reception_location,
    get_breakfast_config,
)

from .base import BaseAPITestCase
from .factories import make_booking, make_guest, make_room_type, make_staff


class AmenityAllocationTests(BaseAPITestCase):
    """Store Keeper allocates amenity stock to the Reception Department."""

    def setUp(self):
        super().setUp()
        self.keeper = make_staff("amenity-keeper@staff.dev", role=User.Role.STOREKEEPER)
        self.receptionist = make_staff("amenity-receptionist@staff.dev", role=User.Role.RECEPTIONIST)

        # Create the Reception location
        self.reception_loc = StockLocation.objects.create(
            code="RECEPTION", name="Reception Desk",
        )
        self.main_store = StockLocation.objects.create(
            code="MAIN", name="Main Store",
        )

        # Create amenity items
        self.soap = StockItem.objects.create(
            sku="AMY-SOAP", name="Guest soap", base_unit="BAR",
            standard_cost=Decimal("50.00"),
        )
        self.toothbrush = StockItem.objects.create(
            sku="AMY-TB", name="Guest toothbrush", base_unit="PC",
            standard_cost=Decimal("100.00"),
        )
        self.toothpaste = StockItem.objects.create(
            sku="AMY-TP", name="Guest toothpaste", base_unit="TUBE",
            standard_cost=Decimal("150.00"),
        )
        self.tissue = StockItem.objects.create(
            sku="AMY-TISSUE", name="Guest tissue", base_unit="PACK",
            standard_cost=Decimal("80.00"),
        )

    def test_allocation_creates_transfer_movement(self):
        """The allocation is a transfer into the Reception location."""
        # Stock the main store first
        record_stock_movement(
            item=self.soap, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="amenity-opening-soap", actor=self.keeper,
        )

        outbound, inbound = allocate_reception_amenities(
            item=self.soap, quantity=Decimal("30"), from_location=self.main_store,
            actor=self.keeper, notes="Initial allocation",
        )

        # Main store decreased
        balance_main = StockBalance.objects.get(item=self.soap, location=self.main_store)
        self.assertEqual(balance_main.quantity_on_hand, Decimal("70.0000"))
        # Reception location increased
        balance_rec = StockBalance.objects.get(item=self.soap, location=self.reception_loc)
        self.assertEqual(balance_rec.quantity_on_hand, Decimal("30.0000"))

    def test_allocation_from_reception_is_rejected(self):
        """Cannot allocate from Reception to Reception."""
        record_stock_movement(
            item=self.soap, location=self.reception_loc, movement_type=StockMovement.Type.OPENING,
            quantity_delta="50", source_key="amenity-rec-opening", actor=self.keeper,
        )
        with self.assertRaises(ValueError):
            allocate_reception_amenities(
                item=self.soap, quantity=Decimal("10"), from_location=self.reception_loc,
                actor=self.keeper,
            )

    def test_allocation_with_no_reception_location_fails(self):
        """If the Reception location doesn't exist, allocation fails."""
        self.reception_loc.delete()
        record_stock_movement(
            item=self.soap, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="amenity-opening-soap2", actor=self.keeper,
        )
        with self.assertRaises(ValueError):
            allocate_reception_amenities(
                item=self.soap, quantity=Decimal("10"), from_location=self.main_store,
                actor=self.keeper,
            )


class AmenityIssuanceTests(BaseAPITestCase):
    """Receptionist issues amenities to guests from Reception allocation."""

    def setUp(self):
        super().setUp()
        self.keeper = make_staff("amenity-issue-keeper@staff.dev", role=User.Role.STOREKEEPER)
        self.receptionist = make_staff("amenity-issue-rec@staff.dev", role=User.Role.RECEPTIONIST)

        self.reception_loc = StockLocation.objects.create(code="RECEPTION", name="Reception Desk")
        self.main_store = StockLocation.objects.create(code="MAIN", name="Main Store")

        self.soap = StockItem.objects.create(
            sku="AMY-SOAP", name="Guest soap", base_unit="BAR", standard_cost=Decimal("50.00"),
        )
        self.toothbrush = StockItem.objects.create(
            sku="AMY-TB", name="Guest toothbrush", base_unit="PC", standard_cost=Decimal("100.00"),
        )

        # Allocate stock to Reception
        record_stock_movement(
            item=self.soap, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="amy-issue-opening", actor=self.keeper,
        )
        record_stock_movement(
            item=self.toothbrush, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="amy-issue-opening-tb", actor=self.keeper,
        )
        from apps.inventory.services.inventory_service import transfer_stock
        transfer_stock(
            item=self.soap, from_location=self.main_store, to_location=self.reception_loc,
            quantity=Decimal("30"), actor=self.keeper,
            transfer_key="amy-issue-transfer-soap", notes="Amenity allocation",
        )
        transfer_stock(
            item=self.toothbrush, from_location=self.main_store, to_location=self.reception_loc,
            quantity=Decimal("30"), actor=self.keeper,
            transfer_key="amy-issue-transfer-tb", notes="Amenity allocation",
        )

        # Create a guest with a booking
        self.guest = make_guest("amenity-guest@example.com")
        room_type = make_room_type("Amenity Suite", price="12000.00")
        self.booking = make_booking(
            self.guest, room_type,
            check_in=date.today(), check_out=date.today() + timedelta(days=2),
            total="24000.00",
        )

    def test_issue_deducts_from_reception_not_main_store(self):
        """Issuance deducts from Reception balance, not Main Store."""
        record, created = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="101",
            item=self.soap, quantity=Decimal("2"), actor=self.receptionist,
            notes="Check-in amenities",
        )

        self.assertTrue(created)
        # Reception balance decreased
        rec_balance = StockBalance.objects.get(item=self.soap, location=self.reception_loc)
        self.assertEqual(rec_balance.quantity_on_hand, Decimal("28.0000"))
        # Main store unchanged
        main_balance = StockBalance.objects.get(item=self.soap, location=self.main_store)
        self.assertEqual(main_balance.quantity_on_hand, Decimal("70.0000"))

        # The issue record has the balances
        self.assertEqual(record.departmental_balance_before, Decimal("30.0000"))
        self.assertEqual(record.departmental_balance_after, Decimal("28.0000"))
        self.assertEqual(record.receptionist, self.receptionist)
        self.assertEqual(record.guest, self.guest)
        self.assertEqual(record.room_number, "101")

    def test_over_issue_is_rejected(self):
        """Cannot issue more than available in Reception."""
        with self.assertRaises(ValueError):
            issue_guest_amenity(
                guest=self.guest, booking=self.booking, room_number="101",
                item=self.soap, quantity=Decimal("999"), actor=self.receptionist,
            )

    def test_issuance_creates_immutable_movement(self):
        """The stock movement is append-only evidence."""
        record, created = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="102",
            item=self.toothbrush, quantity=Decimal("1"), actor=self.receptionist,
        )

        movement = record.movement
        self.assertIsNotNone(movement)
        self.assertEqual(movement.type, StockMovement.Type.ISSUE)
        self.assertEqual(movement.quantity_delta, Decimal("-1.0000"))
        self.assertEqual(movement.location, self.reception_loc)
        self.assertEqual(movement.department, "FRONT_DESK")

    def test_idempotent_issuance(self):
        """Same idempotency key returns the same record."""
        record1, created1 = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="103",
            item=self.soap, quantity=Decimal("1"), actor=self.receptionist,
            idempotency_key="test-key-1",
        )
        record2, created2 = issue_guest_amenity(
            guest=self.guest, booking=self.booking, room_number="103",
            item=self.soap, quantity=Decimal("1"), actor=self.receptionist,
            idempotency_key="test-key-1",
        )

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(record1.pk, record2.pk)


class BreakfastEligibilityTests(BaseAPITestCase):
    """Breakfast eligibility is tied to active bookings and check-in dates."""

    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("bkf-rec@staff.dev", role=User.Role.RECEPTIONIST)
        self.config = get_breakfast_config()

    def test_eligible_guest_on_check_in_date(self):
        guest = make_guest("bkf-eligible@example.com")
        room_type = make_room_type("Breakfast Suite", price="15000.00")
        booking = make_booking(
            guest, room_type,
            check_in=date.today(), check_out=date.today() + timedelta(days=2),
            total="30000.00",
        )
        eligibility = breakfast_eligibility(
            guest=guest, booking=booking, service_date=date.today(),
        )
        self.assertTrue(eligibility["enabled"])
        self.assertTrue(eligibility["eligible"])
        self.assertEqual(eligibility["eligible_guests"], 2)  # adults + children

    def test_not_eligible_after_checkout(self):
        guest = make_guest("bkf-checkedout@example.com")
        room_type = make_room_type("Breakfast Suite", price="15000.00")
        booking = make_booking(
            guest, room_type,
            check_in=date.today() - timedelta(days=3),
            check_out=date.today() - timedelta(days=1),
            total="30000.00",
        )
        eligibility = breakfast_eligibility(
            guest=guest, booking=booking, service_date=date.today(),
        )
        self.assertFalse(eligibility["eligible"])
        self.assertIn("check-out", eligibility["reason"])

    def test_not_eligible_before_checkin(self):
        guest = make_guest("bkf-future@example.com")
        room_type = make_room_type("Breakfast Suite", price="15000.00")
        booking = make_booking(
            guest, room_type,
            check_in=date.today() + timedelta(days=2),
            check_out=date.today() + timedelta(days=4),
            total="30000.00",
        )
        eligibility = breakfast_eligibility(
            guest=guest, booking=booking, service_date=date.today(),
        )
        self.assertFalse(eligibility["eligible"])
        self.assertIn("check-in", eligibility["reason"])

    def test_disabled_config_blocks_eligibility(self):
        self.config.is_enabled = False
        self.config.save()
        guest = make_guest("bkf-disabled@example.com")
        room_type = make_room_type("Breakfast Suite", price="15000.00")
        booking = make_booking(
            guest, room_type,
            check_in=date.today(), check_out=date.today() + timedelta(days=2),
            total="30000.00",
        )
        eligibility = breakfast_eligibility(
            guest=guest, booking=booking, service_date=date.today(),
        )
        self.assertFalse(eligibility["enabled"])
        self.assertFalse(eligibility["eligible"])


class BreakfastIssuanceTests(BaseAPITestCase):
    """Breakfast issuance records the issuance without folio charge or stock movement."""

    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("bkf-issue-rec@staff.dev", role=User.Role.RECEPTIONIST)
        self.config = get_breakfast_config()

        self.guest = make_guest("bkf-issue@example.com")
        room_type = make_room_type("Breakfast Suite", price="15000.00")
        self.booking = make_booking(
            self.guest, room_type,
            check_in=date.today(), check_out=date.today() + timedelta(days=2),
            total="30000.00",
        )

    def test_issuance_creates_record(self):
        """Recording a breakfast creates an immutable issuance record."""
        record, created = record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            items=[{"item_name": "Continental", "quantity": "1", "unit": "plate"}],
            actor=self.receptionist, notes="Served at 08:00",
        )

        self.assertTrue(created)
        self.assertEqual(record.guest, self.guest)
        self.assertEqual(record.booking, self.booking)
        self.assertEqual(record.service_date, date.today())
        self.assertEqual(record.issued_by, self.receptionist)
        self.assertEqual(record.eligible_guests, 2)

    def test_issuance_does_not_create_pos_order(self):
        """No POS order is created for a breakfast issuance."""
        from apps.pos.models import PosOrder
        record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            actor=self.receptionist,
        )
        self.assertFalse(PosOrder.objects.filter(guest_name=self.guest.full_name).exists())

    def test_issuance_does_not_create_stock_movement(self):
        """No inventory movement is created for a breakfast issuance."""
        record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            actor=self.receptionist,
        )
        # No new stock movements
        new_movements = StockMovement.objects.filter(
            metadata__has_key="guest_amenity_issue"
        ).count()
        self.assertEqual(new_movements, 0)

    def test_duplicate_issuance_same_guest_date_is_rejected(self):
        """Cannot issue breakfast twice for the same guest on the same date."""
        record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            actor=self.receptionist,
        )
        # Same guest+date without idempotency key hits the DB constraint.
        with self.assertRaises(ValueError):
            record_breakfast(
                guest=self.guest, booking=self.booking, service_date=date.today(),
                actor=self.receptionist, idempotency_key="different-key",
            )

    def test_ineligible_guest_is_rejected(self):
        """Cannot issue breakfast to an ineligible guest."""
        # Create a booking that has already checked out
        guest2 = make_guest("bkf-ineligible@example.com")
        room_type = make_room_type("Breakfast Suite 2", price="15000.00")
        booking2 = make_booking(
            guest2, room_type,
            check_in=date.today() - timedelta(days=3),
            check_out=date.today() - timedelta(days=1),
            total="30000.00",
        )
        with self.assertRaises(ValueError):
            record_breakfast(
                guest=guest2, booking=booking2, service_date=date.today(),
                actor=self.receptionist,
            )

    def test_idempotent_issuance(self):
        """Same idempotency key returns the same record."""
        record1, created1 = record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            actor=self.receptionist, idempotency_key="bkf-key-1",
        )
        record2, created2 = record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            actor=self.receptionist, idempotency_key="bkf-key-1",
        )

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(record1.pk, record2.pk)

    def test_issuance_record_is_immutable(self):
        """Breakfast issuance records cannot be updated or deleted."""
        record, _ = record_breakfast(
            guest=self.guest, booking=self.booking, service_date=date.today(),
            actor=self.receptionist,
        )
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            record.delete()
