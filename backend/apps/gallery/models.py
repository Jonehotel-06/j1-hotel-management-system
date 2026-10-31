# apps/gallery/models.py
from django.db import models

from apps.core.validators import validate_image_upload


class GalleryItem(models.Model):
    class Category(models.TextChoices):
        HOTEL = "HOTEL", "Hotel"
        ROOMS = "ROOMS", "Rooms"
        RESTAURANT = "RESTAURANT", "Restaurant"
        FACILITIES = "FACILITIES", "Facilities"
        EVENTS = "EVENTS", "Events"
        EXTERIOR = "EXTERIOR", "Exterior"
        OTHER = "OTHER", "Other"

    class HomepageSection(models.TextChoices):
        # Four independent hero slots rotate on the homepage in this order. The
        # original "hero" key is kept unchanged so the existing hero image and
        # its public URL survive; hero_2..hero_4 are additional, separately
        # stored slots that each have their own upload, replacement and removal.
        HERO = "hero", "Hero"
        HERO_2 = "hero_2", "Hero 2"
        HERO_3 = "hero_3", "Hero 3"
        HERO_4 = "hero_4", "Hero 4"
        WELCOME = "welcome", "Welcome to J-ONE"
        EXPERIENCE = "experience", "Experience"
        FIND_US = "find_us", "Find Us"

    homepage_section = models.CharField(max_length=20, choices=HomepageSection.choices,
                                        unique=True, null=True, blank=True)
    image = models.ImageField(upload_to="gallery/%Y/%m/", validators=[validate_image_upload])
    title = models.CharField(max_length=150, blank=True, default="")
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=20, choices=Category.choices,
                                default=Category.HOTEL, db_index=True)
    alt_text = models.CharField(max_length=200, blank=True, default="")
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "-created_at"]
        verbose_name_plural = "Gallery items"

    def __str__(self):
        return self.title or f"Gallery item #{self.pk}"
