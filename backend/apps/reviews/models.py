# apps/reviews/models.py
"""Private guest reviews of completed stays.

Design notes
------------
* A review is ALWAYS anchored to one real booking (verified completed stay);
  the OneToOne relation is the database-level duplicate-review guard.
* Reviews are NEVER exposed publicly — the only read path is the
  administrator-only management API. There is no public list endpoint.
* ``guest_name`` is a display snapshot taken at submission time so the
  review stays meaningful even if the guest record is later edited.
* No payment or identification data is ever stored here.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.bookings.models import Booking, Guest
from apps.core.models import TimeStampedModel

RATING_MIN = 1
RATING_MAX = 5
COMMENT_MIN_LENGTH = 10
COMMENT_MAX_LENGTH = 2000


class Review(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        REVIEWED = "REVIEWED", "Reviewed"

    booking = models.OneToOneField(
        Booking, on_delete=models.CASCADE, related_name="review",
        help_text="One review per booking — enforced by the database.",
    )
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name="reviews")
    # Display snapshot at submission time (guest records are staff-editable).
    guest_name = models.CharField(max_length=200)
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(RATING_MIN), MaxValueValidator(RATING_MAX)],
        db_index=True,
    )
    comment = models.TextField(max_length=COMMENT_MAX_LENGTH)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
    )
    internal_notes = models.TextField(
        blank=True, default="",
        help_text="Administrator-only notes. Never shown to the guest.",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="reviews_handled",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["status", "rating"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=RATING_MIN, rating__lte=RATING_MAX),
                name="review_rating_between_1_and_5",
            ),
        ]

    def __str__(self):
        return f"{self.rating}★ review for {self.booking.booking_reference}"
