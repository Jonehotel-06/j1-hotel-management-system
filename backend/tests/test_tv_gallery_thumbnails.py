from io import BytesIO
from unittest import TestCase

from PIL import Image
from apps.tv_rooms_gallery.thumbnails import ThumbnailError, make_thumbnail


def image_bytes(fmt, size=(120, 80), color="red"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt)
    return output.getvalue()


class GalleryThumbnailTests(TestCase):
    def test_supported_formats_are_reencoded_as_bounded_jpeg(self):
        for fmt in ("JPEG", "PNG", "GIF", "WEBP"):
            with self.subTest(fmt=fmt):
                result = make_thumbnail(image_bytes(fmt), size=(60, 60))
                with Image.open(BytesIO(result)) as thumbnail:
                    self.assertEqual(thumbnail.format, "JPEG")
                    self.assertEqual(thumbnail.size, (60, 40))
                    self.assertFalse(thumbnail.info.get("exif"))

    def test_unsupported_and_corrupted_data_are_rejected(self):
        for data in (b"<script>alert(1)</script>", b"\x89PNG\r\n\x1a\ncorrupt", b""):
            with self.subTest(data=data), self.assertRaises(ThumbnailError):
                make_thumbnail(data)

    def test_byte_and_pixel_limits_are_enforced(self):
        data = image_bytes("PNG", (100, 100))
        with self.assertRaises(ThumbnailError):
            make_thumbnail(data, max_bytes=10)
        with self.assertRaises(ThumbnailError):
            make_thumbnail(data, max_pixels=9999)

    def test_transparency_composited_on_white(self):
        out = BytesIO()
        Image.new("RGBA", (5, 5), (0, 0, 0, 0)).save(out, format="PNG")
        result = make_thumbnail(out.getvalue())
        with Image.open(BytesIO(result)) as img:
            self.assertGreater(min(img.getpixel((0, 0))), 245)

    def test_invalid_configuration_is_not_a_media_error(self):
        with self.assertRaises(ValueError):
            make_thumbnail(b"x", max_pixels=0)
