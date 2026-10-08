# apps/offers/models.py
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.validators import validate_image_upload
from apps.rooms.models import RoomType


class Offer(TimeStampedModel):
    class DiscountType(models.TextChoices):
        PERCENTAGE = "PERCENTAGE", "Percentage"
        FIXED_AMOUNT = "FIXED_AMOUNT", "Fixed amount"

    title = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True, blank=True)
    description = models.TextField(blank=True, default="")
    short_description = models.CharField(max_length=255, blank=True, default="")
    code = models.CharField(
        max_length=30, unique=True, null=True, blank=True,
        help_text="Optional promo code guests can enter (case-insensitive).",
    )
    discount_type = models.CharField(max_length=20, choices=DiscountType.choices,
                                     default=DiscountType.PERCENTAGE)
    discount_value = models.DecimalField(max_digits=12, decimal_places=2,
                                         validators=[MinValueValidator(Decimal("0.00"))])
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    min_nights = models.PositiveSmallIntegerField(default=1)
    max_nights = models.PositiveSmallIntegerField(null=True, blank=True)
    # Empty = applies to every room type.
    room_types = models.ManyToManyField(RoomType, blank=True, related_name="offers")
    is_active = models.BooleanField(default=True, db_index=True)
    is_featured = models.BooleanField(default=False)
    image = models.ImageField(upload_to="offers/%Y/%m/", null=True, blank=True,
                              validators=[validate_image_upload])
    terms = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-is_featured", "-start_date", "title"]
        indexes = [models.Index(fields=["is_active", "start_date", "end_date"])]

    def __str__(self):
        return self.title

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before the start date."})
        if (
            self.discount_type == self.DiscountType.PERCENTAGE
            and self.discount_value is not None
            and self.discount_value > Decimal("100")
        ):
            raise ValidationError({"discount_value": "Percentage discounts cannot exceed 100."})
        if self.max_nights is not None and self.min_nights and self.max_nights < self.min_nights:
            raise ValidationError({"max_nights": "Maximum nights must be >= minimum nights."})

    def save(self, *args, **kwargs):
        # `code` is UNIQUE and nullable. An empty string is NOT "no code" to the
        # database: a second offer saved with code="" collides with the first
        # and raises IntegrityError (a 500 the staff console shows as a failed
        # create). Normalise blank input to NULL so any number of offers may
        # exist without a promo code.
        if self.code is not None and not str(self.code).strip():
            self.code = None
        if not self.slug:
            base = slugify(self.title) or "offer"
            slug, i = base, 1
            while Offer.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                i += 1
                slug = f"{base}-{i}"
            self.slug = slug
        if self.code:
            self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class GuestDiscount(TimeStampedModel):
    """A personal, staff-granted discount for ONE guest.

    This is deliberately separate from :class:`Offer` (a public promotion):

    * an Offer is advertised, applies to anyone and is matched on room type /
      stay length;
    * a GuestDiscount is private, belongs to a single ``Guest`` row and is
      granted by a manager/administrator as a loyalty or service gesture.

    Reusability
    -----------
    The discount is GUEST-LEVEL and reusable inside its validity window. Every
    booking that consumes it stores an immutable snapshot
    (:class:`GuestDiscountApplication`) so editing or deactivating the discount
    later can never rewrite the money on an existing booking.

    The backend is the only authority: eligibility, validity and the discount
    amount are all computed server-side in :mod:`apps.offers.services`.
    """

    class DiscountType(models.TextChoices):
        PERCENTAGE = "PERCENTAGE", "Percentage"
        FIXED_AMOUNT = "FIXED_AMOUNT", "Fixed amount"

    guest = models.ForeignKey(
        "bookings.Guest", on_delete=models.CASCADE, related_name="discounts",
        help_text="The individual guest this discount belongs to.",
    )
    discount_type = models.CharField(max_length=20, choices=DiscountType.choices,
                                     default=DiscountType.PERCENTAGE)
    discount_value = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    start_date = models.DateField(
        db_index=True, help_text="First date the discount may be applied to a booking.",
    )
    # Open-ended when null: the discount stays valid until deactivated.
    end_date = models.DateField(null=True, blank=True, db_index=True,
                                help_text="Optional expiry date (inclusive).")
    is_active = models.BooleanField(default=True, db_index=True)
    reason = models.CharField(
        max_length=255,
        help_text="Why this guest was granted a discount (shown to staff only).",
    )
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="guest_discounts_created",
        help_text="Staff member who created/approved this discount.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["guest", "is_active"]),
            models.Index(fields=["is_active", "start_date", "end_date"]),
        ]

    def __str__(self):
        return f"{self.guest_id} · {self.discount_value} {self.discount_type}"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before the start date."})
        if (
            self.discount_type == self.DiscountType.PERCENTAGE
            and self.discount_value is not None
            and self.discount_value > Decimal("100")
        ):
            raise ValidationError({"discount_value": "Percentage discounts cannot exceed 100."})

    def is_valid_on(self, day) -> bool:
        """Whether this discount may be applied to a stay starting ``day``."""
        if not self.is_active:
            return False
        if self.start_date and day < self.start_date:
            return False
        if self.end_date and day > self.end_date:
            return False
        return True


class GuestDiscountApplication(TimeStampedModel):
    """Immutable record of a guest discount applied to one booking.

    Written once when the booking is created. The stored ``amount`` and the
    snapshotted type/value are what receipts, reports and staff screens read,
    so a later edit to the GuestDiscount never alters historical money.
    """

    booking = models.OneToOneField(
        "bookings.Booking", on_delete=models.CASCADE, related_name="guest_discount_application",
    )
    guest_discount = models.ForeignKey(
        GuestDiscount, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="applications",
    )
    guest = models.ForeignKey("bookings.Guest", on_delete=models.CASCADE,
                              related_name="discount_applications")
    # Snapshot of the discount AS APPLIED — never re-read from guest_discount.
    discount_type = models.CharField(max_length=20, choices=GuestDiscount.DiscountType.choices)
    discount_value = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="Actual money taken off this booking.")
    currency = models.CharField(max_length=3, default="NGN")
    reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["guest", "-created_at"])]

    def __str__(self):
        return f"{self.booking_id} · -{self.amount} {self.currency}"
