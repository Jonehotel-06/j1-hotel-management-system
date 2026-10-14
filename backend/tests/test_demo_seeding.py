# tests/test_demo_seeding.py
"""Isolated demo-data seeding and simplified payroll checks.

Pins the safety properties the owner brief demands:
* demo seeding refuses production settings;
* re-runs are idempotent (no duplicate demo records);
* every synthetic record is DEMO-labelled and isolated;
* approved demo room prices are preserved (15,000 / 25,000 / 45,000);
* opening stock exists only as immutable OPENING movements on demo SKUs;
* payroll runs use simplified compensation with no government tax or pension
  calculation;
"""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import User
from apps.inventory.models import StockItem, StockMovement, StockRecipe, StockRecipeLine, Supplier
from apps.pos.models import MenuCategory, MenuItem
from apps.rooms.models import Room, RoomType
from apps.staff_operations.models import ShiftAssignment, ShiftTemplate, StaffProfile

from .base import BaseAPITestCase


class DemoSeedTests(BaseAPITestCase):
    def _seed(self):
        call_command("seed_demo", stdout=StringIO())
        out = StringIO()
        call_command("seed_demo_operations", stdout=StringIO())
        call_command("seed_demo_operations", stdout=out)  # second run
        self.assertIn("0 object(s) created", out.getvalue())
        return out

    def test_demo_seed_is_idempotent_and_labelled(self):
        self._seed()
        # Approved demo rates preserved exactly.
        prices = dict(RoomType.objects.values_list("name", "base_price"))
        self.assertEqual(prices["Standard Room"], Decimal("15000.00"))
        self.assertEqual(prices["Deluxe Room"], Decimal("25000.00"))
        self.assertEqual(prices["Executive Suite"], Decimal("45000.00"))
        # Six physical rooms per demo room type, no duplicates.
        for name in prices:
            self.assertEqual(Room.objects.filter(room_type__name=name).count(), 6)
        self.assertEqual(Room.objects.values("room_number").distinct().count(),
                         Room.objects.count())
        # Nigerian menu catalogue present, all demo-marked, none taxed on the bill.
        self.assertGreaterEqual(MenuCategory.objects.count(), 6)
        self.assertGreaterEqual(MenuItem.objects.count(), 60)
        self.assertTrue(all("DEMO" in (i.description or "") for i in MenuItem.objects.all()))
        self.assertTrue(all(i.tax_rate_percent == 0 for i in MenuItem.objects.all()))
        # Staff, suppliers, stock: synthetic and demo-labelled.
        self.assertEqual(StaffProfile.objects.count(), 16)
        self.assertTrue(all("DEMO" in p.employee_code for p in StaffProfile.objects.all()))
        self.assertEqual(Supplier.objects.count(), 5)
        # Recipes link menu items to stock components (demo BOM).
        self.assertEqual(StockRecipe.objects.count(), 6)
        self.assertEqual(StockRecipeLine.objects.count(), 10)
        self.assertTrue(all(r.reference.startswith("DEMO-RCP-") for r in StockRecipe.objects.all()))
        # Shift templates + one scheduled assignment per demo staff member.
        self.assertEqual(ShiftTemplate.objects.count(), 5)
        self.assertEqual(ShiftAssignment.objects.count(), 16)
        self.assertTrue(all(a.reference.startswith("DEMO-SHIFT-") for a in ShiftAssignment.objects.all()))
        self.assertEqual(StockItem.objects.count(), 18)
        movements = StockMovement.objects.all()
        self.assertEqual(movements.count(), 18)
        self.assertTrue(all(m.type == StockMovement.Type.OPENING for m in movements))
        self.assertTrue(all(m.source_key.startswith("demo-opening:") for m in movements))
        # Immutable append-only guard still applies to demo movements.
        movement = movements.first()
        with self.assertRaises(Exception):
            movement.save()
        with self.assertRaises(Exception):
            movement.delete()

    def test_seed_refuses_production_settings(self):
        with patch("apps.hotel.management.commands.seed_demo_operations.django_settings") as fake:
            fake.DJANGO_SETTINGS_MODULE = "config.settings.production"
            fake.ENVIRONMENT = "production"
            with self.assertRaises(CommandError):
                call_command("seed_demo_operations", stdout=StringIO())

    def test_no_demo_login_credentials_are_minted(self):
        self._seed()
        for user in User.objects.filter(email__endswith="@demo.j-one.test"):
            self.assertFalse(user.has_usable_password(),
                             "demo accounts must not carry usable passwords")


