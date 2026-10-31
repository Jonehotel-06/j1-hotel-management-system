"""Homepage hero: four independently managed slots (Task 1, batch 04)."""
import io
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.gallery.models import GalleryItem

HERO_SLOTS = ["hero", "hero_2", "hero_3", "hero_4"]


def jpeg(name="photo.jpg", colour="navy", size=(64, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def png(name="photo.png"):
    buf = io.BytesIO()
    Image.new("RGB", (32, 20), "green").save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class HeroSlotTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_user("hero-admin@example.com", "password", first_name="A", last_name="B", role="ADMIN")
        self.guest = User.objects.create_user("hero-guest@example.com", "password", first_name="G", last_name="B", role="GUEST")
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def url(self, slot):
        return f"/api/admin/gallery/homepage/{slot}/"

    def upload(self, slot, file=None, alt=""):
        return self.api.put(self.url(slot), {"image": file or jpeg(f"{slot}.jpg"), "alt_text": alt}, format="multipart")

    def stored_name(self, slot):
        return GalleryItem.objects.get(homepage_section=slot).image.name

    def test_four_slots_upload_independently_and_publish_together(self):
        for index, slot in enumerate(HERO_SLOTS):
            response = self.upload(slot, alt=f"Hero {index + 1}")
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(GalleryItem.objects.filter(homepage_section__in=HERO_SLOTS).count(), 4)
        public = APIClient().get("/api/gallery/homepage/")
        self.assertEqual(public.status_code, 200)
        for slot in HERO_SLOTS:
            self.assertIn(slot, public.data)
            self.assertTrue(public.data[slot]["image_url"].endswith(".webp"))
        self.assertEqual([public.data[slot]["alt_text"] for slot in HERO_SLOTS], ["Hero 1", "Hero 2", "Hero 3", "Hero 4"])

    def test_replacing_one_slot_leaves_the_other_three_untouched(self):
        for slot in HERO_SLOTS:
            self.upload(slot)
        before = {slot: self.stored_name(slot) for slot in HERO_SLOTS}
        replacement = self.api.put(self.url("hero_3"), {"image": jpeg("new.jpg", colour="red")}, format="multipart")
        self.assertEqual(replacement.status_code, 200, replacement.data)
        self.assertNotEqual(self.stored_name("hero_3"), before["hero_3"])
        for slot in ["hero", "hero_2", "hero_4"]:
            self.assertEqual(self.stored_name(slot), before[slot], slot)
        self.assertEqual(GalleryItem.objects.filter(homepage_section__in=HERO_SLOTS).count(), 4)

    def test_deleting_one_slot_leaves_the_other_three_and_the_built_in_fallback(self):
        for slot in HERO_SLOTS:
            self.upload(slot)
        before = {slot: self.stored_name(slot) for slot in HERO_SLOTS}
        self.assertEqual(self.api.delete(self.url("hero_2")).status_code, 204)
        public = APIClient().get("/api/gallery/homepage/").data
        self.assertNotIn("hero_2", public)
        for slot in ["hero", "hero_3", "hero_4"]:
            self.assertIn(slot, public)
            self.assertEqual(self.stored_name(slot), before[slot])

    def test_invalid_format_and_oversize_are_rejected_without_touching_the_slot(self):
        self.upload("hero_4")
        before = self.stored_name("hero_4")
        gif = SimpleUploadedFile("hero.gif", b"GIF89a", content_type="image/gif")
        self.assertEqual(self.api.put(self.url("hero_4"), {"image": gif}, format="multipart").status_code, 400)
        fake = SimpleUploadedFile("hero.jpg", b"not an image at all", content_type="image/jpeg")
        self.assertEqual(self.api.put(self.url("hero_4"), {"image": fake}, format="multipart").status_code, 400)
        with override_settings(MAX_UPLOAD_MB=0):
            self.assertEqual(self.api.put(self.url("hero_4"), {"image": png()}, format="multipart").status_code, 400)
        self.assertEqual(self.stored_name("hero_4"), before)

    def test_unknown_hero_slot_is_not_found_and_guests_are_refused(self):
        self.assertEqual(self.api.put("/api/admin/gallery/homepage/hero_5/", {"image": jpeg()}, format="multipart").status_code, 404)
        self.api.force_authenticate(self.guest)
        self.assertIn(self.api.put(self.url("hero_2"), {"image": jpeg()}, format="multipart").status_code, (401, 403))
        self.assertIn(self.api.delete(self.url("hero_2")).status_code, (401, 403))
        self.assertFalse(GalleryItem.objects.filter(homepage_section="hero_2").exists())

    def test_failed_storage_write_keeps_the_published_slot(self):
        self.upload("hero_3")
        before = self.stored_name("hero_3")
        with patch("apps.gallery.homepage.verify_stored_upload", return_value=(False, "Storage unavailable")):
            response = self.api.put(self.url("hero_3"), {"image": jpeg("broken.jpg")}, format="multipart")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.stored_name("hero_3"), before)

    def test_hero_slots_stay_out_of_the_public_gallery_listing(self):
        for slot in HERO_SLOTS:
            self.upload(slot)
        listing = APIClient().get("/api/gallery/").data["data"]["items"]
        self.assertEqual(listing, [])
        admin_listing = self.api.get("/api/admin/gallery/").data
        self.assertNotIn("hero_2", str(admin_listing))
