"""Explicit, safe repair for databases without an effective financial policy."""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.utils import hotel_today
from apps.finance.models import FinancialControlPolicy


class Command(BaseCommand):
    help = "Initialize conservative financial controls only if no policy has ever been configured."

    @transaction.atomic
    def handle(self, *args, **options):
        if FinancialControlPolicy.objects.select_for_update().exists():
            if FinancialControlPolicy.objects.filter(
                is_active=True, effective_from__lte=hotel_today()
            ).exists():
                self.stdout.write("An effective active policy already exists; no changes made.")
                return
            raise CommandError(
                "Policies exist, but none is currently effective and active. "
                "Review dates and activation in Finance admin; no policy was changed."
            )
        FinancialControlPolicy.objects.create(
            name="Default financial controls",
            effective_from=date(1970, 1, 1),
            is_active=True,
            require_separate_approver=True,
            refund_manager_approval_threshold=Decimal("0.00"),
            discount_manager_approval_threshold=Decimal("0.00"),
            cash_variance_manager_approval_threshold=Decimal("1000.00"),
            expense_manager_approval_threshold=Decimal("0.00"),
            notes="Conservative baseline initialized explicitly by management command.",
        )
        self.stdout.write(self.style.SUCCESS("Default financial controls initialized."))
