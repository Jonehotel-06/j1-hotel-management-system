"""Capability-matrix regression tests for additive operational RBAC."""
from types import SimpleNamespace

from apps.accounts.capabilities import capability_codes_for_user, has_capability
from apps.accounts.models import Capability, RoleCapability, User
from apps.core.permissions import HasCapability

from .base import BaseAPITestCase
from .factories import make_staff, make_user


class CapabilityMatrixTests(BaseAPITestCase):
    def test_seeded_catalog_and_role_mapping_exist(self):
        self.assertGreaterEqual(Capability.objects.count(), 20)
        self.assertTrue(RoleCapability.objects.filter(role=User.Role.RECEPTIONIST).exists())
        self.assertTrue(RoleCapability.objects.filter(role=User.Role.CASHIER).exists())

    def test_legacy_roles_map_safely_to_initial_capabilities(self):
        admin = make_staff("cap-admin@staff.dev", role=User.Role.ADMIN)
        manager = make_staff("cap-manager@staff.dev", role=User.Role.MANAGER)
        receptionist = make_staff("cap-reception@staff.dev", role=User.Role.RECEPTIONIST)
        guest = make_user("cap-guest@example.com", role=User.Role.GUEST)

        self.assertTrue(has_capability(admin, "inventory.manage"))
        self.assertTrue(has_capability(manager, "reports.financial.view"))
        self.assertTrue(has_capability(receptionist, "stay.check_in"))
        self.assertFalse(has_capability(receptionist, "payment.refund.approve"))
        self.assertEqual(capability_codes_for_user(guest), frozenset())

    def test_new_operational_role_is_staff_but_does_not_gain_legacy_broad_endpoint_access(self):
        cashier = make_staff("cap-cashier@staff.dev", role=User.Role.CASHIER)
        self.assertTrue(cashier.is_staff_member)
        self.assertTrue(has_capability(cashier, "payment.capture"))
        self.assertFalse(has_capability(cashier, "booking.read"))
        self.assertFalse(has_capability(cashier, "payment.read"))
        self.assertFalse(has_capability(cashier, "settings.manage"))

        accountant = make_staff("cap-accountant@staff.dev", role=User.Role.ACCOUNTANT)
        self.assertTrue(has_capability(accountant, "payment.read"))
        self.assertFalse(has_capability(accountant, "booking.read"))
        self.assertFalse(has_capability(accountant, "payment.receipt.send"))

        # Existing broad endpoint rules intentionally stay on their old
        # ADMIN/MANAGER/RECEPTIONIST allowlist until migrated individually.
        self.auth(cashier)
        response = self.client.get("/api/admin/bookings/")
        self.assertEqual(response.status_code, 403, response.content)

    def test_capability_permission_fails_closed_and_honours_the_persisted_matrix(self):
        receptionist = make_staff("cap-perm@staff.dev", role=User.Role.RECEPTIONIST)
        request = SimpleNamespace(user=receptionist)
        permission = HasCapability()

        self.assertTrue(permission.has_permission(request, SimpleNamespace(required_capability="booking.manage")))
        self.assertFalse(permission.has_permission(request, SimpleNamespace(required_capability="settings.manage")))
        self.assertFalse(permission.has_permission(request, SimpleNamespace()))

    def test_capability_endpoint_returns_server_resolved_codes(self):
        receptionist = make_staff("cap-endpoint@staff.dev", role=User.Role.RECEPTIONIST)
        self.auth(receptionist)
        response = self.client.get("/api/auth/capabilities/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["role"], User.Role.RECEPTIONIST)
        self.assertIn("booking.manage", data["capabilities"])
        self.assertNotIn("settings.manage", data["capabilities"])
