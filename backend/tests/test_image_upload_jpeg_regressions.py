"""JPG/JPEG upload regressions — the reported "images are not uploading" bug.

Reported symptom: PNG uploads worked, JPG/JPEG uploads did not. The cause was
never the file input's ``accept`` attribute. There were two independent causes on
the same path:

1. **Browser-side (frontend/js/dashboard.js + the two upload pages).** The
   pre-check compared ``File.type`` against the literal list
   ``["image/jpeg", "image/png", "image/webp"]``. A browser derives that string
   from the file name using the *operating system's* MIME table, so the same
   JPEG arrives as ``image/jpeg``, ``image/jpg``, ``image/pjpeg`` or ``""``
   (unknown) depending on the machine — while a PNG is essentially always
   ``image/png``. Valid photos were therefore rejected in the browser before a
   single byte was sent, and the asymmetry matched the report exactly.

2. **Server-side (apps/core/validators.py + the DRF image field).** The
   validator refused any upload whose *file name* did not end in exactly
   ``.jpg/.jpeg/.png/.webp`` — before looking at the bytes — so a JPEG named
   ``IMG_2041.JFIF``, ``scan.jpe`` or ``blob`` (the name several mobile
   browsers send) was rejected as "Unsupported file extension", and Django's
   own extension validator refused it earlier still.

Both are fixed, and this module pins the whole pipeline end to end: the multipart
request, Pillow decoding, the stored name/extension, the stored bytes and the
public URL — for every surface the audit brief lists (gallery, room-type photo,
physical-room photo, profile photo), plus the security rules that must NOT be
weakened (a renamed non-image is still refused, nothing is silently dropped, and
a failed upload never deletes an existing image).
"""
import io
import os

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.core.validators import validate_image_upload
from apps.gallery.models import GalleryItem
from apps.rooms.models import Room, RoomImage, RoomType, RoomTypeImage

MEDIA_ROOT = "/tmp/jone-jpeg-regression-media"


def image_bytes(fmt="JPEG", size=(320, 240), **save_kwargs):
    from PIL import Image

    buffer = io.BytesIO()
    mode = "CMYK" if save_kwargs.pop("cmyk", False) else "RGB"
    Image.new(mode, size, (180, 40, 40) if mode == "RGB" else (10, 20, 30, 40)).save(
        buffer, format=fmt, **save_kwargs
    )
    return buffer.getvalue()


