# apps/finance/management/commands/reconcile_accommodation.py
"""Report (and optionally repair) missing nightly accommodation recognition.

Audit D4/N1: the October 2026 ledger carried ₦50,500 of food-and-beverage
revenue and ₦0 tax while two checked-out stays owed ₦21,750 of tax — the nights
those bookings covered had never been posted. Room revenue is recognized one
night at a time; if the nightly task or the checkout catch-up never ran for a
stay, nothing on the ledger says so, and there was no supported way to find or
repair it.

This command is that way. ``--check`` (the default) only reports; ``--post``
re-posts the missing nights through the same idempotent accrual service, so the
amounts always come from the booking's own totals and a second run changes
nothing.

Examples
--------
    python manage.py reconcile_accommodation
    python manage.py reconcile_accommodation --since 2026-10-01 --json
    python manage.py reconcile_accommodation --post --actor admin@example.com
    python manage.py reconcile_accommodation --stay STY-J1-20261010-ABC123 --post --actor admin@example.com
"""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.core.utils import hotel_today
from apps.finance.services.accommodation_reconciliation import (
    MAX_LIMIT,
    accommodation_night_gaps,
    post_missing_accommodation_charges,
)

ZERO = Decimal("0.00")


class Command(BaseCommand):
    help = "Detect (and with --post, repair) stay nights missing from the finance ledger."

    def add_arguments(self, parser):
        parser.add_argument("--post", action="store_true",
                            help="Post the missing nights. Requires --actor for accountability.")
        parser.add_argument("--actor", metavar="EMAIL",
                            help="Staff account the postings are attributed to (required with --post).")
        parser.add_argument("--stay", metavar="REFERENCE", help="Only consider this stay reference.")
        parser.add_argument("--since", metavar="YYYY-MM-DD", help="Only stays arriving on/after this date.")
        parser.add_argument("--through", metavar="YYYY-MM-DD",
                            help="Last night to consider (default: yesterday, hotel time).")
        parser.add_argument("--limit", type=int, default=200,
                            help=f"Maximum stays to examine (1-{MAX_LIMIT}, default 200).")
        parser.add_argument("--json", action="store_true", dest="as_json",
                            help="Emit machine-readable JSON instead of a table.")
        parser.add_argument("--fail-on-gap", action="store_true",
                            help="Exit non-zero when any stay still has missing nights after the run.")

    def handle(self, *args, **options):
        through_date = self._parse_date(options.get("through"), "through") or (hotel_today() - timedelta(days=1))
        since = self._parse_date(options.get("since"), "since")
        limit = options["limit"]
        if limit < 1 or limit > MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}.")
        if options["post"] and not options.get("actor"):
            raise CommandError("--post requires --actor EMAIL so the postings are accountable.")

        actor = None
        if options.get("actor"):
            email = options["actor"].strip().lower()
            actor = User.objects.filter(email__iexact=email, is_active=True).first()
            if actor is None:
                raise CommandError("No active staff account matches --actor.")
            if not actor.is_staff_member:
                raise CommandError("--actor must be a staff account, not a guest.")

        gaps = accommodation_night_gaps(
            through_date=through_date, stay_reference=options.get("stay"),
            since=since, limit=limit + 1,
        )
        truncated = len(gaps) > limit
        gaps = gaps[:limit]

        created = 0
        failures = []
        if options["post"] and gaps:
            created, failures = post_missing_accommodation_charges(
                gaps, through_date=through_date, actor=actor
            )

        # Re-read after posting so the report always describes what is on the
        # ledger now — for a repair run that is the residual, not the original gap.
        remaining = accommodation_night_gaps(
            through_date=through_date, stay_reference=options.get("stay"),
            since=since, limit=limit + 1,
        ) if options["post"] else gaps

        if options["as_json"]:
            self.stdout.write(json.dumps({
                "through_date": through_date.isoformat(),
                "posted": options["post"],
                "actor": getattr(actor, "email", None),
                "gaps_found": len(gaps),
                "transactions_created": created,
                "failures": failures,
                "truncated": truncated,
                "remaining_gaps": [gap.as_dict() for gap in remaining],
            }, indent=2))
        else:
            self._write_table(gaps, through_date=through_date, posted=options["post"], created=created)

        if failures:
            raise CommandError("Some stays could not be reconciled:\n  " + "\n  ".join(failures))
        if remaining and options["fail_on_gap"]:
            raise CommandError(f"{len(remaining)} stay(s) still have unposted nights.")

    # -- output helpers ----------------------------------------------------
    def _write_table(self, gaps, *, through_date, posted, created):
        self.stdout.write(f"Accommodation recognition through {through_date.isoformat()}"
                          f"{' (repair run)' if posted else ''}")
        if not gaps:
            self.stdout.write(self.style.SUCCESS("No missing nights: every elapsed stay night is on the ledger."))
            return
        totals = {"nights": 0, "net": ZERO, "tax": ZERO, "fee": ZERO}
        self.stdout.write("")
        self.stdout.write(f"{'stay':<28}{'status':<12}{'nights':>8}{'net':>13}{'tax':>12}{'fee':>10}")
        for gap in gaps:
            totals["nights"] += gap.nights_missing
            totals["net"] += gap.net_amount
            totals["tax"] += gap.tax_amount
            totals["fee"] += gap.fee_amount
            self.stdout.write(
                f"{gap.stay_reference:<28}{gap.stay_status:<12}{gap.nights_missing:>8}"
                f"{gap.net_amount:>13.2f}{gap.tax_amount:>12.2f}{gap.fee_amount:>10.2f}"
            )
        self.stdout.write(
            f"{'TOTAL':<28}{'':<12}{totals['nights']:>8}"
            f"{totals['net']:>13.2f}{totals['tax']:>12.2f}{totals['fee']:>10.2f}"
        )
        if posted:
            self.stdout.write(self.style.SUCCESS(f"Posted {created} missing night(s)."))
            self.stdout.write("Re-run --check to confirm no nights remain unposted.")
        else:
            self.stdout.write("")
            self.stdout.write("No ledger changes were made. Re-run with --post --actor EMAIL to repair.")

    def _parse_date(self, value, label):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CommandError(f"--{label} must be an ISO date (YYYY-MM-DD).") from exc
