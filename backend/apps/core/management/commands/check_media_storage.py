# apps/core/management/commands/check_media_storage.py
"""Verify the ACTIVE media storage backend by performing a real round-trip.

A configuration can look correct while Django is still writing to the local
filesystem, so this command reports the backend actually in use and then
writes → reads → deletes a small probe object through it.

    python manage.py check_media_storage            # report + round-trip
    python manage.py check_media_storage --keep     # leave the probe object

Nothing secret is printed: key ids and application keys are never displayed,
only whether they are present. This is a CLI tool, not an HTTP endpoint, so no
debug surface is exposed in production.
"""
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.storage import is_remote_storage, storage_backend_label


class Command(BaseCommand):
    help = "Report the active media storage backend and verify a real file round-trip."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true", help="Do not delete the probe object.")

    def handle(self, *args, **options):
        backend = storage_backend_label()
        remote = is_remote_storage()

        self.stdout.write(self.style.MIGRATE_HEADING("Media storage configuration"))
        self.stdout.write(f"  settings module : {settings.SETTINGS_MODULE}")
        self.stdout.write(f"  STORAGES default: {settings.STORAGES['default']['BACKEND']}")
        self.stdout.write(f"  active backend  : {backend}")
        self.stdout.write(f"  DEBUG           : {settings.DEBUG}")

        if remote:
            # Presence only — never the values themselves.
            self.stdout.write(f"  bucket          : {getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')}")
            self.stdout.write(f"  endpoint        : {getattr(settings, 'AWS_S3_ENDPOINT_URL', '')}")
            self.stdout.write(f"  region          : {getattr(settings, 'AWS_S3_REGION_NAME', '')}")
            self.stdout.write(f"  custom domain   : {getattr(settings, 'AWS_S3_CUSTOM_DOMAIN', '') or '(none)'}")
            self.stdout.write(
                f"  credentials     : key_id={'set' if getattr(settings, 'AWS_ACCESS_KEY_ID', '') else 'MISSING'}, "
                f"app_key={'set' if getattr(settings, 'AWS_SECRET_ACCESS_KEY', '') else 'MISSING'}"
            )
        else:
            self.stdout.write(f"  MEDIA_ROOT      : {settings.MEDIA_ROOT}")
            self.stdout.write(
                self.style.WARNING(
                    "  NOTE: local filesystem storage is in use. On many hosted "
                    "platforms the disk is ephemeral — uploads can be lost on deploy/restart. "
                    "Set the BACKBLAZE_* variables to store media in Backblaze B2."
                )
            )

        name = f"_healthcheck/storage-probe-{timezone.now():%Y%m%d%H%M%S}.txt"
        payload = b"jone-storage-probe"

        self.stdout.write(self.style.MIGRATE_HEADING("Round-trip test"))
        try:
            saved_name = default_storage.save(name, ContentFile(payload))
        except Exception as exc:
            raise CommandError(f"WRITE FAILED via {backend}: {type(exc).__name__}: {exc}")
        self.stdout.write(self.style.SUCCESS(f"  write  OK -> {saved_name}"))

        try:
            if not default_storage.exists(saved_name):
                raise CommandError(f"Object '{saved_name}' is not readable back from {backend}.")
            with default_storage.open(saved_name) as handle:
                content = handle.read()
            if content != payload:
                raise CommandError("Read-back content does not match what was written.")
            self.stdout.write(self.style.SUCCESS("  read   OK (content matches)"))
            self.stdout.write(f"  url       : {default_storage.url(saved_name)}")
        finally:
            if not options["keep"]:
                try:
                    default_storage.delete(saved_name)
                    self.stdout.write(self.style.SUCCESS("  delete OK (probe removed)"))
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"  delete failed: {type(exc).__name__}: {exc}"))

        target = "Backblaze B2 / S3-compatible bucket" if remote else "local filesystem"
        self.stdout.write(self.style.SUCCESS(f"\nMedia storage verified: files are stored in the {target}."))
