"""TV rooms-gallery DURABLE object backend (2026-11-22 media persistence repair).

Root cause being pinned here
============================
The gallery store was filesystem-only. On a container host whose
TV_GALLERY_STORAGE_ROOT is NOT a mounted volume (the production condition the
storage module's 2026-11-20 note documents), every deploy replaced the
container and took the gallery bytes with it while the database rows survived:
the catalog kept listing media whose renditions all answered 404.

The repair adds the same durable home the project already relies on for public
media — an S3-compatible object store (Backblaze B2) — behind the EXISTING
storage interface, selected by TV_GALLERY_STORAGE_BACKEND (auto | fs | object).
These tests pin, against an in-memory fake client (no network, no credentials):

  * the auto decision: a mounted-looking directory stays fs; an ephemeral one
    with a configured bucket switches to object; without a bucket it stays fs;
  * fs mode is byte-for-byte the previous behaviour (files only, no cache dir);
  * object mode: uploads/renditions/deletes round-trip through the storage API
    that the upload and serving paths use; the durable bytes are in the object
    store, only the private staging/cache layer is local; MEDIA_ROOT/public
    media is never touched;
  * resolve() reports missing / unavailable honestly (404 vs 503) in object mode;
  * the serving layer, the duplicate-retry rule and cleanup keep working
    unchanged in object mode;
  * removing a media item deletes the object AND the cache entry;
  * check_gallery_storage and gallery_media_doctor work in object mode.

No real object store is contacted: object_store.client() is replaced with a
fake, and no test uploads anything to any external service.
"""
import os
import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from apps.tv_rooms_gallery import object_store, storage
from apps.tv_rooms_gallery.models import GalleryMedia, GalleryRoom
from .factories import make_user, make_room_type, make_room


class FakeObjectError(Exception):
    """Mimics a botocore ClientError just enough for object_store's mapping."""

    def __init__(self, code, status):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeBody:
    def __init__(self, payload):
        self._payload = payload

    def iter_chunks(self, chunk_size=1024 * 1024):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start:start + chunk_size]

    def close(self):
        pass


class FakeClient:
    """In-memory S3 stand-in implementing the surface object_store uses.

    Method signatures use boto3's own keyword names (Bucket/Key) so the fake
    exercises the exact call shapes the real client receives.
    """

    def __init__(self):
        self.objects = {}
        self.lock = threading.Lock()
        self.fail_reads = False

    def upload_fileobj(self, fileobj, Bucket, Key):
        with self.lock:
            self.objects[Key] = fileobj.read()

    def get_object(self, Bucket, Key):
        if self.fail_reads:
            raise FakeObjectError("ServiceUnavailable", 503)
        with self.lock:
            payload = self.objects.get(Key)
        if payload is None:
            raise FakeObjectError("NoSuchKey", 404)
        return {"Body": FakeBody(payload)}

    def head_object(self, Bucket, Key):
        if self.fail_reads:
            raise FakeObjectError("ServiceUnavailable", 503)
        with self.lock:
            payload = self.objects.get(Key)
        if payload is None:
            raise FakeObjectError("NotFound", 404)
        return {"ContentLength": len(payload)}

    def delete_object(self, Bucket, Key):
        with self.lock:
            self.objects.pop(Key, None)
        return {}

    def get_paginator(self, name):
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                with client.lock:
                    entries = [
                        {"Key": key, "LastModified": None}
                        for key in sorted(client.objects)
                        if key.startswith(Prefix)
                    ]
                yield {"Contents": entries}

        return Paginator()


