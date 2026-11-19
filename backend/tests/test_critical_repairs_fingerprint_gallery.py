"""Regression tests for the critical fingerprint + TV gallery repairs.

Pins the defects found after the 2026-11-19 repair, each of which left one
of the two features broken while the earlier test suite stayed green:

* gallery videos were published to a Celery queue no worker consumed, and
  the global Celery time limit killed longer transcodes;
* duplicate protection skipped background-music uploads (no room);
* thumbnail generation composited full-resolution frames (OOM -> the
  browser's "Network connection interrupted");
* the server-side bridge adapter still defaulted to stale port 8765.
"""
from io import BytesIO
from pathlib import Path
import tempfile

from PIL import Image
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tv_rooms_gallery.models import GalleryMedia
from apps.tv_rooms_gallery.thumbnails import make_thumbnail
from apps.tv_rooms_gallery import tasks as gallery_tasks

from .factories import make_user, make_room, make_room_type

BASE = "/api/rooms-gallery/"


def jpeg_bytes(size=(64, 48), colour="blue"):
    out = BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG")
    return out.getvalue()


class GalleryDuplicateMusicTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user("gallery-dup@example.com", role="ADMIN"))
        self.tmp = tempfile.TemporaryDirectory(prefix="gallery-dup-")
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(TV_GALLERY_STORAGE_ROOT=self.tmp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_background_music_retry_is_not_duplicated(self):
        payload = jpeg_bytes()
        first = self.client.post(BASE + "manage/media/", {
            "file": SimpleUploadedFile("lobby.jpg", payload),
        }, format="multipart")
        self.assertEqual(first.status_code, 201, getattr(first, "data", None))
        second = self.client.post(BASE + "manage/media/", {
            "file": SimpleUploadedFile("lobby.jpg", payload),
        }, format="multipart")
        self.assertEqual(second.status_code, 200, getattr(second, "data", None))
        self.assertTrue(second.data.get("duplicate"))
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(GalleryMedia.objects.count(), 1)


class GalleryThumbnailMemoryShapeTests(TestCase):
    def test_large_phone_image_produces_bounded_renditions(self):
        data = jpeg_bytes(size=(6000, 4000), colour="navy")
        thumb = make_thumbnail(data, size=(640, 400), max_bytes=128 * 1024 * 1024,
                               max_pixels=200_000_000)
        display = make_thumbnail(data, size=(1920, 1080), quality=88,
                                 max_bytes=128 * 1024 * 1024, max_pixels=200_000_000)
        for blob, box in ((thumb, (640, 400)), (display, (1920, 1080))):
            self.assertTrue(blob.startswith(b"\xff\xd8"))  # JPEG
            with Image.open(BytesIO(blob)) as img:
                self.assertLessEqual(img.width, box[0])
                self.assertLessEqual(img.height, box[1])

    def test_oversize_pixel_count_still_refused(self):
        data = jpeg_bytes(size=(64, 64))
        with self.assertRaises(ValueError):
            make_thumbnail(data, max_pixels=1000)


class GalleryVideoQueueTests(TestCase):
    def test_video_task_has_gallery_sized_time_limits(self):
        # ffmpeg may legitimately run 360 s; the global 90/120 s Celery
        # limits must not apply to this task.
        self.assertGreaterEqual(gallery_tasks.process_video.soft_time_limit or 0, 360)
        self.assertGreaterEqual(gallery_tasks.process_video.time_limit or 0,
                                gallery_tasks.process_video.soft_time_limit)

    def test_workers_consume_the_tv_gallery_queue(self):
        repo = Path(settings.BASE_DIR).parent
        candidates = [repo / "compose.yaml", repo / "backend" / "docker" / "docker-cmd.sh"]
        found = [p for p in candidates if p.exists()]
        self.assertTrue(found, "deployment files not found to check queue coverage")
        for path in found:
            self.assertIn("tv_gallery", path.read_text(errors="ignore"),
                          "%s starts a worker that never consumes tv_gallery" % path)


class FingerprintBridgePortTests(TestCase):
    def test_server_adapter_default_port_matches_workstation_bridge(self):
        from apps.staff_operations.services import fingerprint_bridge
        self.assertEqual(fingerprint_bridge.BRIDGE_PORT, 8787)
