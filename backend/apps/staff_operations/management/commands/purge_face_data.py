"""Retention job for facial-verification data.

* ACTIVE templates past their expiry are expired and their descriptors cleared.
* Verification attempt rows older than FACE_ATTEMPT_RETENTION_DAYS are deleted.
Run daily, for example from cron: ``python manage.py purge_face_data``.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.staff_operations.face_models import FaceVerificationAttempt
from apps.staff_operations.services import face_service


class Command(BaseCommand):
    help = "Expire due facial templates and delete old verification attempt records."

    def handle(self, *args, **options):
        now = timezone.now()
        expired = face_service.expire_due_templates(now)
        cutoff = now - timedelta(days=int(getattr(settings, "FACE_ATTEMPT_RETENTION_DAYS", 365)))
        deleted, _ = FaceVerificationAttempt.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(f"Expired templates: {expired}. Deleted attempt rows: {deleted}.")
