"""Regression tests for retiring standalone Cashier into Restaurants/WAITER.

The tests cover active configuration, persisted grants, API assignment guards,
cashier workflow reachability for WAITER, and the reversible data migration.
"""
import importlib
from datetime import time, timedelta
from decimal import Decimal

from django.apps import apps
from django.test import SimpleTestCase
from django.utils import timezone

from apps.accounts.capabilities import ROLE_CAPABILITY_CODES, capability_codes_for_user
from apps.accounts.departments import (
    ASSIGNABLE_ROLES,
    DEPARTMENT_ROLES,
    RETIRED_DEPARTMENTAL_ROLES,
    assert_assignable_role,
    canonical_role,
    department_for_role,
    department_for_user,
    department_role_choices,
    is_removed_cashier_department,
    validate_role_department,
)
from apps.accounts.models import Capability, RoleCapability, User, Workstation
from apps.audit.models import AuditLog
from apps.finance.models import CashSession
from apps.staff_operations.models import Department, ShiftAssignment, ShiftTemplate, StaffProfile
from apps.staff_operations.services.fingerprint_service import staff_department_code

from .base import BaseAPITestCase
from .factories import make_staff

MIGRATION = importlib.import_module("apps.accounts.migrations.0029_merge_cashier_into_restaurants")


class ActiveCashierConfigurationTests(SimpleTestCase):
    def test_cashier_is_not_an_active_department_or_assignable_role(self):
        self.assertNotIn("CASHIER", DEPARTMENT_ROLES)
        self.assertNotIn("CASHIER", ASSIGNABLE_ROLES)
        self.assertNotIn("CASHIER", [code for code, _label in Workstation.Department.choices])
        self.assertNotIn(User.Role.CASHIER, [role for role, _label in department_role_choices()])
        self.assertEqual(RETIRED_DEPARTMENTAL_ROLES[User.Role.CASHIER], User.Role.WAITER)
        self.assertEqual(canonical_role(User.Role.CASHIER), User.Role.WAITER)
        self.assertEqual(department_for_role(User.Role.CASHIER), Workstation.Department.RESTAURANT)
        self.assertTrue(is_removed_cashier_department("cashier-till"))
        self.assertFalse(is_removed_cashier_department("RESTAURANT_BAR"))
        with self.assertRaisesMessage(ValueError, "retired departmental title"):
            assert_assignable_role(User.Role.CASHIER)
        with self.assertRaisesMessage(ValueError, "merged into Restaurants"):
            validate_role_department(User.Role.WAITER, "CASHIER")

    def test_waiter_inherits_cashier_capabilities_without_sensitive_approvals(self):
        waiter = set(ROLE_CAPABILITY_CODES[User.Role.WAITER])
        cashier = set(ROLE_CAPABILITY_CODES[User.Role.CASHIER])
        self.assertTrue(cashier.issubset(waiter))
        self.assertIn("restaurant.order.manage", waiter)
        self.assertIn("cash_session.open", waiter)
        self.assertIn("cash_session.close", waiter)
        self.assertIn("pos.return.refund", waiter)
        for sensitive in (
            "payment.refund.approve", "expense.approve", "finance.controls.manage",
            "reports.financial.view", "payment.read", "audit.view", "settings.manage",
            "payroll.view", "payroll.approve", "staff.manage", "shift.manage",
        ):
            self.assertNotIn(sensitive, waiter)


class CashierRestaurantsApiTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("merge.admin@staff.dev", role=User.Role.ADMIN)
        self.waiter = make_staff("merge.waiter@staff.dev", role=User.Role.WAITER)

    def test_creative_write_paths_cannot_recreate_cashier_role_or_department(self):
        self.auth(self.admin)
        retired_role = self.client.post("/api/admin/users/", {
            "email": "merge.new.cashier@staff.dev",
            "first_name": "New",
            "last_name": "Cashier",
            "role": "CASHIER",
            "department": "CASHIER",
            "password": "Str0ngPass!234",
        }, format="json")
        self.assertEqual(retired_role.status_code, 400, retired_role.content)
        self.assertIn("retired departmental title", retired_role.content.decode())

        removed_department = self.client.post("/api/admin/users/", {
            "email": "merge.removed.dept@staff.dev",
            "first_name": "Removed",
            "last_name": "Department",
            "role": "WAITER",
            "department": "CASHIER",
            "password": "Str0ngPass!234",
        }, format="json")
        self.assertEqual(removed_department.status_code, 400, removed_department.content)
        self.assertIn("merged into Restaurants", removed_department.content.decode())

        secondary_path = self.client.post("/api/admin/staff-operations/profiles/", {
            "email": "merge.secondary.cashier@staff.dev",
            "first_name": "Second",
            "last_name": "Cashier",
            "role": "CASHIER",
            "department": "CASHIER",
            "password": "Str0ngPass!234",
        }, format="json")
        self.assertEqual(secondary_path.status_code, 400, secondary_path.content)
        self.assertFalse(User.objects.filter(email="merge.secondary.cashier@staff.dev").exists())

        workstation = self.client.post("/api/admin/users/terminals/", {
            "name": "Retired till",
            "department": "CASHIER",
        }, format="json")
        self.assertEqual(workstation.status_code, 400, workstation.content)

        department = self.client.post("/api/admin/staff-operations/departments/", {
            "code": "CASHIER_TILL",
            "name": "Cashier Till",
            "slug": "cashier-till",
        }, format="json")
        self.assertEqual(department.status_code, 400, department.content)

        role_grant = self.client.post("/api/admin/users/permissions-matrix/", {
            "role": "CASHIER",
            "capability_code": "payment.capture",
            "effect": "GRANT",
        }, format="json")
        self.assertEqual(role_grant.status_code, 400, role_grant.content)

    def test_active_role_and_department_views_hide_retired_cashier(self):
        Department.objects.create(code="CASHIER", name="Cashier", slug="cashier", is_active=True)
        self.auth(self.admin)

        matrix = self.client.get("/api/admin/users/permissions-matrix/")
        self.assertEqual(matrix.status_code, 200, matrix.content)
        self.assertNotIn("CASHIER", [row["role"] for row in matrix.json()["data"]["roles"]])

        departments = self.client.get("/api/admin/staff-operations/departments/")
        self.assertEqual(departments.status_code, 200, departments.content)
        self.assertNotIn("CASHIER", [row["code"] for row in departments.json()["data"]])

        summary = self.client.get("/api/admin/staff-operations/departments/CASHIER/dashboard-summary/")
        self.assertEqual(summary.status_code, 404, summary.content)

    def test_historical_cashier_identity_resolves_to_restaurants_without_losing_grants(self):
        cashier = make_staff("merge.historical.cashier@staff.dev", role=User.Role.CASHIER)
        StaffProfile.objects.create(
            user=cashier,
            employee_code="MERGE-HIST-1",
            department="CASHIER",
        )
        self.assertEqual(canonical_role(cashier.role), User.Role.WAITER)
        self.assertEqual(department_for_user(cashier), Workstation.Department.RESTAURANT)
        self.assertEqual(staff_department_code(cashier), "RESTAURANT_BAR")
        self.assertTrue(RoleCapability.objects.filter(role=User.Role.CASHIER).exists())

    def test_waiter_can_use_transferred_cashier_workflows_but_not_finance_admin_screens(self):
        self.auth(self.waiter)
        codes = capability_codes_for_user(self.waiter)
        for code in ROLE_CAPABILITY_CODES[User.Role.CASHIER]:
            self.assertIn(code, codes)

        listing = self.client.get("/api/admin/finance/cash-sessions/")
        self.assertEqual(listing.status_code, 200, listing.content)
        opened = self.client.post("/api/admin/pos/cash-sessions/open/", {}, format="json")
        self.assertEqual(opened.status_code, 201, opened.content)
        reference = opened.json()["data"]["reference"]
        closed = self.client.post(
            f"/api/admin/pos/cash-sessions/{reference}/close/",
            {"counted_cash": "0.00"},
            format="json",
        )
        self.assertEqual(closed.status_code, 200, closed.content)

        self.assertEqual(self.client.get("/api/admin/finance/expenses/").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/pos/orders/?mode=BAR").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/pos/menu/?area=BAR").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/pos/orders/returns/eligible/").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/payments/").status_code, 403)
        self.assertEqual(
            self.client.get("/api/admin/finance/reports/summary/", {
                "start": "2026-01-01", "end": "2026-01-31",
            }).status_code,
            403,
        )

    def test_department_registry_after_migration_has_no_cashier_role(self):
        accounts = Department.objects.get(code="ACCOUNTS")
        restaurant = Department.objects.get(code="RESTAURANT_BAR")
        self.assertNotIn("CASHIER", accounts.supported_roles)
        self.assertNotIn("Cashier", accounts.job_positions)
        self.assertIn("WAITER", restaurant.supported_roles)
        self.assertIn("POS Cashier", restaurant.job_positions)


