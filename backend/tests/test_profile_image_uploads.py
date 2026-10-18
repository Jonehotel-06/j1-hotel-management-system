"""Profile-photo uploads, end to end, for the two surfaces that accept one.

Reported symptom (again after the image-upload investigation): "profile images
are not uploading and it doesn't display any error message why it fails".

A silent failure has two possible shapes and both are pinned here:

1. the request answers 200 but the image is not stored — the client then shows
   a success message while the photo never appears (the failure this module
   exists to rule out); and
2. a file the backend will not accept is refused *without* a usable message —
   the client then has nothing to show the user.

Surfaces covered:

* Own profile photo   PATCH /api/auth/profile/            (self-service, any staff)
* Staff photo         PATCH /api/admin/users/<id>/        (administrator editing a staff member)

The upload must come back with a usable ``profile_image_url`` and the stored
file must exist in the media storage; the serializer must answer with a field
error the dashboard renders inline when the file is not an image.
"""
from __future__ import annotations

import io

from django.core.files.storage import FileSystemStorage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User

MEDIA = "/tmp/jone-test-profile-media"


class _UnverifiableStorage(FileSystemStorage):
    """Writes succeed; asking the backend about the object fails.

    This is what a media backend with broken credentials or no network route
    behaves like, and it must not turn into a silent "saved".
    """

    def get_available_name(self, name, max_length=None):  # noqa: D102 - inherited contract
        return name

    def exists(self, name):  # noqa: D102 - inherited contract
        raise RuntimeError("no route to bucket")


class _MissingObjectStorage(FileSystemStorage):
    """Writes "succeed"; the stored object cannot be found afterwards."""

    def get_available_name(self, name, max_length=None):  # noqa: D102
        return name

    def exists(self, name):  # noqa: D102
        return False


def _image_bytes(fmt="PNG", size=(48, 48)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (12, 90, 160)).save(buffer, format=fmt)
    return buffer.getvalue()


def _file(name="avatar.png", payload=None, content_type="image/png"):
    return SimpleUploadedFile(name, payload or _image_bytes(), content_type=content_type)


