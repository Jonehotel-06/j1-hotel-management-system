"""Diagnose TV gallery media that the catalog lists but the API cannot serve.

WHY THIS EXISTS
===============
The public catalog and the public media endpoint answer two different questions
from two different places:

    catalog  -> database only   (room active, media active, processing ready)
    media    -> database + the stored files

Production 2026-11-20 showed the catalog listing media 1–9 for active rooms
while ``/api/rooms-gallery/media/<id>/<rendition>/`` answered 404 for every one
of them. ``GET /api/rooms-gallery/catalog/`` therefore looked healthy while the
TV screen showed broken frames, and the 404s were reported as "the media does
not exist" although the records plainly did.

This command prints, for every media record, exactly which stored files it
needs and whether the current API process can read them — without deleting,
moving or re-uploading anything. It answers the question the reports asked:
"is the record wrong, or is the file unreachable?"

    python manage.py gallery_media_doctor              # every active record
    python manage.py gallery_media_doctor --id 1 --id 7
    python manage.py gallery_media_doctor --json       # machine readable
    python manage.py gallery_media_doctor --storage     # storage root health

Exit code 1 when any record that the catalog advertises is unreadable, so it can
be used as a deploy check.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.tv_rooms_gallery import storage
from apps.tv_rooms_gallery.models import GalleryMedia, GalleryRoom


class Command(BaseCommand):
    help = "Report why a TV gallery media record cannot be served (no writes, no deletes)."

    def add_arguments(self, parser):
        parser.add_argument("--id", action="append", type=int, default=[],
                            help="Limit the report to these media ids (repeatable).")
        parser.add_argument("--json", action="store_true", help="Print one JSON document.")
        parser.add_argument("--storage", action="store_true", help="Also print the storage root report.")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _needed(item):
        """The (rendition, key) pairs a browser will ask this record for."""
        if item.kind == "image":
            return [("thumb", item.thumbnail_key), ("display", item.display_key), ("file", item.storage_key)]
        if item.kind == "video":
            return [("file", item.display_key), ("thumb", item.thumbnail_key)]
        return [("file", item.storage_key)]

    @staticmethod
    def _visible(item):
        if not item.active or item.processing_status != "ready":
            return False
        if item.kind == "audio" and item.room_id is None:
            return True
        return GalleryRoom.objects.filter(
            room_id=item.room_id, active=True, room__is_active=True, room__room_type__is_active=True
        ).exists()

    def _report(self, item):
        entry = {
            "id": item.pk,
            "kind": item.kind,
            "room_id": item.room_id,
            "active": item.active,
            "processing_status": item.processing_status,
            "size": item.size,
            "content_type": item.content_type,
            "advertised_by_catalog": self._visible(item),
            "renditions": [],
        }
        for rendition, key in self._needed(item):
            path, outcome = storage.resolve(key) if key else (None, storage.FILE_MISSING)
            entry["renditions"].append({
                "rendition": rendition,
                "key": key or "",
                "outcome": outcome if key else "not_generated",
                "bytes": (path.stat().st_size if path is not None else 0),
            })
        entry["ok"] = all(r["outcome"] == storage.FILE_OK for r in entry["renditions"])
        return entry

    # -------------------------------------------------------------------- entry
    def handle(self, *args, **options):
        report = {"storage": storage.describe_root(), "media": []}
        queryset = GalleryMedia.objects.order_by("id")
        if options["id"]:
            queryset = queryset.filter(pk__in=options["id"])
        for item in queryset:
            report["media"].append(self._report(item))

        failing = [e for e in report["media"] if e["advertised_by_catalog"] and not e["ok"]]
        report["summary"] = {
            "records": len(report["media"]),
            "advertised": sum(1 for e in report["media"] if e["advertised_by_catalog"]),
            "unreadable_but_advertised": len(failing),
        }

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self._print_human(report)
        if failing:
            raise SystemExit(1)

    def _print_human(self, report):
        store = report["storage"]
        if store.get("configured"):
            self.stdout.write(self.style.SUCCESS("Storage root: %s" % store.get("path")))
            self.stdout.write("  writable: %s   files: %s   volume marker: %s" % (
                store.get("usable"), store.get("files"), store.get("volume_marker")))
        else:
            self.stdout.write(self.style.ERROR("Storage root unavailable: %s" % store.get("error")))

        if not report["media"]:
            self.stdout.write("No gallery media records.")
            return

        self.stdout.write("")
        for entry in report["media"]:
            marker = self.style.SUCCESS("OK ") if entry["ok"] else self.style.ERROR("BAD")
            listed = "listed" if entry["advertised_by_catalog"] else "not listed"
            self.stdout.write("%s media %-4s %-5s room=%-5s %-9s %-10s size=%s %s" % (
                marker, entry["id"], entry["kind"], entry["room_id"], entry["processing_status"],
                entry["content_type"], entry["size"], listed))
            for rendition in entry["renditions"]:
                line = "      %-8s %-34s %s" % (rendition["rendition"], rendition["key"] or "-", rendition["outcome"])
                if rendition["outcome"] == storage.FILE_OK:
                    self.stdout.write(line + " (%d bytes)" % rendition["bytes"])
                else:
                    self.stdout.write(self.style.WARNING(line))

        summary = report["summary"]
        self.stdout.write("")
        self.stdout.write(
            "Records: %d   advertised by catalog: %d   advertised but unreadable: %d"
            % (summary["records"], summary["advertised"], summary["unreadable_but_advertised"])
        )
        if summary["unreadable_but_advertised"]:
            self.stdout.write(self.style.ERROR(
                "\nThose records exist and are listed on the TV, but the stored files are not readable "
                "by this API process. Nothing has been deleted. Either:\n"
                "  * restore the gallery volume (TV_GALLERY_STORAGE_ROOT) from backup at the path shown above, or\n"
                "  * re-upload the files from the TV Gallery admin page (the existing rows can be\n"
                "    deactivated first, which removes them from the TV without deleting history).\n"
                "Compare the path above with the volume mount of the running API container: a differing\n"
                "root between releases is the usual cause."
            ))
