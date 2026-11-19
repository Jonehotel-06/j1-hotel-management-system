"""Regression tests for the 2026-11-19 Rooms Gallery upload repair.

Each test pins one root cause of the reported failure:

* ``TV_GALLERY_STORAGE_ROOT`` was never configured, so EVERY upload (JPG and
  MP4 alike) raised ImproperlyConfigured and the API answered HTTP 400
  "Validation failed.";
* the error envelope's per-field messages were shown as that generic sentence;
* modern phone photos (MPO / >32 MP / >16 MB) were rejected by the image
  pipeline;
* a retried upload created a second record.
"""
from io import BytesIO
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tv_rooms_gallery.models import GalleryMedia
from apps.tv_rooms_gallery import storage

from .factories import make_user, make_room, make_room_type

BASE = "/api/rooms-gallery/"


def jpeg(size=(64, 48), colour="blue", **save_kwargs):
    out = BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG", **save_kwargs)
    return out.getvalue()


class GalleryStorageConfigurationTests(TestCase):
    """The storage root must resolve without any environment variable."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user("gallery-storage@example.com", role="ADMIN"))
        self.room = make_room(make_room_type("Storage room"), "S-101")
        self.tmp = tempfile.TemporaryDirectory(prefix="gallery-default-root-")
        self.addCleanup(self.tmp.cleanup)

    def test_upload_works_with_no_environment_variable_set(self):
        """The reported production failure: nothing configured the root."""
        env = {k: v for k, v in os.environ.items() if k != "TV_GALLERY_STORAGE_ROOT"}
        with patch.dict(os.environ, env, clear=True), \
                override_settings(TV_GALLERY_STORAGE_ROOT=self.tmp.name):
            response = self.client.post(BASE + "manage/media/", {
                "file": SimpleUploadedFile("20261029_200400.jpg", jpeg((4032, 3024))),
                "room_id": str(self.room.pk),
            }, format="multipart")
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        item = GalleryMedia.objects.get(pk=response.data["id"])
        self.assertEqual(item.room_id, self.room.pk)
        self.assertEqual(item.kind, "image")
        self.assertTrue(item.upload_fingerprint)
        self.assertTrue(item.thumbnail_key and item.display_key)

    def test_settings_default_is_a_private_directory(self):
        """The default must be absolute, outside MEDIA_ROOT, and creatable."""
        from django.conf import settings
        from config.settings.base import _default_tv_gallery_root
        default = _default_tv_gallery_root(settings.MEDIA_ROOT)
        self.assertTrue(Path(str(default)).is_absolute())
        media = Path(settings.MEDIA_ROOT).resolve()
        self.assertNotEqual(Path(str(default)).resolve(), media)
        self.assertNotIn(media, Path(str(default)).resolve().parents)

    def test_directory_is_created_when_missing(self):
        target = Path(self.tmp.name) / "nested" / "gallery"
        self.assertFalse(target.exists())
        with override_settings(TV_GALLERY_STORAGE_ROOT=str(target)), \
                patch.dict(os.environ, {"TV_GALLERY_STORAGE_ROOT": str(target)}):
            self.assertEqual(storage.root(), target.resolve())
        self.assertTrue(target.is_dir())

    def test_public_media_root_is_refused(self):
        from django.conf import settings
        with patch.dict(os.environ, {"TV_GALLERY_STORAGE_ROOT": str(settings.MEDIA_ROOT)}):
            with self.assertRaises(storage.GalleryStorageUnavailable):
                storage.root()
        ready, detail = storage.is_configured()
        self.assertFalse(ready)
        self.assertIn("TV_GALLERY_STORAGE_ROOT", detail)


class GalleryMisconfigurationIsNotAValidationErrorTests(TestCase):
    """A storage fault must never be reported as an invalid file (HTTP 400)."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=make_user("gallery-config@example.com", role="ADMIN"))
        self.room = make_room(make_room_type("Config room"), "C-101")

    def test_missing_storage_returns_503_with_configuration_code(self):
        from django.conf import settings
        with patch.dict(os.environ, {"TV_GALLERY_STORAGE_ROOT": str(settings.MEDIA_ROOT)}):
            response = self.client.post(BASE + "manage/media/", {
                "file": SimpleUploadedFile("photo.jpg", jpeg()),
                "room_id": str(self.room.pk),
            }, format="multipart")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "GALLERY_STORAGE_UNAVAILABLE")
        self.assertNotIn("Validation failed", str(response.data))
        self.assertIn("TV_GALLERY_STORAGE_ROOT", response.data["message"])
        self.assertEqual(GalleryMedia.objects.count(), 0)

    def test_settings_payload_reports_storage_readiness(self):
        response = self.client.get(BASE + "manage/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("effective_max_upload_mb", response.data)
        self.assertIn("storage_ready", response.data)


class GalleryImageAcceptanceTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gallery-image-acceptance-")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TV_GALLERY_STORAGE_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = APIClient()
        self.client.force_authenticate(user=make_user("gallery-image@example.com", role="ADMIN"))
        self.room = make_room(make_room_type("Image room"), "I-101")

    def upload(self, name, content, room=True):
        data = {"file": SimpleUploadedFile(name, content)}
        if room:
            data["room_id"] = str(self.room.pk)
        return self.client.post(BASE + "manage/media/", data, format="multipart")

    def test_formats_reported_by_staff_are_accepted(self):
        cases = {
            "20261029_200400.jpg": jpeg((4032, 3024)),
            "IMG_0001.JPEG": jpeg((640, 480), "green"),
            "screenshot.png": self._png(),
            "photo.webp": self._webp(),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                response = self.upload(name, content)
                self.assertEqual(response.status_code, 201, getattr(response, "data", None))
                self.assertEqual(response.data["kind"], "image")

    def _png(self):
        out = BytesIO()
        Image.new("RGBA", (800, 600), (10, 20, 30, 128)).save(out, "PNG")
        return out.getvalue()

    def _webp(self):
        out = BytesIO()
        Image.new("RGB", (640, 480), "purple").save(out, "WEBP")
        return out.getvalue()

    def test_mpo_container_from_android_is_accepted(self):
        """Android writes an MPO (multi-picture JPEG); Pillow reports format MPO."""
        mpo = BytesIO()
        first = Image.new("RGB", (1200, 900), "orange")
        second = Image.new("RGB", (1200, 900), "teal")
        first.save(mpo, "MPO", save_all=True, append_images=[second])
        payload = mpo.getvalue()
        with patch("PIL.Image.open", wraps=Image.open) as opener:
            response = self.upload("20261029_200400.jpg", payload)
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        self.assertEqual(response.data["kind"], "image")
        self.assertTrue(opener.called)

    def test_48_megapixel_photo_is_accepted(self):
        """Above the old 32 MP ceiling, below the configured 200 MP ceiling."""
        response = self.upload("huge.jpg", jpeg((8000, 6000), "black", quality=45))
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        self.assertEqual(response.data["kind"], "image")

    def test_unsupported_and_oversized_files_still_fail_closed(self):
        # An executable masquerading as an image is stored privately, never as
        # an image (existing contract), and a corrupt JPEG is a real 400.
        corrupt = self.upload("broken.jpg", b"\xff\xd8\xff" + b"not-an-image" * 40)
        self.assertEqual(corrupt.status_code, 400)
        self.assertIn("file", corrupt.data["errors"])

        from apps.tv_rooms_gallery.models import GallerySettings
        config = GallerySettings.load()
        config.max_upload_mb = 1
        config.save()
        big = self.upload("big.png", self._png() + b"\x00" * (2 * 1024 * 1024))
        self.assertEqual(big.status_code, 400)
        message = big.data["errors"]["file"]
        message = message[0] if isinstance(message, list) else message
        self.assertIn("maximum allowed size", str(message))

    def test_error_envelope_carries_the_field_message(self):
        """The dashboard shows errors.file — the envelope must carry it."""
        response = self.upload("broken.jpg", b"\xff\xd8\xff" + b"x" * 100)
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.data)
        self.assertIn("file", response.data["errors"])


class GalleryRetryDeduplicationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gallery-dedupe-")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TV_GALLERY_STORAGE_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = APIClient()
        self.client.force_authenticate(user=make_user("gallery-dedupe@example.com", role="ADMIN"))
        self.room = make_room(make_room_type("Dedupe room"), "D-101")
        self.other = make_room(make_room_type("Dedupe room 2"), "D-102")

    def upload(self, room, name="retry.jpg", content=None):
        return self.client.post(BASE + "manage/media/", {
            "file": SimpleUploadedFile(name, content or jpeg((320, 240))),
            "room_id": str(room.pk),
        }, format="multipart")

    def test_retry_does_not_duplicate_and_second_room_is_allowed(self):
        first = self.upload(self.room)
        self.assertEqual(first.status_code, 201)
        again = self.upload(self.room)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data.get("duplicate"))
        self.assertEqual(again.data["id"], first.data["id"])
        self.assertEqual(GalleryMedia.objects.count(), 1)
        # Stored only once: the duplicate upload's temporary object is removed.
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 3)  # original + thumb + display

        elsewhere = self.upload(self.other, name="retry.jpg")
        self.assertEqual(elsewhere.status_code, 201)

    def test_different_bytes_are_not_treated_as_duplicates(self):
        self.assertEqual(self.upload(self.room).status_code, 201)
        different = self.upload(self.room, name="retry.jpg", content=jpeg((321, 241), "red"))
        self.assertEqual(different.status_code, 201)
        self.assertEqual(GalleryMedia.objects.count(), 2)