class ObjectBackendTestCase(TestCase):
    """Shared fixture: env-configured store, fake object client, tmp directory."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="tv-gallery-object-test-")
        self.env = patch.dict(os.environ, {
            "TV_GALLERY_STORAGE_ROOT": self.directory,
            "TV_GALLERY_BUCKET_NAME": "jone-gallery-private-test",
            "TV_GALLERY_BUCKET_KEY_ID": "test-key-id",
            "TV_GALLERY_BUCKET_APPLICATION_KEY": "test-app-key",
        })
        self.env.start()
        self.fake = FakeClient()
        self._previous_client = object_store._CLIENT
        object_store._CLIENT = self.fake
        self.addCleanup(self._teardown)
        self.admin = make_user("tv-object-admin@example.com", role="ADMIN")
        self.room = make_room(make_room_type("Gallery object test"), "O-101")
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.base = "/api/rooms-gallery/"

    def _teardown(self):
        object_store._CLIENT = self._previous_client
        self.env.stop()
        shutil.rmtree(self.directory, ignore_errors=True)
        storage._MODE_CACHE.clear()

    def png(self):
        out = __import__("io").BytesIO()
        Image.new("RGB", (40, 30), "blue").save(out, "PNG")
        return out.getvalue()

    def upload(self, name, content, room=True):
        data = {"file": SimpleUploadedFile(name, content)}
        if room:
            data["room_id"] = str(self.room.pk)
        return self.client.post(self.base + "manage/media/", data, format="multipart")


class BackendModeDecisionTests(ObjectBackendTestCase):
    def test_auto_stays_fs_without_object_config(self):
        with patch.dict(os.environ, {"TV_GALLERY_BUCKET_NAME": ""}):
            mode, reason = storage.backend_mode()
        self.assertEqual(mode, "fs")

    def test_auto_stays_fs_when_directory_is_persistent(self):
        with patch.object(storage, "fs_is_persistent", return_value=(True, "mountpoint")), \
             override_settings(TV_GALLERY_STORAGE_BACKEND="auto"):
            mode, reason = storage.backend_mode()
        self.assertEqual(mode, "fs")
        self.assertIn("persistent", reason)

    def test_auto_switches_to_object_for_ephemeral_directory(self):
        with patch.object(storage, "fs_is_persistent",
                          return_value=(False, "same filesystem as /")), \
             override_settings(TV_GALLERY_STORAGE_BACKEND="auto"):
            mode, reason = storage.backend_mode()
        self.assertEqual(mode, "object")
        self.assertIn("NOT on a persistent volume", reason)

    def test_explicit_object_mode_is_object(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            mode, _ = storage.backend_mode()
        self.assertEqual(mode, "object")

    def test_explicit_fs_mode_is_fs_even_with_bucket_configured(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="fs"):
            mode, _ = storage.backend_mode()
        self.assertEqual(mode, "fs")

    def test_object_selected_but_unconfigured_falls_back_to_fs(self):
        with patch.dict(os.environ, {"TV_GALLERY_BUCKET_NAME": ""}), \
             override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            mode, reason = storage.backend_mode()
        self.assertEqual(mode, "fs")
        self.assertIn("not configured", reason)


class FileSystemBehaviourUnchangedTests(ObjectBackendTestCase):
    """fs mode must behave exactly as before the repair (no cache directory)."""

    def test_fs_mode_stores_only_the_named_files(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="fs"):
            key, size = storage.save_chunks([b"hello-world"], 1024)
            self.assertEqual(size, 11)
            path, outcome = storage.resolve(key)
            self.assertEqual(outcome, storage.FILE_OK)
            self.assertEqual(path.read_bytes(), b"hello-world")
            # The resolved directory contains exactly the stored file — the
            # pre-repair contract the existing upload tests assert.
            names = sorted(p.name for p in Path(self.directory).iterdir())
            self.assertEqual(names, [key])
            storage.remove(key)
            self.assertEqual(list(Path(self.directory).iterdir()), [])


class ObjectBackendRoundTripTests(ObjectBackendTestCase):
    def _object_key(self, key):
        return "%s/%s" % (object_store.object_prefix(), key)

    def test_save_resolve_remove_roundtrip_in_object_mode(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            key, size = storage.save_chunks([b"durable-bytes", b"-here"], 1024)
            self.assertEqual(size, len(b"durable-bytes-here"))
            self.assertIn(self._object_key(key), self.fake.objects)  # durable layer
            self.assertNotIn(key, [p.name for p in Path(self.directory).iterdir()])  # not a plain fs file
            path, outcome = storage.resolve(key)
            self.assertEqual(outcome, storage.FILE_OK)
            self.assertEqual(path.read_bytes(), b"durable-bytes-here")  # staged copy
            storage.remove(key)
            self.assertNotIn(self._object_key(key), self.fake.objects)
            self.assertEqual(storage.resolve(key)[1], storage.FILE_MISSING)

    def test_resolve_missing_object_is_honest_404(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            self.assertEqual(storage.resolve("f" * 32)[1], storage.FILE_MISSING)

    def test_resolve_object_store_down_is_honest_503(self):
        self.fake.fail_reads = True
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            self.assertEqual(storage.resolve("a" * 32)[1], storage.FILE_STORAGE_UNAVAILABLE)

    def test_pre_switch_local_file_still_resolves_after_switch(self):
        # A record written while the backend was fs keeps resolving once the
        # deployment switches to object, for as long as the container that
        # still holds the file lives.
        with override_settings(TV_GALLERY_STORAGE_BACKEND="fs"):
            key, _ = storage.save_chunks([b"written-while-fs"], 1024)
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            path, outcome = storage.resolve(key)
            self.assertEqual(outcome, storage.FILE_OK)
            self.assertEqual(path.read_bytes(), b"written-while-fs")

    def test_upload_api_end_to_end_in_object_mode(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            response = self.upload("room.png", self.png())
            self.assertEqual(response.status_code, 201)
            item = GalleryMedia.objects.get()
            # Durable bytes live in the object store, not in MEDIA_ROOT or the
            # public media tree.
            self.assertIn(self._object_key(item.storage_key), self.fake.objects)
            self.assertNotIn(str(settings_media_root()), [
                self._object_key(item.storage_key), item.storage_key])
            self.assertFalse((Path(settings_media_root()) / item.storage_key).exists())
            # The public rendition streams through the API (from the staged copy).
            GalleryRoom.objects.create(room=self.room, active=True)
            served = self.client.get(self.base + "media/%s/display/" % item.pk)
            self.assertEqual(served.status_code, 200)
            self.assertGreater(len(b"".join(served.streaming_content)), 0)
            served.close()
            # The private original is never published without the room opting in.
            self.assertEqual(self.client.get(self.base + "media/%s/file/" % item.pk).status_code, 404)

    def test_duplicate_retry_rule_holds_in_object_mode(self):
        content = self.png()
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            first = self.upload("room.png", content)
            self.assertEqual(first.status_code, 201)
            second = self.upload("room.png", content)
            self.assertEqual(second.status_code, 200)
            self.assertTrue(second.data.get("duplicate"))
            self.assertEqual(GalleryMedia.objects.count(), 1)
            objects_before = len(self.fake.objects)
            # The retry's duplicate bytes were cleaned from the object store.
            self.assertEqual(objects_before, 3)  # original + thumb + display

    def test_media_delete_removes_object_and_cache(self):
        content = self.png()
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            response = self.upload("room.png", content)
            item = GalleryMedia.objects.get()
            # Prime the cache by resolving the rendition, then delete.
            path, outcome = storage.resolve(item.display_key)
            self.assertEqual(outcome, storage.FILE_OK)
            delete = self.client.delete(self.base + "manage/media/%s/" % item.pk)
            self.assertIn(delete.status_code, (200, 204))
            for key in (item.storage_key, item.thumbnail_key, item.display_key):
                self.assertNotIn(self._object_key(key), self.fake.objects)
            self.assertFalse((Path(self.directory) / storage.OBJECT_CACHE_DIR / key).exists())

    def test_local_file_stages_object_for_binary_processing(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            key, _ = storage.save_chunks([b"raw-frame"], 1024)
            with storage.local_file(key) as path:
                self.assertEqual(path.read_bytes(), b"raw-frame")

    def test_check_gallery_storage_command_passes_in_object_mode(self):
        from django.core.management import call_command

        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            call_command("check_gallery_storage", "--skip-media", "--json")

    def test_gallery_media_doctor_reports_object_backend(self):
        from django.core.management import call_command

        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            response = self.upload("room.png", self.png())
            self.assertEqual(response.status_code, 201)
            # The doctor must find every needed rendition readable (exit 0).
            call_command("gallery_media_doctor", "--storage")

    def test_describe_root_names_object_backend(self):
        with override_settings(TV_GALLERY_STORAGE_BACKEND="object"):
            report = storage.describe_root()
        self.assertEqual(report["backend"], "object")
        self.assertEqual(report["object_store"]["bucket"], "jone-gallery-private-test")
        self.assertIn("object", report["durable"])


def settings_media_root():
    from django.conf import settings

    return settings.MEDIA_ROOT
