from apps.accounts.capabilities import capability_codes_for_user
from apps.accounts.models import Capability, User, UserCapabilityOverride
from tests.base import BaseAPITestCase
from tests.factories import make_staff


class ProcurementDashboardAccessTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.officer = make_staff("dashboard-proc@staff.dev", role=User.Role.PROCUREMENT_OFFICER)
        self.accountant = make_staff("dashboard-acct@staff.dev", role=User.Role.ACCOUNTANT)
        self.storekeeper = make_staff("dashboard-store@staff.dev", role=User.Role.STOREKEEPER)

    def test_inventory_procurement_and_payroll_apis(self):
        self.auth(self.officer)
        for url in ("/api/admin/inventory/items/", "/api/admin/inventory/locations/", "/api/admin/inventory/purchase-orders/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for url in ("/api/admin/staff-operations/payroll/periods/", "/api/admin/staff-operations/payroll/compensation/", "/api/admin/staff-operations/payroll/stock-shortages/", "/api/admin/staff-operations/payroll/stock-shortages/no-such-ref/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post("/api/admin/staff-operations/payroll/periods/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/my-payslips/").status_code, 200)
        self.auth(self.accountant)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/periods/").status_code, 200)
        self.auth(self.storekeeper)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/stock-shortages/").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/periods/").status_code, 403)
        self.unauth()
        self.assertIn(self.client.get("/api/admin/inventory/items/").status_code, (401, 403))
        self.assertIn(self.client.get("/api/admin/staff-operations/payroll/periods/").status_code, (401, 403))

    def test_explicit_payroll_grants_cannot_bypass_role_deny(self):
        for code in ("payroll.view", "payroll.manage", "payroll.approve"):
            UserCapabilityOverride.objects.create(user=self.officer, capability=Capability.objects.get(code=code), effect="GRANT", reason="test")
        officer = User.objects.get(pk=self.officer.pk)
        self.assertFalse(any(code.startswith("payroll.") for code in capability_codes_for_user(officer)))
        self.auth(officer)
        self.assertEqual(self.client.get("/api/admin/staff-operations/payroll/periods/").status_code, 403)
        self.assertEqual(self.client.post("/api/admin/staff-operations/payroll/periods/", {}, format="json").status_code, 403)