class CashierDataMigrationTests(BaseAPITestCase):
    def _cashier_fixture(self):
        cashier = make_staff("migrate.cashier@staff.dev", role=User.Role.CASHIER)
        profile = StaffProfile.objects.create(
            user=cashier,
            employee_code="MERGE-CASH-1",
            department="CASHIER",
        )
        workstation = Workstation.objects.create(name="Old cashier till", department="CASHIER")
        template = ShiftTemplate.objects.create(
            code="MERGE-CASH-SHIFT",
            name="Cashier morning",
            department="CASHIER",
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        now = timezone.now()
        assignment = ShiftAssignment.objects.create(
            reference="MERGE-CASH-ASSIGN-1",
            staff=cashier,
            department="CASHIER",
            planned_start_at=now,
            planned_end_at=now + timedelta(hours=8),
        )
        session = CashSession.objects.create(
            reference="MERGE-CASH-SESSION-1",
            cashier=cashier,
            opening_float=Decimal("0.00"),
        )
        return cashier, profile, workstation, template, assignment, session

    def test_forward_migration_preserves_records_and_merges_unambiguous_staff(self):
        cashier, profile, workstation, template, assignment, session = self._cashier_fixture()
        custom = Capability.objects.create(
            code="cashier.legacy.custom",
            name="Legacy cashier task",
            category="POS",
            description="Custom grant transferred by the merge migration.",
        )
        RoleCapability.objects.create(role=User.Role.CASHIER, capability=custom)

        MIGRATION.merge_cashier_into_restaurants(apps, None)

        cashier.refresh_from_db()
        profile.refresh_from_db()
        workstation.refresh_from_db()
        template.refresh_from_db()
        assignment.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(cashier.role, User.Role.WAITER)
        self.assertEqual(profile.department, Workstation.Department.RESTAURANT)
        self.assertEqual(profile.department_ref.code, "RESTAURANT_BAR")
        self.assertEqual(workstation.department, Workstation.Department.RESTAURANT)
        self.assertEqual(template.department, Workstation.Department.RESTAURANT)
        self.assertEqual(assignment.department, Workstation.Department.RESTAURANT)
        self.assertEqual(session.cashier_id, cashier.pk)
        self.assertTrue(
            RoleCapability.objects.filter(role=User.Role.WAITER, capability=custom).exists()
        )
        self.assertTrue(
            RoleCapability.objects.filter(role=User.Role.CASHIER, capability=custom).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="CASHIER_MERGED_INTO_RESTAURANTS",
                object_type="accounts.user",
                object_id=str(cashier.pk),
            ).exists()
        )

        # Re-running the data operation does not create duplicate user migrations.
        before = AuditLog.objects.filter(
            action="CASHIER_MERGED_INTO_RESTAURANTS",
            object_type="accounts.user",
            object_id=str(cashier.pk),
        ).count()
        MIGRATION.merge_cashier_into_restaurants(apps, None)
        self.assertEqual(
            AuditLog.objects.filter(
                action="CASHIER_MERGED_INTO_RESTAURANTS",
                object_type="accounts.user",
                object_id=str(cashier.pk),
            ).count(),
            before,
        )

    def test_ambiguous_cashier_profile_is_preserved_for_manual_review(self):
        cashier = make_staff("migrate.ambiguous.cashier@staff.dev", role=User.Role.CASHIER)
        StaffProfile.objects.create(
            user=cashier,
            employee_code="MERGE-AMBIG-1",
            department=Workstation.Department.KITCHEN,
        )
        MIGRATION.merge_cashier_into_restaurants(apps, None)
        cashier.refresh_from_db()
        self.assertEqual(cashier.role, User.Role.CASHIER)
        self.assertTrue(
            AuditLog.objects.filter(
                action="CASHIER_MERGE_MANUAL_REVIEW",
                object_type="accounts.user",
                object_id=str(cashier.pk),
            ).exists()
        )

    def test_reverse_migration_restores_recorded_values_without_deleting_financial_rows(self):
        cashier, profile, workstation, template, assignment, session = self._cashier_fixture()
        custom = Capability.objects.create(
            code="cashier.reverse.custom",
            name="Reverse cashier task",
            category="POS",
            description="Custom grant removed only from WAITER on reverse.",
        )
        RoleCapability.objects.create(role=User.Role.CASHIER, capability=custom)

        MIGRATION.merge_cashier_into_restaurants(apps, None)
        self.assertTrue(
            RoleCapability.objects.filter(role=User.Role.WAITER, capability=custom).exists()
        )
        MIGRATION.unmerge_cashier_from_restaurants(apps, None)

        cashier.refresh_from_db()
        profile.refresh_from_db()
        workstation.refresh_from_db()
        template.refresh_from_db()
        assignment.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(cashier.role, User.Role.CASHIER)
        self.assertEqual(profile.department, "CASHIER")
        self.assertEqual(workstation.department, "CASHIER")
        self.assertEqual(template.department, "CASHIER")
        self.assertEqual(assignment.department, "CASHIER")
        self.assertEqual(session.cashier_id, cashier.pk)
        self.assertFalse(
            RoleCapability.objects.filter(role=User.Role.WAITER, capability=custom).exists()
        )
        self.assertTrue(
            RoleCapability.objects.filter(role=User.Role.CASHIER, capability=custom).exists()
        )
