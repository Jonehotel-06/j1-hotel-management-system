# apps/rooms/models.py
"""Room types (bookable products) and physical rooms (inventory).

IMPORTANT DISTINCTION (see spec §12): a Room's operational `status`
(available / occupied / maintenance...) is NOT booking availability. Date-range
availability for a room type is derived by the availability engine from
physical inventory + overlapping reservations.
"""
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.validators import validate_image_upload


class Amenity(TimeStampedModel):
    """Database-driven room amenities (Wi-Fi, AC, TV...)."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    icon = models.CharField(max_length=50, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Amenities"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class RoomType(TimeStampedModel):
    class SmokingPolicy(models.TextChoices):
        NON_SMOKING = "NON_SMOKING", "Non-smoking"
        SMOKING_ALLOWED = "SMOKING_ALLOWED", "Smoking allowed"

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True, default="")
    short_description = models.CharField(max_length=255, blank=True, default="")
    base_price = models.DecimalField(max_digits=12, decimal_places=2,
                                     validators=[MinValueValidator(Decimal("0.00"))],
                                     help_text="Price per night in hotel currency (NGN).")
    max_guests = models.PositiveSmallIntegerField(default=2)
    bed_type = models.CharField(max_length=80, blank=True, default="")
    bed_count = models.PositiveSmallIntegerField(default=1)
    room_size = models.CharField(max_length=60, blank=True, default="", help_text='e.g. "25 m²"')
    view = models.CharField(max_length=80, blank=True, default="")
    smoking_policy = models.CharField(max_length=20, choices=SmokingPolicy.choices,
                                      default=SmokingPolicy.NON_SMOKING)
    children_allowed = models.BooleanField(default=True)
    extra_guest_allowed = models.BooleanField(default=False)
    extra_guest_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                          validators=[MinValueValidator(Decimal("0.00"))],
                                          help_text="Per extra guest, per night.")
    amenities = models.ManyToManyField(Amenity, blank=True, related_name="room_types")
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "room"
            slug, i = base, 1
            while RoomType.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                i += 1
                slug = f"{base}-{i}"
            self.slug = slug
        super().save(*args, **kwargs)


class RoomTypeImage(TimeStampedModel):
    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="room-types/%Y/%m/", validators=[validate_image_upload])
    alt_text = models.CharField(max_length=200, blank=True, default="")
    caption = models.CharField(max_length=200, blank=True, default="")
    display_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self):
        return f"Image for {self.room_type_id} (#{self.pk})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_primary:
            # Only one primary image per room type.
            RoomTypeImage.objects.filter(room_type=self.room_type, is_primary=True).exclude(
                pk=self.pk
            ).update(is_primary=False)


class RoomImage(TimeStampedModel):
    """Images of a specific physical room, distinct from room-type marketing images."""
    room = models.ForeignKey("Room", on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="rooms/%Y/%m/", validators=[validate_image_upload])
    alt_text = models.CharField(max_length=200, blank=True, default="")
    caption = models.CharField(max_length=200, blank=True, default="")
    display_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_order", "id"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_primary:
            RoomImage.objects.filter(room=self.room, is_primary=True).exclude(pk=self.pk).update(is_primary=False)


class Room(TimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        OCCUPIED = "OCCUPIED", "Occupied"
        RESERVED = "RESERVED", "Reserved"
        MAINTENANCE = "MAINTENANCE", "Under maintenance"
        OUT_OF_SERVICE = "OUT_OF_SERVICE", "Out of service"

    class HousekeepingStatus(models.TextChoices):
        CLEAN = "CLEAN", "Clean"
        DIRTY = "DIRTY", "Dirty"
        CLEANING = "CLEANING", "Being cleaned"

    room_number = models.CharField(max_length=10, unique=True)
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms")
    floor = models.SmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.AVAILABLE, db_index=True)
    housekeeping_status = models.CharField(max_length=20, choices=HousekeepingStatus.choices,
                                           default=HousekeepingStatus.CLEAN)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["room_number"]
        indexes = [models.Index(fields=["room_type", "status"])]

    def __str__(self):
        return f"Room {self.room_number} ({self.room_type.name})"
