"""End-to-end verification of the 11-department / 11-staff-role consolidation."""
from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection

from apps.accounts.capabilities import capability_codes_for_user, is_maintenance_worker
from apps.accounts.departments import (
    ASSIGNABLE_ROLES,
    DEPARTMENT_ROLES,
    GLOBAL_ROLES,
    RETIRED_DEPARTMENTAL_ROLES,
    canonical_role,
    department_for_role,
)
from apps.accounts.models import Capability, RoleCapability, User, UserCapabilityOverride, Workstation
from apps.audit.models import AuditLog
from apps.inventory.models import StockDepartment
from apps.maintenance.models import MaintenanceWorkOrder
from apps.maintenance.services import work_order_service
from apps.rooms.models import Room
from apps.staff_operations.models import Department, ShiftAssignment, ShiftTemplate, StaffProfile

from .base import BaseAPITestCase
from .factories import make_room, make_room_type, make_staff

APPROVED_11_LABELS = [
    "General Manager",
    "Accountant",
    "Admin Manager/Procurement",
    "Chefs",
    "Kitchen Assistant",
    "Waiters/Waitresses",
    "Porters",
    "Receptionist",
    "Housekeepers",
    "Store Keeper",
    "Security",
]


class ApprovedElevenDepartmentsAndRolesTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_staff("cons-admin@staff.dev", role=User.Role.ADMIN)

    def test_exact_eleven_approved_departments_and_eleven_staff_roles(self):
        operational_ws_choices = [
            (code, label)
            for code, label in Workstation.Department.choices
            if code != Workstation.Department.SHARED
        ]
        self.assertEqual([label for _code, label in operational_ws_choices], APPROVED_11_LABELS)
        self.assertEqual(len(DEPARTMENT_ROLES), 11)
        self.assertEqual(GLOBAL_ROLES, frozenset({User.Role.ADMIN}))
        self.assertEqual(
            ASSIGNABLE_ROLES,
            frozenset(DEPARTMENT_ROLES.values()) | {User.Role.ADMIN, User.Role.GUEST},
        )
        role_labels = [User.Role(role_code).label for role_code in DEPARTMENT_ROLES.values()]
        self.assertEqual(role_labels, APPROVED_11_LABELS)

        # StockDepartment excludes retired BAR, MAINTENANCE, and HR staff departments.
        for retired_dept in ("BAR", "MAINTENANCE", "HR"):
            self.assertNotIn(retired_dept, StockDepartment.values)
        self.assertIn("KITCHEN_ASSISTANT", StockDepartment.values)
        self.assertIn("SECURITY", StockDepartment.values)

    def test_department_registry_and_permission_matrix_expose_only_approved_eleven(self):
        self.auth(self.admin)
        dept_resp = self.client.get("/api/admin/staff-operations/departments/")
        self.assertEqual(dept_resp.status_code, 200, dept_resp.content)
        dept_names = [row["name"] for row in dept_resp.json()["data"]]
        self.assertEqual(sorted(dept_names), sorted(APPROVED_11_LABELS))
        for row in dept_resp.json()["data"]:
            self.assertEqual(len(row["supported_roles"]), 1)
            self.assertIn(row["supported_roles"][0], DEPARTMENT_ROLES.values())

        matrix_resp = self.client.get("/api/admin/users/permissions-matrix/")
        self.assertEqual(matrix_resp.status_code, 200, matrix_resp.content)
        matrix_roles = [row["role"] for row in matrix_resp.json()["data"]["roles"]]
        self.assertEqual(matrix_roles, [User.Role.ADMIN, *DEPARTMENT_ROLES.values()])

        # Security and Kitchen Assistant have live, isolated dashboard summaries.
        sec_user = make_staff("cons-security@staff.dev", role=User.Role.SECURITY)
        ka_user = make_staff("cons-ka@staff.dev", role=User.Role.KITCHEN_ASSISTANT)

        self.auth(sec_user)
        sec_sum = self.client.get("/api/admin/staff-operations/departments/SECURITY/dashboard-summary/")
        self.assertEqual(sec_sum.status_code, 200, sec_sum.content)
        self.assertEqual(sec_sum.json()["data"]["department"]["name"], "Security")
        self.assertEqual(
            self.client.get("/api/admin/staff-operations/departments/ACCOUNTS/dashboard-summary/").status_code,
            403,
        )

        self.auth(ka_user)
        ka_sum = self.client.get("/api/admin/staff-operations/departments/KITCHEN_ASSISTANT/dashboard-summary/")
        self.assertEqual(ka_sum.status_code, 200, ka_sum.content)
        self.assertEqual(ka_sum.json()["data"]["department"]["name"], "Kitchen Assistant")
        self.assertIn("pending_tickets", ka_sum.json()["data"]["metrics"])

        # Retired standalone department summaries return 404.
        self.auth(self.admin)
        for retired_slug in ("CASHIER", "BAR", "MAINTENANCE", "HR"):
            resp = self.client.get(f"/api/admin/staff-operations/departments/{retired_slug}/dashboard-summary/")
            self.assertEqual(resp.status_code, 404, f"{retired_slug}: {resp.content}")

    def test_all_retired_roles_and_departments_are_refused_on_write_endpoints(self):
        self.auth(self.admin)
        for idx, retired_role in enumerate(sorted(RETIRED_DEPARTMENTAL_ROLES)):
            resp = self.client.post(
                "/api/admin/users/",
                {
                    "email": f"retired-{idx}@staff.dev",
                    "first_name": "Retired",
                    "last_name": "Role",
                    "role": retired_role,
                    "password": "Str0ng!PassWord99",
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 400, f"{retired_role}: {resp.content}")
            self.assertIn("retired departmental title", resp.content.decode())

        for retired_dept, role_code in (
            ("BAR", User.Role.WAITER),
            ("MAINTENANCE", User.Role.HOUSEKEEPING),
            ("HR", User.Role.PROCUREMENT_OFFICER),
            ("CASHIER", User.Role.WAITER),
        ):
            resp = self.client.post(
                "/api/admin/users/",
                {
                    "email": f"dept-{retired_dept.lower()}@staff.dev",
                    "first_name": "Retired",
                    "last_name": "Dept",
                    "role": role_code,
                    "department": retired_dept,
                    "password": "Str0ng!PassWord99",
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 400, f"{retired_dept}: {resp.content}")

            dept_create = self.client.post(
                "/api/admin/staff-operations/departments/",
                {"code": retired_dept, "name": retired_dept.title(), "slug": retired_dept.lower()},
                format="json",
            )
            self.assertEqual(dept_create.status_code, 400, f"{retired_dept}: {dept_create.content}")

    def test_housekeepers_work_maintenance_orders_while_room_maintenance_status_stays_intact(self):
        room_type = make_room_type(name="Consolidation Room Type", price="30000.00")
        room = make_room(room_type, "509")
        receptionist = make_staff("cons-rec@staff.dev", role=User.Role.RECEPTIONIST)
        housekeeper = make_staff("cons-hk@staff.dev", role=User.Role.HOUSEKEEPING)

        self.assertTrue(is_maintenance_worker(housekeeper))
        self.assertIn("maintenance.work_order.manage", capability_codes_for_user(housekeeper))

        order, _ = work_order_service.create_work_order(
            room=room,
            category=MaintenanceWorkOrder.Category.ELECTRICAL,
            priority=MaintenanceWorkOrder.Priority.HIGH,
            summary="Replace bedside socket",
            actor=receptionist,
        )
        work_order_service.start_room_outage(
            work_order=order,
            actor=receptionist,
            outage_status=Room.Status.MAINTENANCE,
            reason="Socket repair",
        )
        room.refresh_from_db()
        self.assertEqual(room.status, Room.Status.MAINTENANCE)

        claimed = work_order_service.claim_work_order(work_order=order, actor=housekeeper)
        self.assertEqual(claimed.assigned_to_id, housekeeper.pk)
        progressed = work_order_service.transition_work_order(
            work_order=claimed,
            target_status=MaintenanceWorkOrder.Status.IN_PROGRESS,
            actor=housekeeper,
            note="Working on bedside socket.",
        )
        self.assertEqual(progressed.status, MaintenanceWorkOrder.Status.IN_PROGRESS)

    def test_migration_0030_is_idempotent_auditable_and_reversible(self):
        mig = import_module("apps.accounts.migrations.0030_consolidate_approved_departments_and_roles")

        hr_mgr = make_staff("mig-hrmgr@staff.dev", role=User.Role.HR_MANAGER)
        hr_prof = StaffProfile.objects.create(user=hr_mgr, employee_code="EMP-HR-01", department="HR")

        maint_tech = make_staff("mig-maint@staff.dev", role=User.Role.MAINTENANCE_TECHNICIAN)
        maint_prof = StaffProfile.objects.create(user=maint_tech, employee_code="EMP-MNT-01", department="MAINTENANCE")

        bartender = make_staff("mig-bar@staff.dev", role=User.Role.BARTENDER)
        bar_prof = StaffProfile.objects.create(user=bartender, employee_code="EMP-BAR-01", department="BAR")

        # Ambiguous account: BARTENDER whose persisted profile says ACCOUNTS -> must NOT be guessed at.
        conflict = make_staff("mig-conflict@staff.dev", role=User.Role.BARTENDER)
        conflict_prof = StaffProfile.objects.create(user=conflict, employee_code="EMP-CNF-01", department="ACCOUNTS")

        ws = Workstation.objects.create(name="Lounge Bar Till", department="BAR")
        tmpl = ShiftTemplate.objects.create(
            code="MIG-BAR-SHIFT", name="Bar Evening", department="BAR", start_time="16:00", end_time="23:00"
        )

        fake_editor = type("SchemaEditorStub", (), {"connection": connection})()
        mig.consolidate_approved_departments_and_roles(django_apps, fake_editor)

        hr_mgr.refresh_from_db()
        hr_prof.refresh_from_db()
        self.assertEqual(hr_mgr.role, User.Role.PROCUREMENT_OFFICER)
        self.assertEqual(hr_prof.employee_code, "EMP-HR-01")
        self.assertEqual(hr_prof.department, Workstation.Department.PROCUREMENT)
        # HR_MANAGER previously held leave.approve; migration preserves it via explicit override.
        self.assertIn("leave.approve", capability_codes_for_user(hr_mgr))
        self.assertTrue(
            UserCapabilityOverride.objects.filter(
                user=hr_mgr, capability__code="leave.approve", effect="GRANT"
            ).exists()
        )

        maint_tech.refresh_from_db()
        maint_prof.refresh_from_db()
        self.assertEqual(maint_tech.role, User.Role.HOUSEKEEPING)
        self.assertEqual(maint_prof.department, Workstation.Department.HOUSEKEEPING)

        bartender.refresh_from_db()
        bar_prof.refresh_from_db()
        self.assertEqual(bartender.role, User.Role.WAITER)
        self.assertEqual(bar_prof.department, Workstation.Department.RESTAURANT)

        # Conflicting account left untouched and flagged for manual review.
        conflict.refresh_from_db()
        conflict_prof.refresh_from_db()
        self.assertEqual(conflict.role, User.Role.BARTENDER)
        self.assertEqual(conflict_prof.department, "ACCOUNTS")
        self.assertTrue(
            AuditLog.objects.filter(
                action="DEPARTMENT_CONSOLIDATION_MANUAL_REVIEW", object_id=str(conflict.pk)
            ).exists()
        )

        ws.refresh_from_db()
        tmpl.refresh_from_db()
        self.assertEqual(ws.department, Workstation.Department.RESTAURANT)
        self.assertEqual(tmpl.department, Workstation.Department.RESTAURANT)

        # Reverse the migration and verify every row returns to its exact original state.
        mig.unconsolidate_approved_departments_and_roles(django_apps, fake_editor)

        hr_mgr.refresh_from_db()
        hr_prof.refresh_from_db()
        maint_tech.refresh_from_db()
        maint_prof.refresh_from_db()
        bartender.refresh_from_db()
        bar_prof.refresh_from_db()
        ws.refresh_from_db()
        tmpl.refresh_from_db()

        self.assertEqual(hr_mgr.role, User.Role.HR_MANAGER)
        self.assertEqual(hr_prof.department, "HR")
        self.assertEqual(maint_tech.role, User.Role.MAINTENANCE_TECHNICIAN)
        self.assertEqual(maint_prof.department, "MAINTENANCE")
        self.assertEqual(bartender.role, User.Role.BARTENDER)
        self.assertEqual(bar_prof.department, "BAR")
        self.assertEqual(ws.department, "BAR")
        self.assertEqual(tmpl.department, "BAR")
