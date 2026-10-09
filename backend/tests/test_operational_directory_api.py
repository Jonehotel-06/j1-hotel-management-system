"""Bounded, capability-scoped staff assignment directory coverage."""

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

    def test_non_dispatching_or_payroll_role_cannot_list_operational_staff(self):
        cashier = make_staff("directory-cashier@staff.dev", role=User.Role.CASHIER)
        self.auth(cashier)
        response = self.client.get("/api/admin/users/directory/")
        self.assertEqual(response.status_code, 403, response.content)