class SimplifiedPayrollRunTests(BaseAPITestCase):
    """End-to-end DRAFT payroll using simplified compensation only.

    No real employee is involved and no real payment is made — everything is
    synthetic and confined to the test DB.
    """

    def setUp(self):
        super().setUp()
        from apps.core.utils import hotel_today
        from apps.staff_operations.services.staff_operations_service import ensure_staff_profile

        self.today = hotel_today()
        self.creator = User.objects.create_user(
            email="seed.payroll.hr@staff.dev", password="Str0ng!Pass",
            first_name="Seed", last_name="HR", role=User.Role.HR_MANAGER,
        )
        self.reviewer = User.objects.create_user(
            email="seed.payroll.reviewer@staff.dev", password="Str0ng!Pass",
            first_name="Seed", last_name="Reviewer", role=User.Role.ACCOUNTS_MANAGER,
        )
        self.employee = User.objects.create_user(
            email="seed.payroll.employee@staff.dev", password="Str0ng!Pass",
            first_name="Seed", last_name="Employee", role=User.Role.HOUSEKEEPER,
        )
        profile, _ = ensure_staff_profile(staff=self.employee)
        profile.department = "Housekeeping"
        profile.employment_start = self.today
        profile.save(update_fields=["department", "employment_start", "updated_at"])

    def test_draft_period_uses_simplified_compensation_without_a_tax_id(self):
        self.auth(self.creator)
        self.assertEqual(
            self.client.get("/api/admin/staff-operations/payroll/statutory-rules/").status_code, 404,
        )
        self.assertEqual(
            self.client.get("/api/admin/staff-operations/payroll/tax-identities/").status_code, 404,
        )
        comp = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": self.employee.pk, "effective_from": self.today.isoformat(),
            "currency": "NGN", "basic_salary": "250000.00",
            "housing_allowance": "25000.00", "transport_allowance": "15000.00",
            "overtime_rate": "0.00",
        }, format="json")
        self.assertEqual(comp.status_code, 201, comp.content)
        self.assertNotIn("pension_applicable", comp.json()["data"])
        self.assertNotIn("minimum_wage_applicable", comp.json()["data"])

        period = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Housekeeping", "idempotency_key": "simplified-draft-001",
            "adjustments": [],
        }, format="json")
        self.assertIn(period.status_code, (200, 201), period.content)
        line = period.json()["data"]["lines"][0]
        self.assertEqual(Decimal(line["gross_pay"]), Decimal("290000.00"))
        self.assertEqual(Decimal(line["deductions_total"]), Decimal("0.00"))
        self.assertEqual(Decimal(line["net_pay"]), Decimal("290000.00"))
        self.assertNotIn("paye_tax", line)
        self.assertNotIn("employee_pension", line)
        self.assertNotIn("tax_snapshot", line)
        self.assertEqual(period.json()["data"]["status"], "DRAFT")

    def test_manual_deductions_cannot_smuggle_statutory_labels(self):
        self.auth(self.creator)
        self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": self.employee.pk, "effective_from": self.today.isoformat(),
            "currency": "NGN", "basic_salary": "60000.00", "overtime_rate": "0.00",
        }, format="json")
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Housekeeping", "idempotency_key": "simplified-guard-001",
            "adjustments": [{
                "staff_id": self.employee.pk, "overtime_hours": "0",
                "allowances": [], "bonus": "0.00",
                "deductions": [{"label": "PAYE", "amount": "5000.00"}],
            }],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
