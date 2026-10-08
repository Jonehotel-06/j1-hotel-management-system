"""Re-queue tracked emails that never reached the broker.

Operational recovery tool for a broker (Redis) outage: while the broker is
down, every queued email becomes FAILED with failure_stage=BROKER (see
apps.core.emails._dispatch). Once Redis/Celery is healthy again, run:

    python manage.py requeue_emails            # BROKER failures only
    python manage.py requeue_emails --all      # any non-terminal failure

Rows are reset to PENDING and handed back to the broker. Rows already SENT are
never touched, so the command can never double-deliver a message.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.notifications.models import EmailLog


class Command(BaseCommand):
    help = "Re-queue EmailLog rows that failed to reach the task broker (or other non-terminal failures)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Re-queue every FAILED email, not only BROKER-stage failures.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Only report what would be re-queued.",
        )

    def handle(self, *args, **options):
        from apps.core.emails import _dispatch

        qs = EmailLog.objects.filter(status=EmailLog.Status.FAILED)
        if not options["all"]:
            qs = qs.filter(failure_stage=EmailLog.FailureStage.BROKER)
        rows = list(qs.order_by("id"))
        if not rows:
            self.stdout.write("No failed emails to re-queue.")
            return

        self.stdout.write(f"Re-queueing {len(rows)} email(s)...")
        requeued = 0
        for log in rows:
            if options["dry_run"]:
                self.stdout.write(f"  [dry-run] EmailLog#{log.pk} {log.kind} -> {log.to_email}")
                continue
            EmailLog.objects.filter(pk=log.pk).update(
                status=EmailLog.Status.PENDING,
                error_class="",
                error_message="",
                failure_stage="",
                failed_at=None,
                retry_count=0,
            )
            _dispatch(log)
            log.refresh_from_db()
            self.stdout.write(
                f"  EmailLog#{log.pk} {log.kind} -> {log.to_email}: {log.status}"
            )
            requeued += 1
        if options["dry_run"]:
            self.stdout.write(f"[dry-run] {len(rows)} email(s) would be re-queued.")
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Re-queued {requeued} email(s) at {timezone.now():%Y-%m-%d %H:%M} UTC.")
            )