def _cleanup():
    import shutil

    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class ProfileImageUploadTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="photo-admin@jone.test",
            password="Str0ng-Pass!23",
            first_name="Photo",
            last_name="Admin",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff = User.objects.create_user(
            email="photo-staff@jone.test",
            password="Str0ng-Pass!23",
            first_name="Photo",
            last_name="Staff",
            role=User.Role.RECEPTIONIST,
        )

    def tearDown(self):
        _cleanup()

    # ------------------------- own profile (self-service) -------------------

    def test_a_staff_member_can_save_their_own_profile_photo(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            "/api/auth/profile/",
            {"profile_image": _file("me.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"]
        # 200 must MEAN stored: a success answer with no url is the silent
        # failure the user reported.
        self.assertTrue(payload["profile_image_url"], "profile_image_url came back empty on a 200")

        self.staff.refresh_from_db()
        self.assertTrue(self.staff.profile_image, "the photo was not written to the user record")
        self.assertTrue(
            payload["profile_image_url"].endswith(self.staff.profile_image.name.rsplit("/", 1)[-1]),
            payload["profile_image_url"],
        )
        self.assertTrue(default_storage.exists(self.staff.profile_image.name))
        with default_storage.open(self.staff.profile_image.name) as stored:
            self.assertTrue(stored.read().startswith(b"\x89PNG"))

    def test_a_non_image_is_refused_with_a_message_the_ui_can_show(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            "/api/auth/profile/",
            {"profile_image": _file("notes.png", payload=b"this is not an image", content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json().get("errors") or {}
        self.assertIn("profile_image", errors)
        self.assertTrue(str(errors["profile_image"][0]).strip())
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.profile_image, "a refused upload must not touch the stored photo")

    def test_the_other_profile_fields_still_save_on_their_own(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            "/api/auth/profile/", {"first_name": "Amina", "phone": "08030000000"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.first_name, "Amina")
        self.assertEqual(self.staff.phone, "08030000000")

    # --------------------- administrator editing a staff photo ---------------

    def test_an_administrator_can_set_a_staff_members_photo(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            f"/api/admin/users/{self.staff.pk}/",
            {"profile_image": _file("worker.png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.profile_image)
        self.assertTrue(default_storage.exists(self.staff.profile_image.name))

    def test_the_staff_list_publishes_the_stored_photo_url(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(
            f"/api/admin/users/{self.staff.pk}/",
            {"profile_image": _file("worker.png")},
            format="multipart",
        )
        listed = self.client.get("/api/admin/users/", {"search": "photo-staff@jone.test"})
        self.assertEqual(listed.status_code, 200, listed.content)
        rows = [
            row for row in listed.json()["data"]
            if str(row.get("email", "")).lower() == "photo-staff@jone.test"
        ]
        self.assertTrue(rows, "the staff member was not returned by the users list")
        self.assertTrue(
            rows[0].get("profile_image_url"),
            _dump(self.staff),
        )

    def test_a_second_photo_replaces_the_first(self):
        self.client.force_authenticate(self.staff)
        self.client.patch("/api/auth/profile/", {"profile_image": _file("one.png")}, format="multipart")
        self.staff.refresh_from_db()
        first = self.staff.profile_image.name
        self.client.patch("/api/auth/profile/", {"profile_image": _file("two.png")}, format="multipart")
        self.staff.refresh_from_db()
        self.assertNotEqual(self.staff.profile_image.name, first, "the replacement photo was not stored")
        self.assertTrue(default_storage.exists(self.staff.profile_image.name))


def _dump(user):
    return {"profile_image": str(user.profile_image), "pk": user.pk}


class ProfilePhotoHonestyTests(ProfileImageUploadTests):
    """The uploader must never answer "saved" for a photo nothing can serve.

    Reported: "profile images are not uploading and it doesn't display any error
    message why it fails". These tests pin the two halves of the answer: the API
    says what happened to the file (``profile_image_stored`` + a readable
    ``profile_image_warning``), and a storage backend that refuses the write
    fails the request with a message instead of a false success.
    """

    def test_a_stored_photo_is_confirmed_in_the_response(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            "/api/auth/profile/", {"profile_image": _file("ok.png")}, format="multipart"
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIs(data["profile_image_stored"], True)
        self.assertEqual(data["profile_image_warning"], "")

    def test_a_photo_the_storage_does_not_hold_is_reported_as_not_stored(self):
        from unittest.mock import patch

        from django.core.files.storage import default_storage

        self.client.force_authenticate(self.staff)
        with override_settings(
            STORAGES={"default": {"BACKEND": "tests.test_profile_image_uploads._MissingObjectStorage"}}
        ):
            response = self.client.patch(
                "/api/auth/profile/", {"profile_image": _file("ghost.png")}, format="multipart"
            )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIs(data["profile_image_stored"], False)
        self.assertIn("storage", data["profile_image_warning"].lower())
        # The message names what the administrator has to look at.
        self.assertIn("Storage in use:", data["profile_image_warning"])

    def test_a_storage_that_cannot_be_checked_is_reported_with_its_reason(self):
        from unittest.mock import patch

        from django.core.files.storage import default_storage

        self.client.force_authenticate(self.staff)
        with override_settings(
            STORAGES={"default": {"BACKEND": "tests.test_profile_image_uploads._UnverifiableStorage"}}
        ):
            response = self.client.patch(
                "/api/auth/profile/", {"profile_image": _file("unchecked.png")}, format="multipart"
            )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIs(data["profile_image_stored"], False)
        self.assertIn("RuntimeError", data["profile_image_warning"])
        # A credential must never leak into a user-facing message.
        self.assertNotIn("no route to bucket", data["profile_image_warning"])


    def test_the_admin_edit_reports_the_same_photo_state(self):
        self.client.force_authenticate(self.admin)
        ok = self.client.patch(
            f"/api/admin/users/{self.staff.pk}/", {"profile_image": _file("worker.png")}, format="multipart"
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertIs(ok.json()["data"]["profile_image_stored"], True)

        with override_settings(
            STORAGES={"default": {"BACKEND": "tests.test_profile_image_uploads._MissingObjectStorage"}}
        ):
            bad = self.client.patch(
                f"/api/admin/users/{self.staff.pk}/", {"profile_image": _file("worker2.png")}, format="multipart"
            )
        self.assertEqual(bad.status_code, 200, bad.content)
        self.assertIs(bad.json()["data"]["profile_image_stored"], False)
        self.assertTrue(bad.json()["data"]["profile_image_warning"])

    def test_a_save_without_a_photo_says_nothing_about_photos(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch("/api/auth/profile/", {"first_name": "Bala"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertNotIn("profile_image_stored", data)
        self.assertNotIn("profile_image_warning", data)

    @override_settings(DEBUG=False)
    def test_uploaded_photo_is_served_in_production_mode_via_media_and_api_media(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            "/api/auth/profile/", {"profile_image": _file("prod-avatar.png")}, format="multipart"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.staff.refresh_from_db()
        rel_name = self.staff.profile_image.name
        self.assertTrue(rel_name)

        for route in (f"/media/{rel_name}", f"/api/media/{rel_name}"):
            res = self.client.get(route)
            self.assertEqual(res.status_code, 200, f"{route} failed in DEBUG=False")
            self.assertEqual(res["Content-Type"], "image/png")
            self.assertEqual(res["Cross-Origin-Resource-Policy"], "cross-origin")
            self.assertTrue(res.content.startswith(b"\x89PNG"))

    def test_failed_replacement_photo_preserves_previous_valid_image(self):
        self.client.force_authenticate(self.staff)
        first_res = self.client.patch(
            "/api/auth/profile/", {"profile_image": _file("original-valid.png")}, format="multipart"
        )
        self.assertEqual(first_res.status_code, 200, first_res.content)
        self.staff.refresh_from_db()
        original_name = self.staff.profile_image.name
        self.assertTrue(original_name)

        with override_settings(
            STORAGES={"default": {"BACKEND": "tests.test_profile_image_uploads._MissingObjectStorage"}}
        ):
            failed_res = self.client.patch(
                "/api/auth/profile/", {"profile_image": _file("broken-replacement.png")}, format="multipart"
            )
        self.assertEqual(failed_res.status_code, 200, failed_res.content)
        self.assertIs(failed_res.json()["data"]["profile_image_stored"], False)
        self.staff.refresh_from_db()
        self.assertEqual(
            self.staff.profile_image.name,
            original_name,
            "Failed replacement upload must preserve the previous valid profile image",
        )


class RefusedProfilePhotoWriteTests(TransactionTestCase):
    """A media backend that refuses the write must fail loudly and store nothing.

    A ``TransactionTestCase`` because the failing write rolls the request back:
    the assertions are about what the database holds afterwards, which needs a
    real rollback rather than a savepoint inside one wrapping test transaction.
    """

    def setUp(self):
        _cleanup()
        # APIClient on purpose: force_authenticate is what the other cases use,
        # and this stays the same kind of request.
        self.client = APIClient()
        self.staff = User.objects.create_user(
            email="photo-refused@jone.test",
            password="Str0ng-Pass!23",
            first_name="Refused",
            last_name="Photo",
            role=User.Role.RECEPTIONIST,
        )

    def tearDown(self):
        _cleanup()

    def test_a_refused_write_fails_loudly_and_changes_nothing(self):
        from unittest.mock import patch

        from django.core.files.storage import default_storage

        self.client.force_authenticate(self.staff)
        with patch.object(default_storage, "save", side_effect=RuntimeError("bucket denied the write")):
            response = self.client.patch(
                "/api/auth/profile/", {"profile_image": _file("denied.png")}, format="multipart"
            )
        self.assertEqual(response.status_code, 502, response.content)
        body = response.json()
        self.assertEqual(body["code"], "STORAGE_UPLOAD_FAILED")
        self.assertTrue(str(body["message"]).strip())
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.profile_image, "a refused upload must not be recorded")
