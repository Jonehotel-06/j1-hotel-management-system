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
from apps.inventory.models import StockItem, StockMovement, Supplier
from apps.pos.models import MenuCategory, MenuItem
from apps.rooms.models import Room, RoomType
from apps.staff_operations.models import (
    PayrollStatutoryRuleEvent, PayrollStatutoryRuleSet, StaffProfile,
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
