# apps/inventory/management/commands/process_pos_stock_consumption.py
"""Resolve open POS-to-inventory stock-consumption hand-offs.

Stock is normally depleted automatically inside the POS delivery transaction.
Run this when you need to catch up:

* sales delivered before automatic depletion was introduced;
* menu items whose recipe was configured after the sale;
* stock that was short at delivery time and has since been received.

Idempotent: the hand-off is unique per POS order and each resulting movement is
source-keyed on that hand-off, so running this repeatedly cannot double-deduct.

    python manage.py process_pos_stock_consumption --dry-run
    python manage.py process_pos_stock_consumption --limit 500
"""
from django.core.management.base import BaseCommand

from apps.inventory.models import StockConsumptionRequest
from apps.inventory.services.inventory_service import process_pending_pos_consumption


class Command(BaseCommand):
    help = "Post stock deductions for delivered POS orders whose hand-off is still open."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=500,
            help="Maximum number of open hand-offs to examine in this run (default 500).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what is open without posting any stock movement.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            open_requests = StockConsumptionRequest.objects.filter(
                status__in=[StockConsumptionRequest.Status.PENDING, StockConsumptionRequest.Status.FAILED]
            ).order_by("requested_at", "pk")[: max(1, options["limit"])]
            if not open_requests:
                self.stdout.write(self.style.SUCCESS("No open POS stock-consumption hand-offs."))
                return
            self.stdout.write(f"{len(open_requests)} open hand-off(s):")
            for request in open_requests:
                note = (request.resolution_note or "").strip() or "-"
                self.stdout.write(f"  {request.reference}  {request.status:<10}  {note[:120]}")
            self.stdout.write(self.style.WARNING("Dry run: nothing was posted."))
            return

        summary = process_pending_pos_consumption(limit=options["limit"])
        message = (
            f"Examined {summary['examined']} hand-off(s); posted {summary['posted']}; "
            f"still open {summary['still_open']}."
        )
        if summary["still_open"]:
            self.stdout.write(self.style.WARNING(message))
            self.stdout.write(
                "Hand-offs that remain open need a stock recipe, an active stock item/location, "
                "or sufficient on-hand stock. Re-run after correcting them."
            )
        else:
            self.stdout.write(self.style.SUCCESS(message))
