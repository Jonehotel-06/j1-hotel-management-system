# tests/test_media_storage.py
"""Media-storage tests: the configured backend must really persist bytes.

These cover the failure that made Backblaze B2 uploads silently land on the
a hosted container's ephemeral disk instead of the bucket:

* ``STORAGES`` must be the single source of truth and must be honoured at
  runtime (a mis-set ``DEFAULT_FILE_STORAGE`` would be ignored by Django 5).
* ``storage.save()`` must round-trip through whatever backend is configured.
* The S3/B2 client must be built with checksum calculation set to
  ``when_required``; botocore >= 1.36 otherwise sends
  ``x-amz-sdk-checksum-algorithm`` on every PutObject and B2 answers
  ``400 InvalidArgument``, which is exactly why nothing reached the bucket.
* Uploading through the API must store the file via the configured backend and
  return a usable absolute URL.
"""
import io

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.test import SimpleTestCase, override_settings
from PIL import Image

from apps.accounts.models import User
from apps.core.storage import absolute_media_url, is_remote_storage, storage_backend_label
from apps.rooms.models import RoomTypeImage

from .base import BaseAPITestCase
from .factories import make_room_type, make_staff


def png_bytes(size=(12, 12), color=(10, 90, 140)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class StorageRoundTripTests(SimpleTestCase):
    """storage.save() must actually write, read back and delete."""

    def test_default_storage_round_trip(self):
        name = default_storage.save("tests/storage-probe.txt", ContentFile(b"jone-storage-probe"))
        try:
            self.assertTrue(default_storage.exists(name))
            with default_storage.open(name) as fh:
                self.assertEqual(fh.read(), b"jone-storage-probe")
            self.assertTrue(default_storage.url(name))
        finally:
            default_storage.delete(name)
        self.assertFalse(default_storage.exists(name))

    def test_image_round_trip_preserves_bytes(self):
        payload = png_bytes()
        name = default_storage.save("tests/storage-probe.png", ContentFile(payload))
        try:
            with default_storage.open(name) as fh:
                self.assertEqual(fh.read(), payload)
        finally:
            default_storage.delete(name)

    def test_storages_setting_is_authoritative(self):
        """Django 5 reads STORAGES; a stale DEFAULT_FILE_STORAGE must not decide."""
        self.assertIn("default", settings.STORAGES)
        self.assertIn("staticfiles", settings.STORAGES)
        # The live object must match the configured backend, i.e. the setting
        # is not being silently overridden somewhere else.
        live = storages["default"].__class__
        self.assertEqual(live.__module__ + "." + live.__name__, storage_backend_label())

    def test_backend_label_resolves_real_class(self):
        """The diagnostic must name a real backend, never a lazy wrapper."""
        label = storage_backend_label()
        self.assertNotIn("LazyObject", label)
        self.assertNotEqual(label, "builtins.object")
        self.assertIn(".", label)


class B2ClientConfigTests(SimpleTestCase):
    """Guards the actual root cause of the B2 outage.

    configure_b2_media_storage() reads os.environ, so each case runs with a
    controlled environment and an isolated STORAGES dict.
    """

    B2_ENV = {
        "BACKBLAZE_KEY_ID": "test-key-id",
        "BACKBLAZE_APPLICATION_KEY": "test-app-key",
        "BACKBLAZE_BUCKET_NAME": "jone-media-test",
        "BACKBLAZE_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
    }

    def _configure(self, **overrides):
        import os
        from unittest import mock

        from config.settings.base import configure_b2_media_storage

        env = dict(self.B2_ENV)
        env.update(overrides)
        env = {k: v for k, v in env.items() if v is not None}
        storages_map = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
        keys = set(self.B2_ENV) | {"BACKBLAZE_REGION", "MEDIA_CUSTOM_DOMAIN"}
        clean = {k: os.environ.pop(k, None) for k in keys}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                result = configure_b2_media_storage(storages_map)
        finally:
            for k, v in clean.items():
                if v is not None:
                    os.environ[k] = v
        return storages_map, result

    def test_checksums_are_only_calculated_when_required(self):
        """botocore >= 1.36 otherwise sends headers B2 rejects with 400."""
        _, result = self._configure()
        cfg = result["settings"]["AWS_S3_CLIENT_CONFIG"]
        self.assertEqual(cfg.request_checksum_calculation, "when_required")
        self.assertEqual(cfg.response_checksum_validation, "when_required")

    def test_signature_and_addressing_suit_b2(self):
        _, result = self._configure()
        cfg = result["settings"]["AWS_S3_CLIENT_CONFIG"]
        self.assertEqual(cfg.signature_version, "s3v4")
        self.assertEqual(cfg.s3.get("addressing_style"), "virtual")

    def test_b2_configuration_selects_s3_backend(self):
        storages_map, result = self._configure()
        self.assertTrue(result["enabled"])
        backend = storages_map["default"]["BACKEND"]
        self.assertIn(backend, ("storages.backends.s3boto3.S3Boto3Storage", "storages.backends.s3.S3Storage"))
        settings_out = result["settings"]
        self.assertEqual(settings_out["AWS_STORAGE_BUCKET_NAME"], "jone-media-test")
        self.assertEqual(settings_out["AWS_S3_ENDPOINT_URL"], "https://s3.us-west-004.backblazeb2.com")
        # Never sign with querystring auth for a public media bucket.
        self.assertFalse(settings_out["AWS_QUERYSTRING_AUTH"])

    def test_region_is_derived_from_endpoint(self):
        """A blank region made botocore default to us-east-1 -> B2 rejects the signature."""
        _, result = self._configure()
        self.assertEqual(result["settings"]["AWS_S3_REGION_NAME"], "us-west-004")

    def test_explicit_region_overrides_derivation(self):
        _, result = self._configure(BACKBLAZE_REGION="eu-central-003")
        self.assertEqual(result["settings"]["AWS_S3_REGION_NAME"], "eu-central-003")

    def test_endpoint_without_scheme_is_normalised(self):
        """boto3 builds a malformed URL when the scheme is missing."""
        _, result = self._configure(BACKBLAZE_ENDPOINT="s3.us-west-004.backblazeb2.com")
        self.assertEqual(result["settings"]["AWS_S3_ENDPOINT_URL"], "https://s3.us-west-004.backblazeb2.com")

    def test_incomplete_b2_configuration_is_not_silently_accepted(self):
        """Missing credentials must leave the default backend untouched."""
        storages_map, result = self._configure(BACKBLAZE_KEY_ID="")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["settings"], {})
        self.assertEqual(storages_map["default"]["BACKEND"], "django.core.files.storage.FileSystemStorage")


