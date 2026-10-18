"""Bounded, capability-scoped staff assignment directory coverage."""

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User
from apps.staff_operations.services.staff_operations_service import ensure_staff_profile

from .base import BaseAPITestCase
from .factories import make_staff


class OperationalStaffDirectoryApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.receptionist = make_staff("directory-reception@staff.dev", role=User.Role.RECEPTIONIST)
        self.housekeeper = make_staff("directory-housekeeper@staff.dev", role=User.Role.HOUSEKEEPING)
        self.technician = make_staff("directory-maintenance@staff.dev", role=User.Role.MAINTENANCE)
        self.inactive = make_staff("directory-inactive@staff.dev", role=User.Role.HOUSEKEEPING)
        self.inactive.is_active = False
        self.inactive.save(update_fields=["is_active", "updated_at"])

    def test_dispatcher_gets_only_minimal_active_role_filtered_picker_rows(self):
        self.auth(self.receptionist)
        response = self.client.get("/api/admin/users/directory/", {"roles": "HOUSEKEEPING", "search": "directory"})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.housekeeper.pk)
        self.assertEqual(rows[0]["role"], User.Role.HOUSEKEEPING)
        self.assertEqual(set(rows[0]), {"id", "full_name", "email", "role"})

    def test_payroll_manager_gets_only_the_minimal_picker_projection(self):
        accounts = make_staff("directory.accounts@staff.dev", role=User.Role.ACCOUNTS_MANAGER)
        self.auth(accounts)
        response = self.client.get("/api/admin/users/directory/", {"search": "directory-housekeeper"})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual([row["id"] for row in rows], [self.housekeeper.pk])
        self.assertEqual(set(rows[0]), {"id", "full_name", "email", "role"})

    def test_dispatch_only_roles_receive_no_staff_email_addresses(self):
        """The picker is reachable by design; the addresses are not (audit N7).

        The audit's own probe register records the expected gate for this
        endpoint as 200 for the operational roles - it is a dispatch picker, not
        an account directory. What it must not leak is contact data: email is an
        ACCOUNT field and is withheld unless the caller holds ``account.view``.
        """
        self.auth(self.receptionist)
        self.assertFalse(has_capability(self.receptionist, "account.view"), "this test needs a caller without account.view")
        response = self.client.get("/api/admin/users/directory/")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertTrue(rows, "the picker must still return assignable staff")
        for row in rows:
            self.assertIsNone(row["email"], f"{row['id']} leaked an email address")
            self.assertEqual(set(row), {"id", "full_name", "email", "role"})

    def test_callers_holding_account_view_still_receive_the_address(self):
        """The withholding above is capability-driven, not a blanket removal."""
        manager = make_staff("directory-manager@staff.dev", role=User.Role.MANAGER)
        self.auth(manager)
        self.assertTrue(has_capability(manager, "account.view"))
        response = self.client.get("/api/admin/users/directory/", {"roles": "HOUSEKEEPING"})
        self.assertEqual(response.status_code, 200, response.content)
        emails = {row["email"] for row in response.json()["data"]}
        self.assertIn(self.housekeeper.email, emails)
        self.assertNotIn(None, emails)

    def test_supervisor_terminal_picker_can_find_staff_by_employee_code(self):
        """The reception-terminal fallback selector is backed by this endpoint."""
        profile, _ = ensure_staff_profile(staff=self.housekeeper)
        manager = make_staff("directory-terminal-manager@staff.dev", role=User.Role.MANAGER)
        self.auth(manager)
        response = self.client.get(
            "/api/admin/users/directory/", {"search": profile.employee_code}
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual([row["id"] for row in rows], [self.housekeeper.pk])
        self.assertEqual(rows[0]["email"], self.housekeeper.email)

    def test_non_dispatching_or_payroll_role_cannot_list_operational_staff(self):
        cashier = make_staff("directory-cashier@staff.dev", role=User.Role.CASHIER)
        self.auth(cashier)
        response = self.client.get("/api/admin/users/directory/")
        self.assertEqual(response.status_code, 403, response.content)

    def test_the_owner_administrator_is_never_offered_as_staff(self):
        """The Administrator is the hotel owner, not an assignable employee.

        Reported: the CEO appeared as staff. The owner cannot be rostered,
        clocked, put on leave or disciplined, so the picker never returns the
        Administrator account — while still returning real staff.
        """
        owner = make_staff("directory-owner@staff.dev", role=User.Role.ADMIN)
        self.auth(owner)
        response = self.client.get("/api/admin/users/directory/")
        self.assertEqual(response.status_code, 200, response.content)
        ids = {row["id"] for row in response.json()["data"]}
        self.assertNotIn(owner.pk, ids, "the owner must not be assignable as staff")
        self.assertTrue(ids, "and the picker still returns the hotel's staff")
        # Asking for administrators explicitly returns nobody, not the owner.
        explicit = self.client.get("/api/admin/users/directory/", {"roles": "ADMIN"})
        self.assertEqual(explicit.status_code, 200, explicit.content)
        self.assertEqual(explicit.json()["data"], [])
