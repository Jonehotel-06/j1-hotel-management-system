from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.utils import hotel_today
from apps.finance.models import FinancialControlPolicy
from apps.finance.services.approval_service import active_control_policy


class FinancialPolicyInitializationTests(TestCase):
    def test_fresh_migration_has_effective_active_policy(self):
        self.assertTrue(active_control_policy().require_separate_approver)

    def test_empty_table_can_be_initialized_and_second_run_does_not_duplicate(self):
        FinancialControlPolicy.objects.all().delete()
        call_command('initialize_financial_policy', stdout=StringIO())
        self.assertEqual(FinancialControlPolicy.objects.count(), 1)
        self.assertEqual(active_control_policy().expense_manager_approval_threshold, 0)
        call_command('initialize_financial_policy', stdout=StringIO())
        self.assertEqual(FinancialControlPolicy.objects.count(), 1)

    def test_inactive_policy_is_not_reactivated(self):
        FinancialControlPolicy.objects.update(is_active=False)
        with self.assertRaises(CommandError):
            call_command('initialize_financial_policy', stdout=StringIO())
        self.assertFalse(FinancialControlPolicy.objects.filter(is_active=True).exists())

    def test_future_policy_is_not_overwritten(self):
        FinancialControlPolicy.objects.update(effective_from=hotel_today()+timedelta(days=10))
        with self.assertRaises(CommandError):
            call_command('initialize_financial_policy', stdout=StringIO())
        self.assertEqual(FinancialControlPolicy.objects.count(), 1)