class MediaUrlTests(SimpleTestCase):
    def test_absolute_media_url_handles_empty_field(self):
        self.assertIsNone(absolute_media_url(None, None))

    @override_settings(MEDIA_PUBLIC_BASE_URL="https://api.example.com")
    def test_relative_url_is_absolutised_without_request(self):
        """The frontend is hosted separately, so URLs must never be relative."""
        class _Field:
            url = "/media/room-types/x.png"

            def __bool__(self):
                return True

        self.assertEqual(absolute_media_url(_Field(), None), "https://api.example.com/media/room-types/x.png")

    def test_remote_url_is_passed_through_unchanged(self):
        class _Field:
            url = "https://bucket.s3.us-west-004.backblazeb2.com/room-types/x.png"

            def __bool__(self):
                return True

        self.assertEqual(absolute_media_url(_Field(), None), _Field.url)


class RoomTypeImageUploadTests(BaseAPITestCase):
    """The whole pipeline: multipart -> parser -> serializer -> storage -> URL."""

    def setUp(self):
        super().setUp()
        self.manager = make_staff("storage-manager@jone.dev", role=User.Role.MANAGER)
        self.auth(self.manager)

    @staticmethod
    def payload(response):
        """The JSON envelope is applied by the renderer, so DRF's response.data
        is the bare serializer payload here."""
        body = response.data
        return body.get("data", body) if isinstance(body, dict) and "success" in body else body

    def _upload(self, name="cover.png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, png_bytes(), content_type="image/png")

    def test_create_room_type_with_image_persists_to_storage(self):
        r = self.client.post("/api/admin/room-types/", {
            "name": "Storage Suite",
            "base_price": "50000.00",
            "max_guests": 2,
            "image": self._upload(),
        }, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)

        image = RoomTypeImage.objects.get(room_type__name="Storage Suite")
        # The bytes must be retrievable through the configured backend.
        self.assertTrue(default_storage.exists(image.image.name))
        with default_storage.open(image.image.name) as fh:
            self.assertEqual(fh.read(), png_bytes())
        self.assertTrue(image.is_primary)

        # And the API must hand the frontend a usable absolute URL.
        url = self.payload(r)["primary_image_url"]
        self.assertTrue(url.startswith("http://") or url.startswith("https://"), url)

    def test_image_replacement_updates_primary(self):
        rt = make_room_type(name="Replace Me")
        first = self.client.post(f"/api/admin/room-types/{rt.id}/", {"image": self._upload("a.png")},
                                 format="multipart")
        self.assertIn(first.status_code, (200, 405))
        r = self.client.patch(f"/api/admin/room-types/{rt.id}/", {"image": self._upload("b.png")},
                              format="multipart")
        self.assertEqual(r.status_code, 200, r.data)
        primaries = RoomTypeImage.objects.filter(room_type=rt, is_primary=True, is_active=True)
        self.assertEqual(primaries.count(), 1)
        self.assertTrue(default_storage.exists(primaries.first().image.name))

    def test_invalid_upload_is_rejected_before_storage(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("evil.png", b"definitely not a png", content_type="image/png")
        r = self.client.post("/api/admin/room-types/", {
            "name": "Bad Upload", "base_price": "1000.00", "max_guests": 1, "image": bad,
        }, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("image", r.data["errors"])
        # No orphan row and no orphan object.
        self.assertFalse(RoomTypeImage.objects.filter(room_type__name="Bad Upload").exists())

    def test_json_requests_still_work(self):
        """Adding multipart parsers must not break existing JSON consumers."""
        r = self.client.post("/api/admin/room-types/", {
            "name": "Json Suite", "base_price": "42000.00", "max_guests": 2,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(self.payload(r)["primary_image_url"])


class StorageDiagnosticsTests(SimpleTestCase):
    def test_is_remote_storage_matches_backend(self):
        remote = is_remote_storage()
        label = storage_backend_label()
        self.assertEqual(remote, "S3" in label or "s3" in label)

    def test_check_media_storage_command_reports_backend(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("check_media_storage", stdout=out, stderr=out)
        output = out.getvalue()
        self.assertIn("active backend", output)
        self.assertIn(storage_backend_label(), output)

    def test_check_media_storage_never_prints_credential_values(self):
        """Diagnostics may report presence, never the secrets themselves."""
        import os
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        secret_id, secret_key = "SUPER-SECRET-KEY-ID", "SUPER-SECRET-APP-KEY"
        out = StringIO()
        with mock.patch.dict(os.environ, {
            "BACKBLAZE_KEY_ID": secret_id,
            "BACKBLAZE_APPLICATION_KEY": secret_key,
        }, clear=False):
            call_command("check_media_storage", stdout=out, stderr=out)
        output = out.getvalue()
        self.assertNotIn(secret_id, output)
        self.assertNotIn(secret_key, output)
        if is_remote_storage():
            # Presence is reported for the remote backend, values never are.
            self.assertIn("key_id=", output)
