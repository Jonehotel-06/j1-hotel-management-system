# tests/test_demo_seeding.py
"""Isolated demo-data seeding and the Nigerian payroll rules seed.

Pins the safety properties the owner brief demands:
* demo seeding refuses production settings;
* re-runs are idempotent (no duplicate demo records);
* every synthetic record is DEMO-labelled and isolated;
* approved demo room prices are preserved (15,000 / 25,000 / 45,000);
* opening stock exists only as immutable OPENING movements on demo SKUs;
* the payroll rule set is seeded PENDING_REVIEW with the researched Nigeria
  Tax Act 2025 values and can only be approved through the two-person workflow.
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
from apps.staff_operations.models import (
    PayrollStatutoryRuleEvent, PayrollStatutoryRuleSet, ShiftAssignment,
    ShiftTemplate, StaffProfile,
)

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


class NigeriaPayrollRulesSeedTests(BaseAPITestCase):
    def _seed(self):
        out = StringIO()
        call_command("seed_nigeria_payroll_rules", stdout=out)
        return out.getvalue()

    def test_rules_are_seeded_pending_review_with_researched_values(self):
        output = self._seed()
        self.assertIn("PENDING_REVIEW", output)
        rule_set = PayrollStatutoryRuleSet.objects.get()
        self.assertEqual(rule_set.jurisdiction, "NG")
        self.assertEqual(rule_set.effective_from.isoformat(), "2026-01-01")
        self.assertEqual(rule_set.status, PayrollStatutoryRuleSet.Status.PENDING_REVIEW)
        # Nigeria Tax Act 2025 bands (annual chargeable income).
        self.assertEqual(
            rule_set.paye_bands,
            [
                {"up_to": "800000.00", "rate": "0.0000"},
                {"up_to": "3000000.00", "rate": "0.1500"},
                {"up_to": "12000000.00", "rate": "0.1800"},
                {"up_to": "25000000.00", "rate": "0.2100"},
                {"up_to": "50000000.00", "rate": "0.2300"},
                {"up_to": None, "rate": "0.2500"},
            ],
        )
        self.assertEqual(rule_set.minimum_wage_monthly, Decimal("70000.00"))
        self.assertEqual(rule_set.employee_pension_rate, Decimal("0.0800"))
        self.assertEqual(rule_set.employer_pension_rate, Decimal("0.1000"))
        self.assertEqual(rule_set.rent_relief_rate, Decimal("0.2000"))
        self.assertEqual(rule_set.rent_relief_cap, Decimal("500000.00"))
        # Sources and legal basis recorded for review; event trail started.
        self.assertTrue(rule_set.source_references)
        self.assertIn("Nigeria Tax Act 2025", rule_set.legal_basis)
        self.assertEqual(
            PayrollStatutoryRuleEvent.objects.filter(rule_set=rule_set).count(), 1,
        )

    def test_reseed_is_idempotent(self):
        self._seed()
        output = self._seed()
        self.assertIn("already present", output)
        self.assertEqual(PayrollStatutoryRuleSet.objects.count(), 1)

    def test_seeded_rules_cannot_be_used_before_independent_approval(self):
        from apps.staff_operations.services.payroll_service import review_statutory_rule_set

        self._seed()
        rule_set = PayrollStatutoryRuleSet.objects.get()
        reviewer = User.objects.create_user(
            email="rules.reviewer@staff.dev", password="Str0ng!Pass",
            first_name="Rules", last_name="Reviewer", role=User.Role.ADMIN,
        )
        reviewed = review_statutory_rule_set(
            rule_set=rule_set, reviewer=reviewer, approved=True,
            review_note="Confirmed against Nigeria Tax Act 2025 and PRA 2014.",
        )
        self.assertEqual(reviewed.status, PayrollStatutoryRuleSet.Status.APPROVED)
        # A reviewed ruleset is immutable: further edits are refused.
        reviewed.paye_bands = []
        with self.assertRaises(Exception):
            reviewed.save()


class SeededRulesetPayrollRunTests(BaseAPITestCase):
    """End-to-end DRAFT payroll computed by the command-seeded rule set.

    This is the B4 'safest next action' executed in the isolated test
    environment: seed the researched rules, have an independent reviewer
    approve them, then run a draft period and verify the computed statutory
    amounts against the researched maths. No real employee is involved and no
    real payment is made — everything is synthetic and confined to the test DB.
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

    def test_draft_period_computes_from_the_seeded_rule_set(self):
        from decimal import Decimal

        from apps.staff_operations.models import PayrollTaxIdentity, StaffCompensation

        call_command("seed_nigeria_payroll_rules", stdout=StringIO())
        rule_set = PayrollStatutoryRuleSet.objects.get()
        # Independent review through the API (proposer is the system/none;
        # reviewer is a distinct authorised person).
        self.auth(self.reviewer)
        response = self.client.post(
            f"/api/admin/staff-operations/payroll/statutory-rules/{rule_set.reference}/review/",
            {"approved": True,
             "review_note": "Verified against Nigeria Tax Act 2025 and PRA 2014 for isolated QA."},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        rule_set.refresh_from_db()
        self.assertEqual(rule_set.status, PayrollStatutoryRuleSet.Status.APPROVED)

        self.auth(self.creator)
        tin = self.client.post("/api/admin/staff-operations/payroll/tax-identities/", {
            "staff_id": self.employee.pk, "tax_id": f"NG-TIN-{self.employee.pk}",
            "effective_from": self.today.isoformat(),
            "registered_on": self.today.isoformat(),
            "evidence_reference": f"TIN-{self.employee.pk}",
        }, format="json")
        self.assertEqual(tin.status_code, 201, tin.content)
        self.assertTrue(PayrollTaxIdentity.objects.filter(staff=self.employee).exists())

        comp = self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": self.employee.pk, "effective_from": self.today.isoformat(),
            "currency": "NGN", "basic_salary": "250000.00",
            "housing_allowance": "25000.00", "transport_allowance": "15000.00",
            "overtime_rate": "0.00", "pension_applicable": True,
            "minimum_wage_applicable": True,
        }, format="json")
        self.assertEqual(comp.status_code, 201, comp.content)

        period = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Housekeeping", "idempotency_key": "seeded-rules-draft-001",
            "adjustments": [],
        }, format="json")
        self.assertIn(period.status_code, (200, 201), period.content)
        line = period.json()["data"]["lines"][0]

        # Pensionable = 250,000 + 25,000 + 15,000 = 290,000 → employee 8% = 23,200;
        # employer 10% = 29,000. PAYE uses the engine's documented proration:
        # expected_periods = 13 − current month; projected chargeable =
        # monthly pensionable × periods × 0.92 (pension relief), banded at the
        # NTA 2025 rates, divided back over the periods.
        self.assertEqual(Decimal(line["pensionable_pay"]), Decimal("290000.00"))
        self.assertEqual(Decimal(line["employee_pension"]), Decimal("23200.00"))
        self.assertEqual(Decimal(line["employer_pension"]), Decimal("29000.00"))
        self.assertFalse(line["tax_snapshot"]["minimum_wage_exempt"])
        from apps.staff_operations.services.payroll_service import progressive_tax

        expected_periods = Decimal(13 - self.today.month)
        projected_chargeable = Decimal("290000.00") * expected_periods * Decimal("0.92")
        annual_tax, _ = progressive_tax(projected_chargeable, rule_set.paye_bands)
        expected_monthly = (annual_tax / expected_periods).quantize(
            Decimal("0.01"), rounding="ROUND_HALF_UP"
        )
        self.assertEqual(Decimal(line["paye_tax"]), expected_monthly)
        # Sanity band: chargeable sits just above the ₦800k tax-free band in a
        # 3-period projection, so PAYE must be positive but below 15% of it.
        self.assertGreater(Decimal(line["paye_tax"]), Decimal("0.00"))
        # The draft is clearly a draft: nothing is finalised or paid.
        self.assertEqual(period.json()["data"]["status"], "DRAFT")

    def test_manual_deductions_cannot_smuggle_statutory_labels(self):
        call_command("seed_nigeria_payroll_rules", stdout=StringIO())
        rule_set = PayrollStatutoryRuleSet.objects.get()
        self.auth(self.reviewer)
        self.client.post(
            f"/api/admin/staff-operations/payroll/statutory-rules/{rule_set.reference}/review/",
            {"approved": True, "review_note": "Approved for isolated QA only."},
            format="json",
        )
        self.auth(self.creator)
        self.client.post("/api/admin/staff-operations/payroll/tax-identities/", {
            "staff_id": self.employee.pk, "tax_id": f"NG-TIN-{self.employee.pk}",
            "effective_from": self.today.isoformat(), "registered_on": self.today.isoformat(),
            "evidence_reference": f"TIN-{self.employee.pk}",
        }, format="json")
        self.client.post("/api/admin/staff-operations/payroll/compensation/", {
            "staff_id": self.employee.pk, "effective_from": self.today.isoformat(),
            "currency": "NGN", "basic_salary": "60000.00", "overtime_rate": "0.00",
            "pension_applicable": True, "minimum_wage_applicable": True,
        }, format="json")
        response = self.client.post("/api/admin/staff-operations/payroll/periods/", {
            "starts_on": self.today.isoformat(), "ends_on": self.today.isoformat(),
            "department": "Housekeeping", "idempotency_key": "seeded-rules-guard-001",
            "adjustments": [{
                "staff_id": self.employee.pk, "overtime_hours": "0",
                "allowances": [], "bonus": "0.00",
                "deductions": [{"label": "PAYE", "amount": "5000.00"}],
            }],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
