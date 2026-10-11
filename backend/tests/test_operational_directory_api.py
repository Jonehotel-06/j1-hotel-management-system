"""Bounded, capability-scoped staff assignment directory coverage."""

from apps.accounts.capabilities import has_capability
from apps.accounts.models import User

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

    def test_non_dispatching_or_payroll_role_cannot_list_operational_staff(self):
        cashier = make_staff("directory-cashier@staff.dev", role=User.Role.CASHIER)
        self.auth(cashier)
        response = self.client.get("/api/admin/users/directory/")
        self.assertEqual(response.status_code, 403, response.content)
