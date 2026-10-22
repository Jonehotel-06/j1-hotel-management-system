"""The dashboard gate must match the roles the frontend routes to it.

``frontend/js/dashboard.js`` (``PAGE_ACCESS``) sends ADMIN, MANAGER,
RECEPTIONIST, FRONT_DESK_SUPERVISOR and GENERAL_MANAGER to ``index.html`` and
renders the link for them. The API used to gate ``/api/admin/dashboard/`` with
``IsStaffRole`` (ADMIN/MANAGER/RECEPTIONIST only), so the last two roles landed
on a page whose only request answered 403 — the audit's GET probe measured that
for 22 of 26 staff roles, including the general manager.

``apps.core.permissions.DASHBOARD_ROLES`` is now exactly the union of the
legacy allowlist and those two roles. Nothing else is granted: every department
worker keeps their own screen and is still refused, and the financial block of
the payload keeps its existing role gate.
"""
from apps.accounts.models import User
from apps.core.permissions import DASHBOARD_ROLES

from .base import BaseAPITestCase
from .factories import make_staff, make_user

DASHBOARD_URL = "/api/admin/dashboard/"


class DashboardRoleAccessTests(BaseAPITestCase):
    def test_roles_the_frontend_routes_to_the_dashboard_can_load_it(self):
        for index, role in enumerate(
            (
                User.Role.ADMIN,
                User.Role.MANAGER,
                User.Role.RECEPTIONIST,
                User.Role.FRONT_DESK_SUPERVISOR,
                User.Role.GENERAL_MANAGER,
            )
        ):
            with self.subTest(role=role):
                staff = make_staff(f"dash.role{index}@staff.dev", role=role)
                self.auth(staff)
                response = self.client.get(DASHBOARD_URL)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertIn("rooms", response.json()["data"])

    def test_department_workers_are_still_refused(self):
        for index, role in enumerate(
            (
                User.Role.CHEF,
                User.Role.WAITER,
                User.Role.HOUSEKEEPER,
                User.Role.PORTER,
                User.Role.STOREKEEPER,
                User.Role.SECURITY,
            )
        ):
            with self.subTest(role=role):
                staff = make_staff(f"dash.worker{index}@staff.dev", role=role)
                self.auth(staff)
                self.assertEqual(self.client.get(DASHBOARD_URL).status_code, 403)

    def test_guest_accounts_are_still_refused(self):
        guest = make_user("dash.guest@example.com", password="Str0ng!Pass")
        self.auth(guest)
        self.assertEqual(self.client.get(DASHBOARD_URL).status_code, 403)

    def test_the_allowlist_is_the_documented_one(self):
        self.assertEqual(
            set(DASHBOARD_ROLES),
            {"ADMIN", "MANAGER", "RECEPTIONIST", "FRONT_DESK_SUPERVISOR", "GENERAL_MANAGER"},
        )

    def test_receptionist_still_receives_no_financial_block(self):
        receptionist = make_staff("dash.desk@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(receptionist)
        data = self.client.get(DASHBOARD_URL).json()["data"]
        self.assertNotIn("revenue", data)
        self.assertNotIn("ledger", data)
