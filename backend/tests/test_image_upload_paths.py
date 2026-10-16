"""Every staff image-upload path, exercised end to end.

Reported symptom: "images are not uploading". This module drives each upload
surface through the same multipart request the browser sends — a real image
body, with the file part named exactly as the backend expects — so a failure is
reproduced here rather than guessed at.

Surfaces covered:

* Gallery media           POST   /api/admin/gallery/                (works)
* Room-type photo         POST   /api/admin/room-types/<id>/images/
* Physical-room photo     POST   /api/admin/rooms/<id>/images/
* Facility photo          POST   /api/admin/facilities/
* Website branding images PATCH  /api/admin/settings/

The last one is the reported failure: the upload endpoint answers 200 while
silently dropping the file, because the website-image fields were removed from
HotelSettings upstream (migration hotel/0004) and the dashboard Settings page
still offers an uploader for them. The test below pins that behaviour so the
frontend cannot promise an upload the backend does not store, and pins that the
Gallery — the surface that DOES drive the public site — keeps accepting files.
"""
from __future__ import annotations

import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.gallery.models import GalleryItem
from apps.hotel.models import HotelSettings
from apps.rooms.models import Room, RoomImage, RoomType, RoomTypeImage


def _image_bytes(fmt="PNG", size=(64, 48)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def _file(name="photo.png", payload=None, content_type="image/png"):
    return SimpleUploadedFile(name, payload or _image_bytes(), content_type=content_type)


@override_settings(MEDIA_ROOT="/tmp/jone-test-media")
class StaffImageUploadTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="upload-admin@jone.test",
            password="Str0ng-Pass!23",
            first_name="Upload",
            last_name="Admin",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(self.admin)
        self.room_type = RoomType.objects.create(
            name="Deluxe Upload", base_price=Decimal("45000.00"), max_guests=2,
        )
        self.room = Room.objects.create(room_type=self.room_type, room_number="UPL-101")

    # ------------------------------- gallery --------------------------------

    def test_gallery_accepts_png_webp_and_jpeg(self):
        for name, fmt, ctype in (
            ("lobby.png", "PNG", "image/png"),
            ("lobby.webp", "WEBP", "image/webp"),
            ("lobby.jpg", "JPEG", "image/jpeg"),
        ):
            with self.subTest(name=name):
                response = self.client.post(
                    "/api/admin/gallery/",
                    {
                        "title": "Lobby " + name,
                        "category": "HOTEL",
                        "alt_text": "Hotel lobby",
                        "is_active": "true",
                        "image": _file(name, _image_bytes(fmt), ctype),
                    },
                    format="multipart",
                )
                self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        self.assertEqual(GalleryItem.objects.count(), 3)

    def test_gallery_rejects_a_disguised_non_image(self):
        response = self.client.post(
            "/api/admin/gallery/",
            {"title": "Fake", "category": "HOTEL", "is_active": "true",
             "image": _file("fake.png", b"not an image at all", "image/png")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(GalleryItem.objects.count(), 0)

    # ------------------------------- rooms ----------------------------------

    def test_room_type_photo_upload(self):
        """Room-TYPE photos live under /api/admin/room-types/ (room-details.html)."""
        response = self.client.post(
            f"/api/admin/room-types/{self.room_type.pk}/images/",
            {"image": _file("deluxe.png"), "is_primary": "true", "caption": "Deluxe room"},
            format="multipart",
        )
        self.assertIn(response.status_code, (200, 201), getattr(response, "data", None))
        self.assertEqual(RoomTypeImage.objects.count(), 1)

    def test_physical_room_photo_upload(self):
        """Physical-ROOM photos live under /api/admin/rooms/ (rooms.html)."""
        response = self.client.post(
            f"/api/admin/rooms/{self.room.pk}/images/",
            {"image": _file("room.png")},
            format="multipart",
        )
        self.assertIn(response.status_code, (200, 201), getattr(response, "data", None))
        self.assertEqual(RoomImage.objects.count(), 1)

    def test_facility_photo_upload(self):
        response = self.client.post(
            "/api/admin/facilities/",
            {"name": "Pool", "description": "Outdoor pool", "image": _file("pool.png")},
            format="multipart",
        )
        self.assertIn(response.status_code, (200, 201), getattr(response, "data", None))

    # --------------------------- website branding ---------------------------

    def test_settings_branding_fields_are_gone_from_the_backend(self):
        """The dashboard must not offer an uploader the backend cannot store.

        HotelSettings no longer carries hero_image/intro_image/… (migration
        hotel/0004 removed them), and the admin serializer has no such field, so
        a PATCH answers 200 and stores nothing. Website images are uploaded to
        the Gallery instead, which the public site reads. If the fields are ever
        restored, this test fails and tells the frontend to surface them again.
        """
        response = self.client.patch("/api/admin/settings/", {"hero_image": _file("hero.png")}, format="multipart")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            hasattr(HotelSettings.get_settings(), "hero_image"),
            "the website-image fields were restored: put the uploader back in Settings",
        )

    def test_gallery_is_what_the_public_site_reads(self):
        self.client.post(
            "/api/admin/gallery/",
            {"title": "Lobby", "category": "HOTEL", "is_active": "true", "image": _file("lobby.png")},
            format="multipart",
        )
        self.client.force_authenticate(None)
        public = self.client.get("/api/gallery/")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(len(public.json()["data"]["items"]), 1)
