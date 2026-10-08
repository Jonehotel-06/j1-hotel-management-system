# tests/test_role_permissions_matrix.py
"""Task 23: one place that pins the role matrix for every endpoint this
project added, so a permission regression fails loudly rather than quietly
handing a receptionist manager powers (or locking staff out of a read).

Matrix (as agreed):
  * guest discounts  — MANAGER/ADMIN write, all staff read, guests none
  * reschedule       — MANAGER/ADMIN only
  * occupancy/missed — all staff read
  * audit actions    — ADMIN only
"""
from datetime import timedelta
from decimal import Decimal

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.offers.models import GuestDiscount

from .base import BaseAPITestCase
from .factories import (
    hotel_settings,
    make_booking,
    make_guest,
    make_room,
    make_room_type,
    make_staff,
    make_user,
)

STAFF_ROLES = (User.Role.RECEPTIONIST, User.Role.MANAGER, User.Role.ADMIN)
WRITE_ROLES = (User.Role.MANAGER, User.Role.ADMIN)


class RolePermissionMatrixTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        hotel_settings()
        self.today = hotel_today()
        self.room_type = make_room_type("Roles", price="20000.00")
        self.rooms = [make_room(self.room_type, f"R{i}") for i in range(1, 6)]
        self.guest = make_guest("roles.guest@example.com")
        self.staff = {
            role: make_staff(f"roles.{role.lower()}@staff.dev", role=role)
            for role in STAFF_ROLES
        }
        self.plain_guest_user = make_user("roles.plain@example.com", password="Str0ng!Pass")

    # --- guest discounts -----------------------------------------------------
    def _create_discount_payload(self):
        return {
            "guest": self.guest.pk,
            "discount_type": GuestDiscount.DiscountType.PERCENTAGE,
            "discount_value": "10.00",
            "start_date": str(self.today),
            "reason": "Corporate rate",
        }

    def test_only_manager_and_admin_may_create_a_guest_discount(self):
        for role in STAFF_ROLES:
            self.auth(self.staff[role])
            response = self.client.post("/api/admin/guest-discounts/",
                                        self._create_discount_payload())
            if role in WRITE_ROLES:
                self.assertEqual(response.status_code, 201, f"{role}: {response.content[:200]}")
                GuestDiscount.objects.all().delete()
            else:
                self.assertEqual(response.status_code, 403, f"{role} must not write discounts")

    def test_every_staff_role_may_read_guest_discounts(self):
        for role in STAFF_ROLES:
            self.auth(self.staff[role])
            self.assertEqual(self.client.get("/api/admin/guest-discounts/").status_code, 200,
                             f"{role} must be able to read discounts")

    def test_guest_user_and_anonymous_are_locked_out_of_guest_discounts(self):
        self.auth(self.plain_guest_user)
        self.assertEqual(self.client.get("/api/admin/guest-discounts/").status_code, 403)
        self.unauth()
        self.assertIn(self.client.get("/api/admin/guest-discounts/").status_code, (401, 403))

    # --- reschedule ----------------------------------------------------------
    def test_only_manager_and_admin_may_reschedule(self):
        for index, role in enumerate(STAFF_ROLES):
            booking = make_booking(self.guest, self.room_type, [self.rooms[index]],
                                   check_in=self.today - timedelta(days=3),
                                   check_out=self.today - timedelta(days=1))
            self.auth(self.staff[role])
            response = self.client.post(
                f"/api/admin/bookings/{booking.booking_reference}/reschedule/",
                {"check_in": str(self.today + timedelta(days=5)),
                 "check_out": str(self.today + timedelta(days=7)),
                 "reason": "Guest called"},
            )
            if role in WRITE_ROLES:
                self.assertEqual(response.status_code, 200, f"{role}: {response.content[:200]}")
            else:
                self.assertEqual(response.status_code, 403, f"{role} must not reschedule")

    # --- read-only staff surfaces -------------------------------------------
    def test_every_staff_role_may_read_occupancy_and_missed_bookings(self):
        for role in STAFF_ROLES:
            self.auth(self.staff[role])
            self.assertEqual(self.client.get("/api/admin/bookings/calendar/").status_code, 200,
                             f"{role} must see the occupancy calendar")
            self.assertEqual(self.client.get("/api/admin/bookings/missed/").status_code, 200,
                             f"{role} must see missed bookings")

    def test_guest_user_cannot_read_staff_booking_surfaces(self):
        self.auth(self.plain_guest_user)
        self.assertEqual(self.client.get("/api/admin/bookings/calendar/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/bookings/missed/").status_code, 403)

    # --- audit ---------------------------------------------------------------
    def test_audit_action_list_is_admin_only(self):
        for role in STAFF_ROLES:
            self.auth(self.staff[role])
            expected = 200 if role == User.Role.ADMIN else 403
            self.assertEqual(self.client.get("/api/admin/audit-logs/actions/").status_code,
                             expected, f"{role} audit access")
