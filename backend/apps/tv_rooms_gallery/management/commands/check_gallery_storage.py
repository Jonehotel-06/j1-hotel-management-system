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
    python manage.py check_gallery_storage --id 1 --id 7   # audit specific media
    python manage.py check_gallery_storage --skip-media     # directory only

It also audits every gallery RECORD against the files it needs. A writable
directory is not enough: production 2026-11-20 had a healthy, writable storage
root and seven media records whose files had vanished, so this command said
"ready" while the TV showed nothing. The audit answers "is the record wrong, or
is the file unreachable?" and exits non-zero when a record the catalog
advertises cannot be read, so a deploy can be gated on it.

Exit code is non-zero when the store is unusable, so it can gate a release.
"""
import json as jsonlib
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tv_rooms_gallery import storage
from apps.tv_rooms_gallery.models import GalleryMedia


class Command(BaseCommand):
    help = "Report and round-trip-test the private TV rooms-gallery storage directory."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
        parser.add_argument("--skip-roundtrip", action="store_true", help="Report only; write nothing.")
        parser.add_argument("--skip-media", action="store_true",
                            help="Skip the per-record file audit.")
        parser.add_argument("--id", action="append", type=int, default=[],
                            help="Audit only these media ids (repeatable).")

    @staticmethod
    def _persistence(root_path):
        """Is the root on a MOUNTED filesystem, or inside the container layer?

        Files written into a container's own filesystem disappear when that
        container is replaced by the next deploy — which is exactly how seven
        gallery records lost their files while every upload had "succeeded".
        A root that shares a mount with / and is not itself a mountpoint is
        reported, because no amount of code can make such a directory survive.
        """
        import os

        try:
            if not root_path:
                return {"mounted": None, "detail": "storage root not resolved"}
            target = os.path.realpath(root_path)
            device = os.stat(target).st_dev
            root_device = os.stat("/").st_dev
            mounts = []
            try:
                with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
                    mounts = [line.split(" ")[1] for line in handle if len(line.split(" ")) > 1]
            except OSError:
                mounts = []
            is_mountpoint = target.rstrip("/") in {m.rstrip("/") for m in mounts}
            return {
                "mounted": bool(device != root_device or is_mountpoint),
                "detail": ("separate filesystem or mountpoint" if (device != root_device or is_mountpoint)
                           else "same filesystem as / and not a mountpoint: NOT on a volume, "
                                "files are lost on redeploy"),
            }
        except OSError as exc:
            return {"mounted": None, "detail": "unavailable: %s" % exc}

    @staticmethod
    def _needed(item):
        """The (rendition, key) pairs a browser will ask this record for."""
        if item.kind == "image":
            return [("thumb", item.thumbnail_key), ("display", item.display_key), ("file", item.storage_key)]
        if item.kind == "video":
            return [("file", item.display_key), ("thumb", item.thumbnail_key)]
        return [("file", item.storage_key)]

    def _audit_media(self, ids):
        records = []
        queryset = GalleryMedia.objects.order_by("id")
        if ids:
            queryset = queryset.filter(pk__in=ids)
        for item in queryset:
            entry = {
                "id": item.pk, "kind": item.kind, "active": item.active,
                "room_id": item.room_id, "processing_status": item.processing_status,
                "renditions": [],
            }
            for rendition, key in self._needed(item):
                path, outcome = storage.resolve(key) if key else (None, storage.FILE_MISSING)
                entry["renditions"].append({
                    "rendition": rendition, "key": key or "",
                    "outcome": outcome if key else "not_generated",
                    "bytes": path.stat().st_size if path is not None else 0,
                })
            entry["ok"] = all(r["outcome"] == storage.FILE_OK for r in entry["renditions"])
            records.append(entry)
        return records

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

        if ready:
            report["persistence"] = self._persistence(report["resolved"])
        else:
            report["persistence"] = {"mounted": None, "detail": "storage not resolved"}
        if options["skip_media"]:
            report["media"] = []
            report["summary"] = {"records": 0, "unreadable": 0}
        else:
            report["media"] = self._audit_media(options["id"])
            report["summary"] = {
                "records": len(report["media"]),
                "unreadable": sum(1 for entry in report["media"] if not entry["ok"]),
            }

        if options["json"]:
            self.stdout.write(jsonlib.dumps(report, indent=2))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("TV rooms-gallery storage"))
            for key in ("storage_ready", "resolved", "detail", "setting", "environment_variable",
                        "media_root", "max_upload_mb", "ffprobe", "ffmpeg", "roundtrip"):
                self.stdout.write(f"  {key:22}: {report[key]}")
            self.stdout.write(f"  {'persistence':22}: {report['persistence']['mounted']} "
                              f"({report['persistence']['detail']})")
            for entry in report["media"]:
                marker = "OK " if entry["ok"] else "BAD"
                self.stdout.write("  %s media %-4s %-5s room=%-5s %-8s" % (
                    marker, entry["id"], entry["kind"], entry["room_id"], entry["processing_status"]))
                for rendition in entry["renditions"]:
                    line = "        %-8s %-34s %s" % (
                        rendition["rendition"], rendition["key"] or "-", rendition["outcome"])
                    self.stdout.write(
                        line + (" (%d bytes)" % rendition["bytes"]) if rendition["outcome"] == storage.FILE_OK
                        else self.style.WARNING(line))
            summary = report["summary"]
            if summary["records"]:
                self.stdout.write("  Media records: %d   unreadable: %d"
                                  % (summary["records"], summary["unreadable"]))
            if report["persistence"]["mounted"] is False:
                self.stdout.write(self.style.ERROR(
                    "  The storage root is NOT on a mounted volume. Every uploaded gallery file "
                    "is destroyed when this container is replaced by a deploy. Mount a volume at "
                    "TV_GALLERY_STORAGE_ROOT (Docker image default: /var/lib/jone-gallery)."))
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
        if report["media"] and report["summary"]["unreadable"]:
            self.stderr.write(self.style.ERROR(
                "%d gallery record(s) exist but their stored files are not readable by this API "
                "process. Nothing has been deleted. Restore the volume at the path above from "
                "backup, or re-upload the files from the TV Gallery admin page. Compare that path "
                "with the volume mount of the running API container: a differing root between "
                "releases is the usual cause."
                % report["summary"]["unreadable"]))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("TV gallery storage is ready."))
