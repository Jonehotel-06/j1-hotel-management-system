"""Operational stay and room-occupancy history.

A Booking remains the reservation/payment contract. A Stay is created only when
an arrival becomes operationally real (or when an old in-house row is lazily
bridged during checkout), so future/expired reservations do not fabricate stay
history.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.utils import hotel_today


class Stay(TimeStampedModel):
    class Status(models.TextChoices):
        EXPECTED = "EXPECTED", "Expected"
        IN_HOUSE = "IN_HOUSE", "In house"
        CHECKED_OUT = "CHECKED_OUT", "Checked out"
        NO_SHOW = "NO_SHOW", "No show"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=56, unique=True, db_index=True)
    booking = models.OneToOneField("bookings.Booking", on_delete=models.PROTECT, related_name="stay")
    guest = models.ForeignKey("bookings.Guest", on_delete=models.PROTECT, related_name="stays")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.EXPECTED, db_index=True)
    expected_arrival = models.DateField()
    expected_departure = models.DateField()
    business_date = models.DateField(default=hotel_today, db_index=True)
    actual_check_in_at = models.DateTimeField(null=True, blank=True)
    actual_check_out_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stays_checked_in",
    )
    checked_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stays_checked_out",
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-business_date", "-created_at"]
        indexes = [
            models.Index(fields=["status", "business_date"]),
            models.Index(fields=["guest", "status"]),
            models.Index(fields=["expected_departure", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(expected_departure__gt=F("expected_arrival")),
                name="stay_departure_after_arrival",
            ),
        ]

    def __str__(self):
        return f"{self.reference} · {self.get_status_display()}"


class StayRoom(TimeStampedModel):
    """Historical physical-room occupancy under a stay, including room moves."""

    stay = models.ForeignKey(Stay, on_delete=models.PROTECT, related_name="stay_rooms")
    room = models.ForeignKey("rooms.Room", on_delete=models.PROTECT, related_name="stay_occupancies")
    booking_room = models.ForeignKey(
        "bookings.BookingRoom", null=True, blank=True, on_delete=models.SET_NULL, related_name="stay_rooms"
    )
    assigned_at = models.DateTimeField(default=timezone.now, db_index=True)
    actual_check_in_at = models.DateTimeField(null=True, blank=True)
    actual_check_out_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True, db_index=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stay_rooms_assigned",
    )
    assignment_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["assigned_at", "pk"]
        indexes = [
            models.Index(fields=["stay", "released_at"]),
            models.Index(fields=["room", "released_at"]),
        ]

    @property
    def is_active(self):
        return self.released_at is None

    def __str__(self):
        return f"{self.stay.reference} → {self.room}"


class StayEvent(models.Model):
    """Append-only operational evidence for stay state and room history."""

    class Type(models.TextChoices):
        CHECKED_IN = "CHECKED_IN", "Checked in"
        CHECKED_OUT = "CHECKED_OUT", "Checked out"
        ROOM_ASSIGNED = "ROOM_ASSIGNED", "Room assigned"
        ROOM_RELEASED = "ROOM_RELEASED", "Room released"
        NO_SHOW = "NO_SHOW", "No show"
        CORRECTED = "CORRECTED", "Corrected"

    stay = models.ForeignKey(Stay, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=30, choices=Type.choices, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stay_events"
    )
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        indexes = [models.Index(fields=["stay", "occurred_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Stay events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stay events are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.stay.reference} · {self.type}"
