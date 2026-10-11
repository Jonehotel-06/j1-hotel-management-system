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

from apps.accounts.capabilities import has_capability
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


class AuditProbeContractGapTests(BaseAPITestCase):
    """The audit's own probe register recorded six capability/route gaps.

    ``ROLE_PERMISSION_PROBE_REGISTER.csv`` flagged exactly six of its 391
    probes as contract gaps - four where ``audit.view`` was granted while the
    audit-log route answered 403, and two where booking capabilities were
    granted while the booking list answered 403. Each row is pinned here as
    *agreement*: the grant and the route now say the same thing, so the matrix a
    manager reads can no longer describe a permission the API refuses.
    """

    # (role, expected capability grant, expected route status)
    AUDIT_LOG_ROWS = (
        (User.Role.MANAGER, False, 403),
        (User.Role.GENERAL_MANAGER, False, 403),
        (User.Role.ACCOUNTS_MANAGER, False, 403),
        (User.Role.SECURITY, False, 403),
    )
    BOOKING_LIST_ROWS = (
        (User.Role.GENERAL_MANAGER, True, 200),
        (User.Role.FRONT_DESK_SUPERVISOR, True, 200),
    )

    def test_audit_log_grants_and_the_route_agree_for_every_flagged_role(self):
        for index, (role, expected_grant, expected_status) in enumerate(self.AUDIT_LOG_ROWS):
            with self.subTest(role=role):
                staff = make_staff(f"probe.audit{index}@staff.dev", role=role)
                self.auth(staff)
                self.assertEqual(
                    has_capability(staff, "audit.view"), expected_grant,
                    f"{role}: the capability matrix and the enforced policy disagree",
                )
                response = self.client.get("/api/admin/audit-logs/")
                self.assertEqual(response.status_code, expected_status, response.content)

    def test_booking_grants_and_the_reservation_desk_agree_for_every_flagged_role(self):
        for index, (role, expected_grant, expected_status) in enumerate(self.BOOKING_LIST_ROWS):
            with self.subTest(role=role):
                staff = make_staff(f"probe.booking{index}@staff.dev", role=role)
                self.auth(staff)
                self.assertEqual(has_capability(staff, "booking.read"), expected_grant)
                response = self.client.get("/api/admin/bookings/")
                self.assertEqual(response.status_code, expected_status, response.content)

    def test_the_register_rows_are_the_only_gaps_and_they_are_closed(self):
        """A role that holds no audit grant must not reach the audit route."""
        for index, role in enumerate(
            (User.Role.RECEPTIONIST, User.Role.CASHIER, User.Role.HOUSEKEEPING, User.Role.STOREKEEPER)
        ):
            with self.subTest(role=role):
                staff = make_staff(f"probe.other{index}@staff.dev", role=role)
                self.auth(staff)
                self.assertFalse(has_capability(staff, "audit.view"))
                self.assertEqual(self.client.get("/api/admin/audit-logs/").status_code, 403)


class AuditViewPersistenceTests(BaseAPITestCase):
    """N9: the persisted grant must match the enforced policy.

    The audit-log endpoint is administrator-only. Migration 0014 had seeded
    `audit.view` onto MANAGER / GENERAL_MANAGER / ACCOUNTS_MANAGER / SECURITY,
    so the capability matrix advertised a permission the API refused.
    `0019_audit_view_is_administrator_only` reconciles the stored grants; this
    test pins the result (and would fail if a later seed re-introduced them).
    """

    def test_only_admin_holds_the_audit_view_grant(self):
        from apps.accounts.models import RoleCapability

        holders = set(
            RoleCapability.objects.filter(capability__code="audit.view")
            .values_list("role", flat=True)
        )
        self.assertEqual(holders, {"ADMIN"})

    def test_non_admin_audit_holder_gets_403_from_the_audit_endpoint(self):
        from .factories import make_staff

        self.auth(make_staff("n9.manager@staff.dev", role=User.Role.MANAGER))
        response = self.client.get("/api/admin/audit-logs/")
        self.assertEqual(response.status_code, 403, response.content)