def upload(name, payload, content_type="image/jpeg"):
    return SimpleUploadedFile(name, payload, content_type=content_type)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class JpegVariantUploadTests(APITestCase):
    """Every real-world JPEG naming/MIME combination reaches storage."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="jpeg-admin@jone.test", password="Str0ng-Pass!23",
            first_name="Jpeg", last_name="Admin", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.admin)
        self.room_type = RoomType.objects.create(name="JPEG Deluxe", base_price="42000.00", max_guests=2)
        self.room = Room.objects.create(room_type=self.room_type, room_number="JP-101")

    # --------------------------- helpers ------------------------------------
    def post_gallery(self, name, payload, content_type="image/jpeg"):
        return self.client.post(
            "/api/admin/gallery/",
            {
                "title": f"Photo {name}",
                "category": "HOTEL",
                "alt_text": "Hotel photo",
                "is_active": "true",
                "image": upload(name, payload, content_type),
            },
            format="multipart",
        )

    def assert_stored_jpeg(self, item):
        self.assertTrue(item.image.name.lower().endswith(".jpg"), item.image.name)
        self.assertTrue(os.path.exists(item.image.path), item.image.path)
        with open(item.image.path, "rb") as handle:
            self.assertEqual(handle.read(3), b"\xff\xd8\xff", "stored bytes are not a JPEG")

    # ----------------------------- cases ------------------------------------
    def test_jpg_and_jpeg_extensions_both_upload(self):
        for name in ("lobby.jpg", "lobby.jpeg", "LOBBY.JPG", "Lobby.JPEG"):
            with self.subTest(name=name):
                res = self.post_gallery(name, image_bytes())
                self.assertEqual(res.status_code, 201, res.content)
                item = GalleryItem.objects.latest("id")
                self.assert_stored_jpeg(item)
                self.assertIn("/media/", res.json()["data"]["image_url"])

    def test_legacy_mime_types_are_accepted(self):
        """image/jpg, image/pjpeg and an empty type are still a real JPEG."""
        for content_type in ("image/jpeg", "image/jpg", "image/pjpeg",
                             "image/x-citrix-jpeg", ""):
            with self.subTest(content_type=content_type or "<empty>"):
                res = self.post_gallery("photo.jpg", image_bytes(), content_type)
                self.assertEqual(res.status_code, 201, res.content)
                self.assert_stored_jpeg(GalleryItem.objects.latest("id"))

    def test_odd_extensions_are_accepted_and_normalised(self):
        """.jfif / .jpe / no extension at all are the same JPEG container."""
        for name in ("IMG_2041.JFIF", "scan.jpe", "photo.pjpeg", "blob", "image"):
            with self.subTest(name=name):
                res = self.post_gallery(name, image_bytes(), "image/jpeg")
                self.assertEqual(res.status_code, 201, res.content)
                self.assert_stored_jpeg(GalleryItem.objects.latest("id"))

    def test_progressive_and_cmyk_jpegs_upload(self):
        cases = {
            "progressive.jpg": image_bytes(progressive=True),
            "cmyk.jpg": image_bytes(cmyk=True),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                res = self.post_gallery(name, payload)
                self.assertEqual(res.status_code, 201, res.content)
                self.assert_stored_jpeg(GalleryItem.objects.latest("id"))

    def test_png_and_webp_still_upload(self):
        for name, fmt, content_type in (
            ("shot.png", "PNG", "image/png"),
            ("shot.webp", "WEBP", "image/webp"),
        ):
            with self.subTest(fmt=fmt):
                res = self.post_gallery(name, image_bytes(fmt), content_type)
                self.assertEqual(res.status_code, 201, res.content)
                item = GalleryItem.objects.latest("id")
                expected = ".png" if fmt == "PNG" else ".webp"
                self.assertTrue(item.image.name.lower().endswith(expected), item.image.name)

    def test_a_png_named_jpg_is_stored_as_the_png_it_really_is(self):
        """Content decides the stored extension — never the client's claim."""
        res = self.post_gallery("actually-a-png.jpg", image_bytes("PNG"), "image/jpeg")
        self.assertEqual(res.status_code, 201, res.content)
        item = GalleryItem.objects.latest("id")
        self.assertTrue(item.image.name.lower().endswith(".png"), item.image.name)
        with open(item.image.path, "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")

    # ---------------------- security is not weakened ------------------------
    def test_a_renamed_non_image_is_still_refused(self):
        for name in ("fake.jpg", "fake.jpeg", "fake.png", "fake"):
            with self.subTest(name=name):
                res = self.post_gallery(name, b"totally not an image", "image/jpeg")
                self.assertEqual(res.status_code, 400, res.content)
                self.assertIn("image", res.content.decode().lower())
        self.assertEqual(GalleryItem.objects.count(), 0)

    def test_an_executable_renamed_jpg_is_refused(self):
        res = self.post_gallery("payload.jpg", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64, "image/jpeg")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(GalleryItem.objects.count(), 0)

    def test_an_oversize_jpeg_is_refused_with_an_actionable_message(self):
        with override_settings(MAX_UPLOAD_MB=1):
            res = self.post_gallery("huge.jpg", image_bytes(size=(2400, 2400), quality=100) * 40)
        self.assertEqual(res.status_code, 400, res.content)
        message = str(res.json()).lower()
        self.assertIn("maximum", message)
        self.assertIn("mb", message)
        self.assertEqual(GalleryItem.objects.count(), 0)

    def test_gif_is_refused_with_a_named_reason(self):
        buffer = io.BytesIO()
        from PIL import Image

        Image.new("RGB", (8, 8)).save(buffer, format="GIF")
        res = self.post_gallery("anim.gif", buffer.getvalue(), "image/gif")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(GalleryItem.objects.count(), 0)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class JpegUploadEverySurfaceTests(APITestCase):
    """The same JPEG works on every upload surface the brief lists."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="jpeg-surface@jone.test", password="Str0ng-Pass!23",
            first_name="Surface", last_name="Admin", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.admin)
        self.room_type = RoomType.objects.create(name="Surface Type", base_price="30000.00", max_guests=2)
        self.room = Room.objects.create(room_type=self.room_type, room_number="SF-201")

    def test_room_type_and_room_photo_accept_jpeg_variants(self):
        res = self.client.post(
            f"/api/admin/room-types/{self.room_type.pk}/images/",
            {"image": upload("type.jpg", image_bytes(), "image/jpg"), "is_primary": "true"},
            format="multipart",
        )
        self.assertIn(res.status_code, (200, 201), res.content)
        self.assertEqual(RoomTypeImage.objects.count(), 1)

        res = self.client.post(
            f"/api/admin/rooms/{self.room.pk}/images/",
            {"images": upload("room.JPEG", image_bytes(), "image/jpeg")},
            format="multipart",
        )
        self.assertIn(res.status_code, (200, 201), res.content)
        self.assertEqual(RoomImage.objects.count(), 1)
        self.assertTrue(RoomImage.objects.first().image.name.lower().endswith(".jpg"))

    def test_room_type_cover_image_patch_accepts_jpeg(self):
        res = self.client.patch(
            f"/api/admin/room-types/{self.room_type.pk}/",
            {"image": upload("cover.jfif", image_bytes(), "image/jpeg")},
            format="multipart",
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_profile_photo_accepts_jpeg_variants(self):
        for name, content_type in (("me.jpg", "image/jpeg"), ("me.jpeg", "image/jpg"),
                                   ("me.JFIF", "image/pjpeg"), ("blob", "")):
            with self.subTest(name=name):
                res = self.client.patch(
                    "/api/auth/profile/",
                    {"profile_image": upload(name, image_bytes(), content_type)},
                    format="multipart",
                )
                self.assertEqual(res.status_code, 200, res.content)
                self.assertTrue(res.json()["data"]["profile_image_stored"])

    def test_a_rejected_upload_never_removes_an_existing_photo(self):
        first = self.client.post(
            f"/api/admin/rooms/{self.room.pk}/images/",
            {"images": upload("keep.jpg", image_bytes())},
            format="multipart",
        )
        self.assertIn(first.status_code, (200, 201), first.content)
        original = RoomImage.objects.get()
        original_name = original.image.name

        bad = self.client.post(
            f"/api/admin/rooms/{self.room.pk}/images/",
            {"images": upload("bad.jpg", b"not an image")},
            format="multipart",
        )
        self.assertEqual(bad.status_code, 400, bad.content)
        self.assertEqual(RoomImage.objects.count(), 1)
        self.assertEqual(RoomImage.objects.get().image.name, original_name)
        self.assertTrue(os.path.exists(RoomImage.objects.get().image.path))


class ValidatorUnitTests(APITestCase):
    """The validator's contract, without any HTTP layer in the way."""

    def test_validator_normalises_the_name_to_the_real_format(self):
        for name, expected in (("a.jpg", ".jpg"), ("b.JPEG", ".jpg"), ("c.jfif", ".jpg"),
                               ("d", ".jpg"), ("e.png", ".png"), ("f.webp", ".webp")):
            with self.subTest(name=name):
                fmt = {"e.png": "PNG", "f.webp": "WEBP"}.get(name, "JPEG")
                payload = image_bytes(fmt)
                file = upload(name, payload, "image/jpeg")
                validate_image_upload(file)
                self.assertTrue(file.name.lower().endswith(expected), file.name)

    def test_validator_never_accepts_a_disguised_file(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            validate_image_upload(upload("evil.jpg", b"<html>not an image</html>"))

    def test_zero_byte_file_is_named_as_empty(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            validate_image_upload(upload("empty.jpg", b""))
        self.assertIn("empty", str(ctx.exception).lower())
