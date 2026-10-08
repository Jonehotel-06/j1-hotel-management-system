# apps/hotel/models.py
"""Configurable hotel content and operational settings.

These models exist so hotel facts/rules are DATA (editable by an admin), not
hard-coded in the frontend or spread across code.
"""
from decimal import Decimal
from datetime import time

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.validators import validate_image_upload

SETTINGS_CACHE_KEY = "hotel:settings:instance"


class HotelSettings(models.Model):
    """Singleton row (pk=1) holding hotel identity and business rules."""

    # Identity / contact
    hotel_name = models.CharField(max_length=150, default="J-ONE HOTEL & LODGE")
    tagline = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    address = models.CharField(max_length=255, default="Plot 566 Mgbowo Street, off Ezike Street")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=100, default="Nigeria")
    phone = models.CharField(max_length=30, default="+234803 211 2874")
    email = models.EmailField(default="jonathanonu76@gmail.com")
    google_maps_url = models.URLField(blank=True, default="")
    # A Google Maps URL is NOT automatically a review URL — this is the
    # dedicated "write a review" link (e.g. https://g.page/r/…/review).
    # Optional: the frontend only shows the Google CTA when it is configured.
    google_review_url = models.URLField(
        blank=True, default="",
        help_text="Direct 'write a review' link for the hotel's Google Business profile. "
                  "Leave blank to hide the Google review button.",
    )
    social_links = models.JSONField(
        default=list, blank=True,
        help_text='List of {"platform": "facebook", "url": "https://..."} entries.',
    )

    # Stay rules
    check_in_time = models.TimeField(default=time(14, 0))
    check_out_time = models.TimeField(default=time(12, 0))
    min_stay_nights = models.PositiveSmallIntegerField(default=1)
    max_stay_nights = models.PositiveSmallIntegerField(default=30)
    currency = models.CharField(max_length=3, default="NGN")

    # Money rules
    tax_rate_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    service_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Flat fee applied once per booking.",
    )
    deposit_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("100.00"),
        validators=[MinValueValidator(Decimal("1.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Percentage of the total required to confirm a booking online.",
    )

    # Booking lifecycle rules
    pending_booking_minutes = models.PositiveIntegerField(
        default=15, help_text="How long an unpaid pending booking holds inventory."
    )
    cancellation_deadline_hours = models.PositiveIntegerField(
        default=48, help_text="Guests may cancel up to this many hours before check-in."
    )
    cancellation_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
    )
    restrict_check_in_to_booked_date = models.BooleanField(
        default=True,
        help_text=(
            "Block check-in before the booked check-in date. Late arrivals "
            "inside the booked date range are always allowed."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Hotel settings"
        verbose_name_plural = "Hotel settings"

    def __str__(self):
        return self.hotel_name

    def clean(self):
        if self.min_stay_nights < 1:
            raise ValidationError({"min_stay_nights": "Minimum stay must be at least 1 night."})
        if self.max_stay_nights < self.min_stay_nights:
            raise ValidationError({"max_stay_nights": "Maximum stay must be >= minimum stay."})
        if self.social_links and not isinstance(self.social_links, list):
            raise ValidationError({"social_links": "Must be a list of {platform, url} objects."})

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)
        cache.delete(SETTINGS_CACHE_KEY)

    @classmethod
    def get_settings(cls):
        """Cached accessor used by pricing/booking code on hot paths."""
        instance = cache.get(SETTINGS_CACHE_KEY)
        if instance is None:
            instance, _ = cls.objects.get_or_create(pk=1)
            cache.set(SETTINGS_CACHE_KEY, instance, 300)
        return instance


class HotelPolicy(TimeStampedModel):
    """Free-form hotel policy documents, publicly readable, admin editable."""

    key = models.SlugField(max_length=60, unique=True)
    title = models.CharField(max_length=150)
    content = models.TextField()
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "title"]
        verbose_name_plural = "Hotel policies"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = slugify(self.title)
        super().save(*args, **kwargs)


class Facility(TimeStampedModel):
    """Hotel-level amenities (restaurant, pool, parking...) shown on the site."""

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True, default="")
    # Stable icon identifier (e.g. "wifi"); the frontend maps it to its icon set.
    icon = models.CharField(max_length=50, blank=True, default="")
    image = models.ImageField(upload_to="facilities/%Y/%m/", null=True, blank=True,
                              validators=[validate_image_upload])
    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name_plural = "Facilities"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "facility"
            slug, i = base, 1
            while Facility.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                i += 1
                slug = f"{base}-{i}"
            self.slug = slug
        super().save(*args, **kwargs)
