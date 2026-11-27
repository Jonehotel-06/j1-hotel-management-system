# tests/test_critical_repair_gallery_preview.py
"""Rooms-gallery preview repair (2026-11-26).

Reported fault: the gallery preview said "Preview unavailable - open the file"
and showed nothing, and the preview could show a file other than the one just
selected.

Two independent causes, both pinned here:

1. BACKEND — an image was requested as ``display`` and then ``file``.
   ``file`` is not a rendition an image has, so an image whose derived
   rendition had never been produced (media stored before the thumbnail
   pipeline, or whose rendition object was lost) could not be previewed at
   all, while its ORIGINAL upload sat in storage, readable.

2. FRONTEND — every preview wrote into the same element with no request
   identity, so a slower earlier response could overwrite a newer one, and the
   object URL of the preview being replaced was never revoked.

The repair never touches the stored original: ``storage_key`` keeps its value,
no existing object is renamed, moved or overwritten, and a missing rendition is
re-derived under a NEW key. Legacy media therefore keeps working across the
deploy.
"""
import os
import tempfile
from io import BytesIO
from unittest.mock import patch

from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.tv_rooms_gallery.models import GalleryMedia, GalleryRoom
from apps.tv_rooms_gallery import storage

from .factories import make_room, make_room_type, make_user

BASE = "/api/rooms-gallery/"


def png_bytes(size=(120, 90), colour="navy"):
    out = BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


class GalleryPreviewRepairTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="gallery-preview-")
        self.env = patch.dict(os.environ, {"TV_GALLERY_STORAGE_ROOT": self.directory.name})
        self.env.start()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.env.stop)
        self.client = APIClient()
        self.admin = make_user("gallery-preview-admin@example.com", role="ADMIN")
        self.client.force_authenticate(user=self.admin)
        self.room = make_room(make_room_type("Preview suite"), "P-101")

    # ------------------------------------------------------------- helpers

    def upload(self, name="lobby.png", payload=None):
        response = self.client.post(
            BASE + "manage/media/",
            {"file": SimpleUploadedFile(name, payload or png_bytes()), "room_id": str(self.room.pk)},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        return GalleryMedia.objects.get(pk=response.data["id"])

    def legacy(self, item):
        """Turn a media row into one stored BEFORE renditions existed."""
        GalleryMedia.objects.filter(pk=item.pk).update(thumbnail_key="", display_key="")
        item.refresh_from_db()
        return item

    # --------------------------------------------------------------- tests

    def test_a_fresh_image_reports_which_renditions_it_has(self):
        item = self.upload()
        response = self.client.get(BASE + "manage/media/")
        row = [r for r in response.data if r["id"] == item.pk][0]
        self.assertEqual(row["renditions"], {"thumb": True, "display": True, "original": True})
        # Additive field: the pre-existing keys are still all present.
        for key in ("id", "room_id", "kind", "name", "size", "active", "is_cover", "order",
                    "processing_status", "processing_error"):
            self.assertIn(key, row)

    def test_a_legacy_image_previews_by_rebuilding_the_missing_rendition(self):
        item = self.legacy(self.upload())
        original_key = item.storage_key
        self.assertEqual(self.client.get(BASE + "manage/media/%s/display/" % item.pk).status_code, 200)
        item.refresh_from_db()
        # The derived rendition was produced under a NEW key …
        self.assertTrue(item.display_key)
        self.assertNotEqual(item.display_key, "")
        # … and the stored original is untouched, so old URLs still resolve.
        self.assertEqual(item.storage_key, original_key)
        path, outcome = storage.resolve(original_key)
        self.assertEqual(outcome, storage.FILE_OK)
        self.assertTrue(path.exists())
        self.assertTrue(
            AuditLog.objects.filter(action="TV_GALLERY_RENDITION_REBUILT", object_id=str(item.pk)).exists()
        )

    def test_the_thumbnail_of_a_legacy_image_is_rebuilt_too(self):
        item = self.legacy(self.upload())
        response = self.client.get(BASE + "manage/media/%s/thumb/" % item.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        item.refresh_from_db()
        self.assertTrue(item.thumbnail_key)
        self.assertEqual(item.storage_key, GalleryMedia.objects.get(pk=item.pk).storage_key)

    def test_the_staff_preview_can_fall_back_to_the_original(self):
        item = self.upload()
        response = self.client.get(BASE + "manage/media/%s/original/" % item.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Content-Disposition"], "inline")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response["Cache-Control"])

    def test_the_public_route_never_serves_an_original_upload(self):
        item = self.upload()
        GalleryRoom.objects.create(room=self.room, active=True)
        anon = APIClient()
        self.assertEqual(anon.get(BASE + "media/%s/original/" % item.pk).status_code, 404)
        self.assertEqual(anon.get(BASE + "media/%s/file/" % item.pk).status_code, 404)
        # The re-encoded renditions remain what the TV is given.
        self.assertEqual(anon.get(BASE + "media/%s/display/" % item.pk).status_code, 200)

    def test_a_rebuild_request_repairs_both_renditions(self):
        item = self.legacy(self.upload())
        response = self.client.post(BASE + "manage/media/%s/rebuild-renditions/" % item.pk)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.data["rebuilt"], {"thumb": True, "display": True})
        item.refresh_from_db()
        self.assertTrue(item.thumbnail_key and item.display_key)

    def test_a_rebuild_is_refused_for_a_video(self):
        payload = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        response = self.client.post(
            BASE + "manage/media/",
            {"file": SimpleUploadedFile("clip.mp4", payload), "room_id": str(self.room.pk)},
            format="multipart",
        )
        item = GalleryMedia.objects.get(pk=response.data["id"])
        refused = self.client.post(BASE + "manage/media/%s/rebuild-renditions/" % item.pk)
        self.assertEqual(refused.status_code, 400, getattr(refused, "data", None))

    def test_a_rebuild_reports_when_the_original_is_gone(self):
        item = self.legacy(self.upload())
        path, _ = storage.resolve(item.storage_key)
        os.unlink(path)
        response = self.client.post(BASE + "manage/media/%s/rebuild-renditions/" % item.pk)
        self.assertEqual(response.status_code, 409, getattr(response, "data", None))
        self.assertIn("check_gallery_storage", response.data["message"])
        item.refresh_from_db()
        self.assertEqual(item.thumbnail_key, "")

    def test_a_guest_cannot_reach_the_preview_routes(self):
        item = self.upload()
        client = APIClient()
        client.force_authenticate(user=make_user("gallery-preview-guest@example.com", role="GUEST"))
        self.assertEqual(client.get(BASE + "manage/media/%s/original/" % item.pk).status_code, 403)
        self.assertEqual(
            client.post(BASE + "manage/media/%s/rebuild-renditions/" % item.pk).status_code, 403
        )

    def test_deleting_a_media_row_never_touches_other_stored_objects(self):
        keep = self.upload("keep.png")
        drop = self.upload("drop.png", png_bytes(colour="green"))
        self.assertEqual(self.client.delete(BASE + "manage/media/%s/" % drop.pk).status_code, 204)
        path, outcome = storage.resolve(keep.storage_key)
        self.assertEqual(outcome, storage.FILE_OK)
        self.assertTrue(path.exists())
