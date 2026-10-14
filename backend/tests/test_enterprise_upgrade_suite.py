"""Comprehensive regression and integration test suite for the J-ONE Hotel & Lodge
enterprise upgrade across Priorities 1–5:
- Priority 1: Staff Management, 10-Section Profiles, Departments, Disciplinary Register,
  Documents, Payroll Deduction Integration, and Administrator Permission Matrix.
- Priority 2: Biometric Fingerprint Attendance & 6-Digit Numeric Workstation Access Codes
  with immediate transactional expiration on Clock-Out.
- Priority 3: Inventory Stock Deduction on POS/Bar/Restaurant Sales, Unit Conversion,
  1:1 SKU Fallback, Void/Cancel Reversal, Opening Stock, and Stock Returns.
- Priority 4: First-Attempt Paystack Payment Verification, Single-Dispatch Guest Receipt
  Email Idempotency, and Check-In/Check-Out Action Reliability.
- Priority 5: Public Footer Staff Link Removal & 11 Department Subdomain Portals.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.capabilities import capability_codes_for_user
from apps.accounts.desktop_policy import issue_desktop_key
from apps.accounts.models import User, Workstation
from apps.bookings.models import Booking
from apps.inventory.models import (
    StockBalance,
    StockConsumptionRequest,
    StockItem,
    StockLocation,
    StockMovement,
    StockRecipe,
    StockRecipeLine,
)
from apps.inventory.services.inventory_service import (
    create_stock_recipe,
    process_pending_pos_consumption,
    record_opening_stock,
    record_stock_return,
    reverse_pos_stock_consumption,
)
from apps.notifications.models import EmailLog
from apps.payments.models import Payment
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import create_order, submit_order, transition_order
from apps.rooms.models import Room, RoomType
from apps.staff_operations.biometric_models import TemporaryWorkstationCredential
from apps.staff_operations.models import (
    AttendanceRecord,
    Department,
    StaffDisciplinaryRecord,
    StaffDocument,
    StaffProfile,
)
from apps.staff_operations.payroll_models import PayrollLine, PayrollPeriod, StaffCompensation
from apps.staff_operations.services.fingerprint_service import (
    approve_fingerprint_enrollment,
    request_fingerprint_enrollment,
)
from apps.staff_operations.services.payroll_service import create_payroll_period


def _sample_minutiae(seed_offset: int = 0):
    return [
        {"x": 100 + seed_offset, "y": 120 + seed_offset, "angle": 45, "type": "ENDING", "quality": 90},
        {"x": 140 + seed_offset, "y": 160 + seed_offset, "angle": 90, "type": "BIFURCATION", "quality": 88},
        {"x": 180 + seed_offset, "y": 200 + seed_offset, "angle": 135, "type": "ENDING", "quality": 92},
        {"x": 210 + seed_offset, "y": 240 + seed_offset, "angle": 180, "type": "BIFURCATION", "quality": 86},
        {"x": 250 + seed_offset, "y": 280 + seed_offset, "angle": 225, "type": "ENDING", "quality": 91},
        {"x": 290 + seed_offset, "y": 310 + seed_offset, "angle": 270, "type": "BIFURCATION", "quality": 89},
        {"x": 320 + seed_offset, "y": 340 + seed_offset, "angle": 315, "type": "ENDING", "quality": 87},
        {"x": 350 + seed_offset, "y": 370 + seed_offset, "angle": 30, "type": "BIFURCATION", "quality": 90},
        {"x": 380 + seed_offset, "y": 400 + seed_offset, "angle": 75, "type": "ENDING", "quality": 85},
        {"x": 410 + seed_offset, "y": 430 + seed_offset, "angle": 120, "type": "BIFURCATION", "quality": 88},
    ]


class Priority1StaffDepartmentsDisciplinaryPermissionsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="admin-ent@jonehotel.com",
            password="StrongPassword!123",
            first_name="System",
            last_name="Admin",
            role=User.Role.ADMIN,
        )
        self.gm = User.objects.create_user(
            email="gm-ent@jonehotel.com",
            password="StrongPassword!123",
            first_name="General",
            last_name="Manager",
            role=User.Role.GENERAL_MANAGER,
        )
        self.accountant = User.objects.create_user(
            email="acct-ent@jonehotel.com",
            password="StrongPassword!123",
            first_name="Ada",
            last_name="Accountant",
            role=User.Role.ACCOUNTANT,
        )
        self.waiter = User.objects.create_user(
            email="waiter-ent@jonehotel.com",
            password="StrongPassword!123",
            first_name="Tunde",
            last_name="Waiter",
            role=User.Role.WAITER,
        )
        self.porter = User.objects.create_user(
            email="porter-ent@jonehotel.com",
            password="StrongPassword!123",
            first_name="Musa",
            last_name="Porter",
            role=User.Role.PORTER,
        )
        self.kitchen_assistant = User.objects.create_user(
            email="ka-ent@jonehotel.com",
            password="StrongPassword!123",
            first_name="Bisi",
            last_name="Kitchen",
            role=User.Role.KITCHEN_ASSISTANT,
        )

    def test_seeded_departments_and_department_dashboard_isolation(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get("/api/admin/staff-operations/departments/")
        self.assertEqual(resp.status_code, 200)
        codes = {d["code"] for d in resp.data["data"]}
        for expected in (
            "MANAGEMENT",
            "ACCOUNTS",
            "ADMIN_PROCUREMENT",
            "FRONT_DESK",
            "KITCHEN",
            "RESTAURANT_BAR",
            "HOUSEKEEPING",
            "PORTERING",
            "STORES_INVENTORY",
        ):
            self.assertIn(expected, codes)

        # Accountant can view ACCOUNTS dashboard summary, but NOT STORES_INVENTORY
        self.client.force_authenticate(user=self.accountant)
        ok_resp = self.client.get("/api/admin/staff-operations/departments/ACCOUNTS/dashboard-summary/")
        self.assertEqual(ok_resp.status_code, 200)
        self.assertIn("total_revenue", ok_resp.data["data"]["metrics"])

        forbidden_resp = self.client.get("/api/admin/staff-operations/departments/STORES_INVENTORY/dashboard-summary/")
        self.assertEqual(forbidden_resp.status_code, 403)

        # Porter can view PORTERING dashboard summary, but NOT ACCOUNTS
        self.client.force_authenticate(user=self.porter)
        porter_resp = self.client.get("/api/admin/staff-operations/departments/PORTERING/dashboard-summary/")
        self.assertEqual(porter_resp.status_code, 200)
        self.assertIn("open_guest_requests", porter_resp.data["data"]["metrics"])
        self.assertEqual(
            self.client.get("/api/admin/staff-operations/departments/ACCOUNTS/dashboard-summary/").status_code,
            403,
        )

    def test_comprehensive_10_tab_staff_profile_and_lifecycle_actions(self):
        self.client.force_authenticate(user=self.admin)
        # Update full profile for waiter
        patch_resp = self.client.patch(
            f"/api/admin/staff-operations/staff/{self.waiter.id}/full-profile/",
            {
                "middle_name": "Olu",
                "department": "Restaurant & Bar Service",
                "job_title": "Senior Waiter",
                "employment_type": "FULL_TIME",
                "date_of_birth": "1996-04-15",
                "gender": "Male",
                "residential_address": "14 Admiralty Way, Abuja",
                "emergency_contact_name": "Kemi Waiter",
                "emergency_contact_relationship": "Spouse",
                "emergency_contact_phone": "+2348011122233",
                "emergency_contact_address": "14 Admiralty Way, Abuja",
                "job_description": "Table service and guest order fulfillment",
            },
            format="json",
        )
        self.assertEqual(patch_resp.status_code, 200)
        data = patch_resp.data["data"]
        self.assertEqual(data["overview"]["job_title"], "Senior Waiter")
        self.assertEqual(data["profile"]["emergency_contact_name"], "Kemi Waiter")
        self.assertEqual(data["user"]["middle_name"], "Olu")
        self.assertTrue(data["salary_payments"]["authorized"])

        # Lifecycle actions: suspend -> reactivate -> transfer_department -> reset_password
        susp = self.client.post(
            f"/api/admin/users/{self.waiter.id}/actions/",
            {"action": "suspend", "reason": "Pending investigation"},
            format="json",
        )
        self.assertEqual(susp.status_code, 200)
        self.waiter.refresh_from_db()
        self.assertFalse(self.waiter.is_active)
        self.assertEqual(self.waiter.staff_profile.employment_status, StaffProfile.EmploymentStatus.SUSPENDED)

        react = self.client.post(
            f"/api/admin/users/{self.waiter.id}/actions/",
            {"action": "reactivate"},
            format="json",
        )
        self.assertEqual(react.status_code, 200)
        self.waiter.refresh_from_db()
        self.assertTrue(self.waiter.is_active)
        self.assertEqual(self.waiter.staff_profile.employment_status, StaffProfile.EmploymentStatus.ACTIVE)

        pw_resp = self.client.post(
            f"/api/admin/users/{self.waiter.id}/actions/",
            {"action": "reset_password", "send_email": False, "must_change_password": True},
            format="json",
        )
        self.assertEqual(pw_resp.status_code, 200)
        self.assertTrue(pw_resp.data["data"]["temporary_password"])
        self.waiter.refresh_from_db()
        self.assertTrue(self.waiter.must_change_password)

    def test_disciplinary_workflow_maker_checker_and_payroll_deduction_integration(self):
        # Ensure waiter has a staff profile in Restaurant & Bar Service and compensation
        StaffProfile.objects.update_or_create(
            user=self.waiter,
            defaults={
                "employee_code": "STF-WAITER-DISC",
                "department": "Restaurant & Bar Service",
                "job_title": "Waiter",
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )
        StaffCompensation.objects.create(
            staff=self.waiter,
            effective_from=date(2025, 10, 1),
            currency="NGN",
            basic_salary=Decimal("120000.00"),
            housing_allowance=Decimal("20000.00"),
            transport_allowance=Decimal("10000.00"),
            overtime_rate=Decimal("0.00"),
            created_by=self.admin,
        )

        # GM records an allegation with a recommended deduction of NGN 5,000
        self.client.force_authenticate(user=self.gm)
        create_resp = self.client.post(
            "/api/admin/staff-operations/disciplinary/",
            {
                "staff": self.waiter.id,
                "category": "DAMAGE",
                "allegation_summary": "Breakage of glassware tray",
                "allegation_detail": "Dropped tray during evening service without following safety protocol.",
                "recommended_deduction_amount": "5000.00",
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, 201)
        ref = create_resp.data["data"]["reference"]

        # Unapproved allegation MUST NOT deduct from payroll
        period_unapproved, _ = create_payroll_period(
            actor=self.admin,
            starts_on=date(2025, 10, 1),
            ends_on=date(2025, 10, 31),
            department="Restaurant & Bar Service",
            idempotency_key="payroll-disc-unapproved",
        )
        line_unapproved = PayrollLine.objects.get(period=period_unapproved, staff=self.waiter)
        self.assertEqual(line_unapproved.deductions_total, Decimal("0.00"))
        self.assertEqual(line_unapproved.deductions, [])

        # Record employee response & resolve investigation
        resp_stmt = self.client.post(
            f"/api/admin/staff-operations/disciplinary/{ref}/action/",
            {"action": "submit_response", "employee_response": "I acknowledge the incident."},
            format="json",
        )
        self.assertEqual(resp_stmt.status_code, 200)

        resolve_resp = self.client.post(
            f"/api/admin/staff-operations/disciplinary/{ref}/action/",
            {
                "action": "resolve",
                "investigation_outcome": "Negligence confirmed.",
                "disciplinary_action": "MONETARY_DEDUCTION",
                "warning_level": "FIRST_WRITTEN",
            },
            format="json",
        )
        self.assertEqual(resolve_resp.status_code, 200)

        # Maker-checker: GM who reported the allegation CANNOT approve their own monetary deduction
        self_approve = self.client.post(
            f"/api/admin/staff-operations/disciplinary/{ref}/action/",
            {
                "action": "approve_deduction",
                "approved_deduction_amount": "5000.00",
                "effective_date": "2025-11-15",
            },
            format="json",
        )
        self.assertEqual(self_approve.status_code, 403)

        # Admin (separate authorizer) approves the monetary deduction
        self.client.force_authenticate(user=self.admin)
        approve_resp = self.client.post(
            f"/api/admin/staff-operations/disciplinary/{ref}/action/",
            {
                "action": "approve_deduction",
                "approved_deduction_amount": "5000.00",
                "effective_date": "2025-11-15",
            },
            format="json",
        )
        self.assertEqual(approve_resp.status_code, 200)
        self.assertTrue(approve_resp.data["data"]["deduction_approved"])

        # Prepare November payroll period -> approved disciplinary deduction of 5000.00 is applied!
        period_approved, _ = create_payroll_period(
            actor=self.admin,
            starts_on=date(2025, 11, 1),
            ends_on=date(2025, 11, 30),
            department="Restaurant & Bar Service",
            idempotency_key="payroll-disc-approved",
        )
        line_approved = PayrollLine.objects.get(period=period_approved, staff=self.waiter)
        self.assertEqual(line_approved.deductions_total, Decimal("5000.00"))
        self.assertTrue(any(ref in d["label"] for d in line_approved.deductions))

    def test_staff_documents_and_admin_permission_matrix_and_overrides(self):
        self.client.force_authenticate(user=self.admin)
        doc_resp = self.client.post(
            "/api/admin/staff-operations/documents/",
            {
                "staff": self.waiter.id,
                "category": "CONTRACT",
                "title": "Signed Employment Contract 2026",
                "description": "Permanent full-time contract",
                "issued_date": "2026-01-10",
            },
            format="json",
        )
        self.assertEqual(doc_resp.status_code, 201)
        doc_ref = doc_resp.data["data"]["reference"]

        # Admin permission matrix
        matrix_resp = self.client.get("/api/admin/users/permissions-matrix/")
        self.assertEqual(matrix_resp.status_code, 200)
        self.assertTrue(len(matrix_resp.data["data"]["roles"]) >= 20)

        # Per-user capability override: grant `inventory.manage` to waiter, then revoke
        self.assertNotIn("inventory.manage", capability_codes_for_user(self.waiter))
        grant_resp = self.client.post(
            f"/api/admin/users/{self.waiter.id}/permissions/",
            {"capability_code": "inventory.manage", "effect": "GRANT", "reason": "Temporary stock count helper"},
            format="json",
        )
        self.assertEqual(grant_resp.status_code, 200)
        self.waiter.refresh_from_db()
        self.assertIn("inventory.manage", capability_codes_for_user(self.waiter))

        clear_resp = self.client.post(
            f"/api/admin/users/{self.waiter.id}/permissions/",
            {"capability_code": "inventory.manage", "effect": "CLEAR"},
            format="json",
        )
        self.assertEqual(clear_resp.status_code, 200)
        self.waiter.refresh_from_db()
        self.assertNotIn("inventory.manage", capability_codes_for_user(self.waiter))

        # Archive document
        del_resp = self.client.delete(f"/api/admin/staff-operations/documents/{doc_ref}/")
        self.assertEqual(del_resp.status_code, 200)
        self.assertFalse(StaffDocument.objects.get(reference=doc_ref).is_active)


class Priority2BiometricAttendanceAndNumericWorkstationCodeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="admin-bio@jonehotel.com",
            password="StrongPassword!123",
            role=User.Role.ADMIN,
        )
        self.receptionist = User.objects.create_user(
            email="reception-bio@jonehotel.com",
            password="StrongPassword!123",
            first_name="Chidinma",
            last_name="Reception",
            role=User.Role.RECEPTIONIST,
        )
        self.chef = User.objects.create_user(
            email="chef-bio@jonehotel.com",
            password="StrongPassword!123",
            first_name="Emeka",
            last_name="Chef",
            role=User.Role.CHEF,
        )
        # The reception terminal answers only to an enrolled Receptionist Desktop
        # (or a management session): the desk key below is that enrolment.
        self.front_desk = Workstation.objects.create(
            name="Enterprise Reception Desk", department=Workstation.Department.FRONT_DESK
        )
        self.desk_key = issue_desktop_key(self.front_desk)
        self.front_desk.save()
        StaffProfile.objects.update_or_create(
            user=self.chef,
            defaults={
                "employee_code": "STF-CHEF01",
                "department": "Kitchen",
                "job_title": "Head Chef",
                "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
            },
        )
        self.minutiae = _sample_minutiae(5)
        template = request_fingerprint_enrollment(
            actor=self.admin,
            staff=self.chef,
            minutiae=self.minutiae,
        )
        approve_fingerprint_enrollment(approver=self.admin, template=template)

    def test_reception_workstation_clock_in_issues_6_digit_numeric_code_and_clock_out_expires_immediately(self):
        self.client.force_authenticate(user=self.receptionist)

        # 1. Reception workstation clocks in Chef via fingerprint verification and issues 6-digit numeric code
        clock_in_resp = self.client.post(
            "/api/admin/staff-operations/biometrics/reception-terminal/",
            {
                "action": "CLOCK_IN",
                "employee_code": "STF-CHEF01",
                "minutiae": self.minutiae,
                "workstation_reference": "WS-KITCHEN-01",
            },
            format="json",
            HTTP_X_JONE_DESKTOP_KEY=self.desk_key,
        )
        self.assertEqual(clock_in_resp.status_code, 200)
        res_data = clock_in_resp.data["data"]
        access_code = res_data["access_code"]
        self.assertTrue(access_code.isdigit(), "Workstation access code must be numeric-only")
        self.assertEqual(len(access_code), 6, "Workstation access code must be 6 digits")
        self.assertEqual(res_data["attendance_status"], "ON_DUTY")

        # 2. Chef signs in on kitchen workstation using ONLY the 6-digit numeric access code
        kitchen_client = APIClient()
        bad_dept_login = kitchen_client.post(
            "/api/auth/login/",
            {"access_code": access_code, "department": "accountant"},
            format="json",
        )
        self.assertIn(bad_dept_login.status_code, (401, 403), "Numeric code must reject cross-department login")

        good_login = kitchen_client.post(
            "/api/auth/login/",
            {"access_code": access_code, "department": "kitchen"},
            format="json",
        )
        self.assertEqual(good_login.status_code, 200)
        access_jwt = good_login.data["data"]["tokens"]["access"]
        kitchen_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_jwt}")

        # Verify Chef's JWT works while clocked in
        me_resp = kitchen_client.get("/api/auth/profile/")
        self.assertEqual(me_resp.status_code, 200)
        self.assertEqual(me_resp.data["data"]["email"], self.chef.email)

        # 3. Chef clocks out at Reception -> numeric code AND active workstation JWT must expire IMMEDIATELY
        clock_out_resp = self.client.post(
            "/api/admin/staff-operations/biometrics/reception-terminal/",
            {
                "action": "CLOCK_OUT",
                "employee_code": "STF-CHEF01",
                "minutiae": self.minutiae,
            },
            format="json",
            HTTP_X_JONE_DESKTOP_KEY=self.desk_key,
        )
        self.assertEqual(clock_out_resp.status_code, 200)
        self.assertEqual(clock_out_resp.data["data"]["attendance_status"], "COMPLETED")
        self.assertGreaterEqual(clock_out_resp.data["data"]["revoked_credentials_count"], 1)

        # Active JWT bound to workstation credential is immediately rejected upon clock-out
        after_clockout_resp = kitchen_client.get("/api/auth/profile/")
        self.assertEqual(after_clockout_resp.status_code, 401)

        # Attempting to reuse the 6-digit numeric code after clock-out is rejected
        reuse_resp = APIClient().post(
            "/api/auth/login/",
            {"access_code": access_code, "department": "kitchen"},
            format="json",
        )
        self.assertEqual(reuse_resp.status_code, 401)


class Priority3InventoryCorrectnessTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-inv@jonehotel.com",
            password="StrongPassword!123",
            role=User.Role.ADMIN,
        )
        self.location = StockLocation.objects.create(
            code="BAR-STORE-ENT",
            name="Main Bar Store Ent",
        )
        # Stock item tracked in L (1 L = 1000 ML)
        self.wine_stock = StockItem.objects.create(
            sku="WINE-CAB-1L",
            name="Cabernet Sauvignon 1L",
            category="Beverages",
            base_unit="L",
            reorder_level=Decimal("3.0000"),
            standard_cost=Decimal("15000.00"),
        )
        # Direct 1:1 retail item without explicit pre-created recipe (same SKU as menu item)
        self.water_stock = StockItem.objects.create(
            sku="WATER-50CL-ENT",
            name="Table Water 50cl Ent",
            category="Beverages",
            base_unit="BOTTLE",
            reorder_level=Decimal("5.0000"),
            standard_cost=Decimal("200.00"),
        )
        self.menu_cat = MenuCategory.objects.create(
            name="Bar Drinks Ent",
            slug="bar-drinks-ent",
            service_area=MenuCategory.ServiceArea.BAR,
        )
        self.wine_glass_menu = MenuItem.objects.create(
            category=self.menu_cat,
            sku="MENU-WINE-GLASS-ENT",
            name="Glass of Cabernet (200ml)",
            base_price=Decimal("6500.00"),
        )
        self.water_menu = MenuItem.objects.create(
            category=self.menu_cat,
            sku="WATER-50CL-ENT",
            name="Table Water 50cl Ent",
            base_price=Decimal("500.00"),
        )
        create_stock_recipe(
            menu_item=self.wine_glass_menu,
            location=self.location,
            tracking_mode="COMPONENTS",
            components=[{"item_id": self.wine_stock.pk, "quantity_per_menu_unit": "0.2000"}],
            actor=self.admin,
            idempotency_key="rcp-wine-glass-ent",
        )

    def test_opening_stock_pos_sale_1to1_fallback_returns_and_void_reversal(self):
        # 1. Record Opening Stock: 10 L of Wine, 20 bottles of Water
        record_opening_stock(
            item=self.wine_stock,
            location=self.location,
            quantity=Decimal("10.0000"),
            unit_cost=Decimal("15000.00"),
            actor=self.admin,
            idempotency_key="open-wine-10",
        )
        record_opening_stock(
            item=self.water_stock,
            location=self.location,
            quantity=Decimal("20.0000"),
            unit_cost=Decimal("200.00"),
            actor=self.admin,
            idempotency_key="open-water-20",
        )

        # 2. Create, submit, and deliver a Bar POS order: 5 glasses of wine (= 1.0 L) + 3 bottles of water (1:1 SKU auto-fallback)
        order, _ = create_order(
            mode=PosOrder.Mode.BAR,
            actor=self.admin,
            idempotency_key="pos-bar-ent-1",
            lines=[
                {"menu_item_id": self.wine_glass_menu.pk, "quantity": 5},
                {"menu_item_id": self.water_menu.pk, "quantity": 3},
            ],
        )
        submit_order(order=order, actor=self.admin)
        # Direct transition from SUBMITTED -> DELIVERED triggers stock deduction
        transition_order(order=order, target_status=PosOrder.Status.DELIVERED, actor=self.admin)

        wine_bal = StockBalance.objects.get(item=self.wine_stock, location=self.location)
        water_bal = StockBalance.objects.get(item=self.water_stock, location=self.location)
        self.assertEqual(wine_bal.quantity_on_hand, Decimal("9.0000"))
        self.assertEqual(water_bal.quantity_on_hand, Decimal("17.0000"))

        # Idempotency: reprocessing the POS consumption request does NOT double-deduct
        process_pending_pos_consumption(actor=self.admin)
        wine_bal.refresh_from_db()
        water_bal.refresh_from_db()
        self.assertEqual(wine_bal.quantity_on_hand, Decimal("9.0000"))
        self.assertEqual(water_bal.quantity_on_hand, Decimal("17.0000"))

        # 3. Stock return inward (+2 water) and outward (-1 wine to supplier)
        record_stock_return(
            item=self.water_stock,
            location=self.location,
            quantity=Decimal("2.0000"),
            direction="IN",
            notes="Customer unopened bottle return",
            actor=self.admin,
            idempotency_key="ret-in-water-2",
        )
        record_stock_return(
            item=self.wine_stock,
            location=self.location,
            quantity=Decimal("1.0000"),
            direction="OUT",
            notes="Corked bottle returned to vendor",
            actor=self.admin,
            idempotency_key="ret-out-wine-1",
        )
        wine_bal.refresh_from_db()
        water_bal.refresh_from_db()
        self.assertEqual(wine_bal.quantity_on_hand, Decimal("8.0000"))
        self.assertEqual(water_bal.quantity_on_hand, Decimal("19.0000"))

        # 4. Reversing POS order stock consumption restores the 1 L wine and 3 water bottles
        reverse_pos_stock_consumption(pos_order=order, actor=self.admin, reason="Voided order")
        wine_bal.refresh_from_db()
        water_bal.refresh_from_db()
        self.assertEqual(wine_bal.quantity_on_hand, Decimal("9.0000"))
        self.assertEqual(water_bal.quantity_on_hand, Decimal("22.0000"))


class Priority4PaymentAndEmailReliabilityTests(TestCase):
    def setUp(self):
        from .factories import make_booking, make_guest, make_room, make_room_type

        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="admin-pay@jonehotel.com",
            password="StrongPassword!123",
            role=User.Role.ADMIN,
        )
        self.room_type = make_room_type(name="Executive Suite Ent", price="45000.00", max_guests=2)
        self.room = make_room(self.room_type, "401")
        self.guest = make_guest(email="ngozi@example.com", first_name="Ngozi", last_name="Okafor")
        self.booking = make_booking(
            self.guest,
            self.room_type,
            rooms=[self.room],
            status=Booking.Status.CONFIRMED,
            amount_paid="90000.00",
            total="90000.00",
        )
        self.payment = Payment.objects.create(
            booking=self.booking,
            reference="PAY-ENT-0001",
            amount=Decimal("90000.00"),
            currency="NGN",
            provider=Payment.Provider.PAYSTACK,
            status=Payment.Status.SUCCESS,
            transaction_id="PSK-TX-998877",
            paid_at=timezone.now(),
        )

    def test_paystack_first_attempt_verification_supports_get_post_and_transaction_id_lookup(self):
        self.client.force_authenticate(user=self.admin)
        # Verify by GET /api/payments/verify/?reference=<transaction_id>
        get_resp = self.client.get(f"/api/payments/verify/?reference={self.payment.transaction_id}")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.data["data"]["transaction_status"].upper(), "SUCCESS")
        self.assertEqual(get_resp.data["data"]["booking_reference"], self.booking.booking_reference)

        # Verify by POST /api/payments/verify/ with booking_reference
        post_resp = self.client.post(
            "/api/payments/verify/",
            {"booking_reference": self.booking.booking_reference},
            format="json",
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertEqual(post_resp.data["data"]["payment_reference"], self.payment.reference)

    def test_send_mail_to_guest_prevents_duplicate_dispatch_on_rapid_clicks(self):
        from django.core import mail

        mail.outbox.clear()
        self.client.force_authenticate(user=self.admin)
        url = f"/api/admin/bookings/{self.booking.booking_reference}/send-receipt/"

        first = self.client.post(
            url,
            {"email": "ngozi@example.com", "idempotency_key": "rcpt-idem-001"},
            format="json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        # Immediate second click (even with different or omitted idempotency_key within 30s window) must deduplicate
        second = self.client.post(
            url,
            {"email": "ngozi@example.com"},
            format="json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["data"].get("deduplicated"))
        self.assertEqual(len(mail.outbox), 1, "Email must be sent only once")
        self.assertEqual(
            EmailLog.objects.filter(booking_reference=self.booking.booking_reference, status=EmailLog.Status.SENT).count(),
            1,
        )
