"""Page↔API alignment for the roles the frontend actually routes.

Each expectation here comes from two independent sources that disagreed:

* ``frontend/js/dashboard.js`` ``PAGE_ACCESS`` — which pages a role is allowed
  to open (and is linked to in the navigation);
* ``ROLE_CAPABILITY_CODES`` — the capabilities the role is granted.

The API used to answer 403 to the very roles those two sources admitted. Every
case below fails on the pre-fix tree and passes now; the negative assertions
keep the corrections from widening anything they were not meant to.
"""
from django.test import RequestFactory

from apps.accounts.models import User
from apps.core.permissions import IsStaffReadOnlyManagerWrite

from .base import BaseAPITestCase
from .factories import make_staff

DASHBOARD = "/api/admin/dashboard/"
ROOMS = "/api/admin/rooms/"
OFFERS = "/api/admin/offers/"
REPORTS_REVENUE = "/api/admin/reports/revenue/"
ATTENDANCE = "/api/admin/staff-operations/attendance/"


class GeneralManagerParityTests(BaseAPITestCase):
    """GENERAL_MANAGER holds the same 50 capabilities as MANAGER."""

    def setUp(self):
        super().setUp()
        self.gm = make_staff("gm@staff.dev", role=User.Role.GENERAL_MANAGER)
        self.auth(self.gm)

    def test_dashboard_loads_with_the_financial_block(self):
        response = self.client.get(DASHBOARD)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIn("rooms", data)
        self.assertIn("ledger", data)
        self.assertIn("ledger_month", data)

    def test_revenue_report_is_readable(self):
        from apps.core.utils import hotel_today

        today = hotel_today().isoformat()
        response = self.client.get(REPORTS_REVENUE, {"start_date": today, "end_date": today})
        self.assertEqual(response.status_code, 200, response.content)

    def test_offers_and_rooms_are_readable(self):
        for url in (OFFERS, ROOMS):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_general_manager_is_not_an_administrator(self):
        for url in ("/api/admin/users/", "/api/admin/audit-logs/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_legacy_configuration_writes_follow_the_manager_set(self):
        factory = RequestFactory()
        request = factory.post(OFFERS)
        request.user = self.gm
        self.assertTrue(IsStaffReadOnlyManagerWrite().has_permission(request, None))


class FrontDeskSupervisorReadTests(BaseAPITestCase):
    """The supervisor runs the desk: rooms read yes, configuration write no."""

    def setUp(self):
        super().setUp()
        self.supervisor = make_staff("fds@staff.dev", role=User.Role.FRONT_DESK_SUPERVISOR)
        self.auth(self.supervisor)

    def test_room_inventory_is_readable(self):
        for url in (ROOMS, "/api/admin/room-types/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_offers_are_read_only(self):
        self.assertEqual(self.client.get(OFFERS).status_code, 200)
        self.assertEqual(self.client.post(OFFERS, {}, format="json").status_code, 403)

    def test_dashboard_loads_without_the_financial_block(self):
        response = self.client.get(DASHBOARD)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("ledger", response.json()["data"])

    def test_administration_stays_closed(self):
        self.assertEqual(self.client.get("/api/admin/users/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/audit-logs/").status_code, 403)


class StaffProfileCardTests(BaseAPITestCase):
    """``/api/admin/users/staff/<pk>/`` follows ``staff.view``, not a role tuple."""

    def setUp(self):
        super().setUp()
        self.subject = make_staff("card.subject@staff.dev", role=User.Role.HOUSEKEEPER)
        self.url = f"/api/admin/users/staff/{self.subject.pk}/"

    def _status_for(self, role, email):
        user = make_staff(email, role=role)
        self.auth(user)
        status = self.client.get(self.url).status_code
        self.auth(user)
        own = self.client.get(f"/api/admin/users/staff/{user.pk}/").status_code
        return status, own

    def test_hr_manager_and_general_manager_can_open_the_card(self):
        for index, role in enumerate((User.Role.HR_MANAGER, User.Role.GENERAL_MANAGER, User.Role.MANAGER)):
            with self.subTest(role=role):
                other, own = self._status_for(role, f"card.role{index}@staff.dev")
                self.assertEqual(other, 200)
                self.assertEqual(own, 200)

    def test_roles_without_staff_view_see_only_themselves(self):
        for index, role in enumerate((User.Role.ACCOUNTANT, User.Role.CASHIER, User.Role.CHEF)):
            with self.subTest(role=role):
                other, own = self._status_for(role, f"card.other{index}@staff.dev")
                self.assertEqual(other, 403)
                self.assertEqual(own, 200)

    def test_guests_are_refused_even_for_their_own_card(self):
        from .factories import make_user

        guest = make_user("card.guest@example.com", password="Str0ng!Pass")
        self.auth(guest)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(f"/api/admin/users/staff/{guest.pk}/").status_code, 403)


class AccountsManagerSelfServiceTests(BaseAPITestCase):
    """Part C: the accounts manager is an employee and can record attendance."""

    def setUp(self):
        super().setUp()
        self.accounts = make_staff("accounts@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        self.auth(self.accounts)

    def test_attendance_endpoints_are_reachable(self):
        self.assertEqual(self.client.get(ATTENDANCE).status_code, 200)

    def test_the_persisted_grant_exists(self):
        from apps.accounts.capabilities import has_capability

        self.assertTrue(has_capability(self.accounts, "attendance.clock"))

    def test_the_grant_does_not_leak_to_related_roles(self):
        from apps.accounts.capabilities import has_capability

        for index, role in enumerate((User.Role.CASHIER, User.Role.STOREKEEPER, User.Role.SECURITY)):
            with self.subTest(role=role):
                user = make_staff(f"accounts.peer{index}@staff.dev", role=role)
                self.assertTrue(has_capability(user, "attendance.clock"))
