"""Management tier on the guest-amenity / breakfast station.

WHAT WENT WRONG (reported from the floor): a general manager covering the front
desk for an hour could not issue soap/toothbrush to a checking-in guest or record
a complementary breakfast — those two endpoints answered 403 while every other
part of the front desk's work was available to them. Two defects sat on top of
each other:

1. migration 0037 granted ``guest_amenity.allocate`` to GENERAL_MANAGER and
   MANAGER but never ``guest_amenity.issue`` / ``breakfast.issue``, and
2. ``ROLE_CAPABILITY_CODES`` in apps/accounts/capabilities.py did not list even
   the allocation for those roles, so the maintained matrix an auditor reads
   disagreed with the rows 0037 wrote.

Fixed by 0038_management_tier_guest_amenity_breakfast (additive, nothing
revoked) plus the matching matrix entries. This module pins the RESULT in both
directions: the management tier can work the station, and no other department
gains a grant it did not have.

The department boundaries that must NOT move:
  * store keeper  -> allocate           (and NOT issue to a guest)
  * receptionist  -> issue + breakfast  (and NOT allocate from the main store)
  * waiter / housekeeping / accountant / procurement officer -> nothing here
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from apps.accounts.capabilities import ROLE_CAPABILITY_CODES, capability_codes_for_user
from apps.accounts.models import Capability, RoleCapability, User
from apps.inventory.models import StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement
from apps.core.utils import hotel_today
from tests.base import BaseAPITestCase
from tests.factories import make_booking, make_guest, make_room_type, make_staff

BASE = "/api/admin/service-requests"
AMENITY_CODES = ("guest_amenity.allocate", "guest_amenity.issue", "breakfast.issue")


class CapabilityMatrixTests(TestCase):
    """The static matrix and the persisted grants must say the same thing."""

    def test_matrix_grants_the_station_to_the_management_tier(self):
        for role in ("GENERAL_MANAGER", "MANAGER"):
            with self.subTest(role=role):
                codes = ROLE_CAPABILITY_CODES[role]
                for code in AMENITY_CODES:
                    self.assertIn(code, codes, f"{role} must hold {code}")

    def test_matrix_keeps_the_department_boundaries(self):
        # The store keeper allocates; issuing to a guest is the front desk's job.
        self.assertIn("guest_amenity.allocate", ROLE_CAPABILITY_CODES["STOREKEEPER"])
        self.assertNotIn("guest_amenity.issue", ROLE_CAPABILITY_CODES["STOREKEEPER"])
        # The front desk issues and records breakfast; it does not allocate from
        # the main store.
        self.assertIn("guest_amenity.issue", ROLE_CAPABILITY_CODES["RECEPTIONIST"])
        self.assertIn("breakfast.issue", ROLE_CAPABILITY_CODES["RECEPTIONIST"])
        self.assertNotIn("guest_amenity.allocate", ROLE_CAPABILITY_CODES["RECEPTIONIST"])
        # Nobody else is dragged in by the widening.
        for role in ("WAITER", "HOUSEKEEPING", "ACCOUNTANT", "PROCUREMENT_OFFICER", "SECURITY"):
            with self.subTest(role=role):
                codes = ROLE_CAPABILITY_CODES[role]
                for code in AMENITY_CODES:
                    self.assertNotIn(code, codes)

    def test_admin_still_holds_every_capability_by_construction(self):
        from apps.accounts.capabilities import ALL_CAPABILITY_CODES

        self.assertIs(ROLE_CAPABILITY_CODES["ADMIN"], ALL_CAPABILITY_CODES)

    def test_persisted_grants_match_the_matrix_for_the_station(self):
        """Migration 0038 must have written the same grants the matrix claims."""
        for role in ("GENERAL_MANAGER", "MANAGER", "RECEPTIONIST", "STOREKEEPER"):
            for code in AMENITY_CODES:
                row_exists = RoleCapability.objects.filter(
                    role=role, capability__code=code, capability__is_active=True
                ).exists()
                self.assertEqual(
                    row_exists, code in ROLE_CAPABILITY_CODES[role],
                    f"{role} / {code}: persisted grant and matrix disagree",
                )

    def test_a_general_manager_resolves_the_codes_from_persisted_rows(self):
        gm = make_staff("gm-amenity-cap@staff.dev", role=User.Role.GENERAL_MANAGER)
        codes = capability_codes_for_user(gm)
        for code in AMENITY_CODES:
            self.assertIn(code, codes, f"GM cannot resolve {code} from persisted grants")
        self.assertEqual(User.objects.get(pk=gm.pk).role, User.Role.GENERAL_MANAGER)


class AmenityStationAPITests(BaseAPITestCase):
    """End-to-end: who may actually work the station, over HTTP."""

    def setUp(self):
        super().setUp()
        self.gm = make_staff("gm-amenity@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.manager = make_staff("manager-amenity@staff.dev", role=User.Role.MANAGER)
        self.receptionist = make_staff("rec-amenity@staff.dev", role=User.Role.RECEPTIONIST)
        self.keeper = make_staff("keeper-amenity@staff.dev", role=User.Role.STOREKEEPER)
        self.housekeeper = make_staff("hk-amenity@staff.dev", role=User.Role.HOUSEKEEPING)
        self.waiter = make_staff("waiter-amenity@staff.dev", role=User.Role.WAITER)

        self.reception_loc = StockLocation.objects.create(code="RECEPTION", name="Reception Desk")
        self.main_store = StockLocation.objects.create(code="MAIN", name="Main Store")
        self.soap = StockItem.objects.create(
            sku="AMY-SOAP", name="Guest soap", base_unit="BAR", standard_cost=Decimal("50.00"),
        )
        record_stock_movement(
            item=self.soap, location=self.main_store, movement_type=StockMovement.Type.OPENING,
            quantity_delta="100", source_key="mt-amenity-opening", actor=self.keeper,
        )

        self.guest = make_guest("amenity-mt-guest@example.com")
        room_type = make_room_type("Amenity MT Suite", price="12000.00")
        self.booking = make_booking(
            self.guest, room_type,
            check_in=hotel_today(), check_out=hotel_today() + timedelta(days=2),
            total="24000.00",
        )

    # ------------------------------ helpers --------------------------------
    def allocate(self, actor):
        self.auth(actor)
        return self.client.post(f"{BASE}/reception/amenities/allocate/", {
            "item_id": self.soap.pk,
            "from_location_id": self.main_store.pk,
            "quantity": "10",
        }, format="json")

    def issue(self, actor):
        self.auth(actor)
        return self.client.post(f"{BASE}/reception/amenities/issue/", {
            "guest_id": self.guest.pk,
            "booking_id": self.booking.pk,
            "room_number": "101",
            "item_id": self.soap.pk,
            "quantity": "1",
        }, format="json")

    def stock(self, actor):
        self.auth(actor)
        return self.client.get(f"{BASE}/reception/amenities/stock/")

    def issues(self, actor):
        self.auth(actor)
        return self.client.get(f"{BASE}/reception/amenities/issues/")

    def breakfast_config(self, actor):
        self.auth(actor)
        return self.client.get(f"{BASE}/breakfast/config/")

    def breakfast_issue(self, actor):
        self.auth(actor)
        return self.client.post(f"{BASE}/breakfast/issue/", {
            "guest_id": self.guest.pk,
            "booking_id": self.booking.pk,
            "service_date": hotel_today().isoformat(),
            "guests_count": 1,
        }, format="json")

    # --------------------- the reported failure, now working ----------------
    def test_general_manager_can_run_the_whole_station(self):
        self.assertEqual(self.stock(self.gm).status_code, 200)
        allocated = self.allocate(self.gm)
        self.assertIn(allocated.status_code, (200, 201), allocated.content)
        issued = self.issue(self.gm)
        self.assertIn(issued.status_code, (200, 201), issued.content)
        self.assertEqual(self.issues(self.gm).status_code, 200)
        self.assertEqual(self.breakfast_config(self.gm).status_code, 200)
        breakfast = self.breakfast_issue(self.gm)
        self.assertIn(breakfast.status_code, (200, 201), breakfast.content)

    def test_manager_can_run_the_whole_station(self):
        self.assertEqual(self.stock(self.manager).status_code, 200)
        self.assertIn(self.allocate(self.manager).status_code, (200, 201))
        self.assertIn(self.issue(self.manager).status_code, (200, 201))
        self.assertIn(self.breakfast_issue(self.manager).status_code, (200, 201))

    def test_receptionist_issues_but_never_allocates(self):
        self.assertEqual(self.allocate(self.receptionist).status_code, 403)
        # The desk's own half of the station stays open to it.
        self.assertIn(self.issues(self.receptionist).status_code, (200, 200))
        self.assertIn(self.breakfast_config(self.receptionist).status_code, (200, 200))

    def test_receptionist_can_see_the_shelf_it_issues_from(self):
        """The issuing clerk must be able to READ the Reception balances.

        This used to answer 403: the stock view was gated on
        ``guest_amenity.allocate`` only, which the front desk does not hold. The
        desk could therefore issue an item it was never allowed to look at — it
        only discovered an empty allocation from the rejection that came back
        after the guest had already been promised the soap. Reading the
        department's own shelf is not allocating: the write paths are unchanged.
        """
        self.assertEqual(self.stock(self.receptionist).status_code, 200)
        self.assertEqual(self.allocate(self.receptionist).status_code, 403)

    def test_store_keeper_allocates_but_never_issues_to_a_guest(self):
        self.assertIn(self.allocate(self.keeper).status_code, (200, 201))
        self.assertEqual(self.stock(self.keeper).status_code, 200)
        self.assertEqual(self.issue(self.keeper).status_code, 403)
        self.assertEqual(self.issues(self.keeper).status_code, 403)
        self.assertEqual(self.breakfast_issue(self.keeper).status_code, 403)

    def test_the_staff_guest_list_exists_for_the_desk(self):
        """The amenity/breakfast guest pickers read /api/admin/guests/.

        The pages asked for /api/bookings/guests/, which is not a route: it is
        captured by the guest booking detail route as ``lookup="guests"`` and
        never returns a guest list, so both pickers rendered empty and the issue
        forms could not be submitted at all. Pin the working route AND the trap.
        """
        self.auth(self.receptionist)
        self.assertEqual(self.client.get("/api/admin/guests/").status_code, 200)
        # The path the pages used to call is the guest booking detail view, not a
        # guest list: it must not answer with a page of guests.
        legacy = self.client.get("/api/bookings/guests/")
        self.assertIn(legacy.status_code, (401, 403, 404), legacy.content[:200])
        if legacy.status_code == 200:
            self.fail("/api/bookings/guests/ unexpectedly returned a guest list")

    def test_other_departments_are_refused_everywhere(self):
        for actor in (self.housekeeper, self.waiter):
            with self.subTest(role=actor.role):
                self.assertEqual(self.stock(actor).status_code, 403)
                self.assertEqual(self.allocate(actor).status_code, 403)
                self.assertEqual(self.issue(actor).status_code, 403)
                self.assertEqual(self.issues(actor).status_code, 403)
                self.assertEqual(self.breakfast_config(actor).status_code, 403)

    def test_a_signed_out_visitor_is_refused(self):
        self.unauth()
        self.assertIn(self.stock(None).status_code, (401, 403))
        self.assertIn(self.issue(None).status_code, (401, 403))
