"""EmailLog: queue lifecycle → synchronous delivery lifecycle.

* adds ``provider_message_id`` (Brevo message id recorded on acceptance);
* removes the queue-only fields ``task_id``, ``queued_at``, ``retry_count``
  and ``max_retries``;
* narrows the status choices to PENDING / SENDING / SENT / FAILED;
* DATA MIGRATION: historical rows stuck in the queue-era states are resolved
  to truthful terminal states so the email history is preserved:
    - QUEUED / RETRYING → FAILED ("the queue was removed before this email
      was confirmed sent") — these rows were never confirmed delivered;
    - stale SENDING rows older than one hour → FAILED (an in-flight
      synchronous send never survives a deploy).
  SENT and FAILED rows are never touched.
"""
from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def _resolve_queue_states(apps, schema_editor):
    EmailLog = apps.get_model("notifications", "EmailLog")
    now = timezone.now()
    EmailLog.objects.filter(status__in=["QUEUED", "RETRYING"]).update(
        status="FAILED",
        error_class="QueueRemoved",
        error_message=(
            "The legacy email queue was removed; this email was queued but "
            "never confirmed sent. Re-send it if it is still needed."
        ),
        failure_stage="BROKER",
        failed_at=now,
    )
    EmailLog.objects.filter(
        status="SENDING", created_at__lt=now - timedelta(hours=1)
    ).update(
        status="FAILED",
        error_class="StaleDelivery",
        error_message=(
            "This delivery attempt did not complete (interrupted before the "
            "provider answered). Re-send it if it is still needed."
        ),
        failure_stage="SMTP",
        failed_at=now,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0012_alter_emaillog_failure_stage"),
    ]

    operations = [
        migrations.RunPython(_resolve_queue_states, migrations.RunPython.noop),
        migrations.AddField(
            model_name="emaillog",
            name="provider_message_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
        migrations.RemoveField(model_name="emaillog", name="task_id"),
        migrations.RemoveField(model_name="emaillog", name="queued_at"),
        migrations.RemoveField(model_name="emaillog", name="retry_count"),
        migrations.RemoveField(model_name="emaillog", name="max_retries"),
        migrations.AlterField(
            model_name="emaillog",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("SENDING", "Sending"),
                    ("SENT", "Sent"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="PENDING",
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="emaillog",
            name="failure_stage",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "—"),
                    ("RENDER", "Rendering the receipt"),
                    ("ATTACHMENT", "Generating the PDF attachment"),
                    ("SMTP", "Submitting to the email provider"),
                ],
                db_default="",
                default="",
                max_length=16,
            ),
        ),
    ]
