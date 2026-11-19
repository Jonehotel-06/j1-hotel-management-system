"""Verify the PRIVATE TV gallery store (originals + renditions) end to end.

The gallery deliberately does not use ``MEDIA_ROOT`` (which is public). Its own
directory must exist, be writable by the API process and be on persistent
storage — if it is not, every Rooms Gallery upload fails. This command reports
the resolved path and performs a real write → read → delete round-trip so a
deployment fault is caught by an operator instead of by a staff member at
upload time.

    python manage.py check_gallery_storage
    python manage.py check_gallery_storage --json      # machine-readable
    python manage.py check_gallery_storage --skip-roundtrip

Exit code is non-zero when the store is unusable, so it can gate a release.
"""
import json as jsonlib
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tv_rooms_gallery import storage


class Command(BaseCommand):
    help = "Report and round-trip-test the private TV rooms-gallery storage directory."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
        parser.add_argument("--skip-roundtrip", action="store_true", help="Report only; write nothing.")

    def handle(self, *args, **options):
        configured = (getattr(settings, "TV_GALLERY_STORAGE_ROOT", "") or "")
        env_value = (storage.os.environ.get("TV_GALLERY_STORAGE_ROOT") or "")
        ready, detail = storage.is_configured()
        report = {
            "storage_ready": ready,
            "detail": detail,
            "setting": configured,
            "environment_variable": env_value,
            "resolved": detail if ready else "",
            "media_root": str(settings.MEDIA_ROOT),
            "max_upload_mb": getattr(settings, "TV_GALLERY_MAX_UPLOAD_MB", None),
            "ffmpeg": shutil.which("ffmpeg") or "",
            "ffprobe": shutil.which("ffprobe") or "",
            "roundtrip": "skipped",
        }

        if ready and not options["skip_roundtrip"]:
            try:
                key, size = storage.save_chunks([b"jone-gallery-probe"], 1024)
                path = storage.path_for(key)
                read_back = path.read_bytes()
                storage.remove(key)
                report["roundtrip"] = "ok" if read_back == b"jone-gallery-probe" else "mismatch"
                report["probe_bytes"] = size
            except Exception as exc:  # noqa: BLE001 - report the failure verbatim
                report["roundtrip"] = "failed: %s: %s" % (type(exc).__name__, exc)
                ready = False

        if options["json"]:
            self.stdout.write(jsonlib.dumps(report, indent=2))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("TV rooms-gallery storage"))
            for key in ("storage_ready", "resolved", "detail", "setting", "environment_variable",
                        "media_root", "max_upload_mb", "ffprobe", "ffmpeg", "roundtrip"):
                self.stdout.write(f"  {key:22}: {report[key]}")
            if not report["ffprobe"] or not report["ffmpeg"]:
                self.stdout.write(self.style.WARNING(
                    "  ffmpeg/ffprobe are missing: video uploads will be stored as private "
                    "attachments and cannot be transcoded for TV playback. Install ffmpeg."
                ))

        if not ready or report["roundtrip"] == "mismatch" or str(report["roundtrip"]).startswith("failed"):
            self.stderr.write(self.style.ERROR(
                "TV gallery storage is NOT usable. Set TV_GALLERY_STORAGE_ROOT to an absolute, "
                "writable directory on a persistent volume (Docker image default: "
                "/var/lib/jone-gallery) and restart the service."
            ))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("TV gallery storage is ready."))
