"""Guest-service requests and immutable operational activity evidence."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class ServiceRequest(TimeStampedModel):
    """A routed guest need; mutable projection with append-only event history."""

    class Category(models.TextChoices):
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeeping"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        ROOM_SERVICE = "ROOM_SERVICE", "Room service"
        AMENITY = "AMENITY", "Amenity / supplies"
        TRANSPORT = "TRANSPORT", "Transport"
        BILLING = "BILLING", "Billing / folio"
        GENERAL = "GENERAL", "General assistance"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class Channel(models.TextChoices):
        PORTAL = "PORTAL", "Guest portal"
        FRONT_DESK = "FRONT_DESK", "Front desk"
        PHONE = "PHONE", "Phone"
        IN_PERSON = "IN_PERSON", "In person"
        STAFF = "STAFF", "Staff created"

    class OwnerTeam(models.TextChoices):
        FRONT_DESK = "FRONT_DESK", "Front desk"
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeeping"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        FOOD_BEVERAGE = "FOOD_BEVERAGE", "Food & beverage"
        MANAGEMENT = "MANAGEMENT", "Management"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"
        ESCALATED = "ESCALATED", "Escalated"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    # A server-scoped digest, not an untrusted raw client key. It is global so
    # retries converge even under concurrent requests without cross-guest reuse.
    idempotency_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    guest = models.ForeignKey("bookings.Guest", on_delete=models.PROTECT, related_name="service_requests")
    stay = models.ForeignKey(
        "stays.Stay", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests"
    )
    room = models.ForeignKey(
        "rooms.Room", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests"
    )
    category = models.CharField(max_length=30, choices=Category.choices, db_index=True)
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    owner_team = models.CharField(max_length=20, choices=OwnerTeam.choices, default=OwnerTeam.FRONT_DESK, db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_service_requests",
    )
    summary = models.CharField(max_length=255)
    detail = models.TextField(blank=True, default="")
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_requests_created",
    )
    # Retain the verified portal identity as a snapshot even if a Guest email
    # is later corrected; never expose it in general staff list output by default.
    portal_email = models.EmailField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "assigned_to", "due_at"]),
            models.Index(fields=["owner_team", "status", "due_at"]),
            models.Index(fields=["stay", "created_at"]),
            models.Index(fields=["room", "status"]),
            models.Index(fields=["guest", "created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.summary}"


class ServiceRequestEvent(models.Model):
    """Append-only status, assignment, escalation, and conversation evidence."""

    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        ASSIGNED = "ASSIGNED", "Assigned"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        COMMENT = "COMMENT", "Comment"
        ESCALATED = "ESCALATED", "Escalated"
        CANCELLED = "CANCELLED", "Cancelled"

    request = models.ForeignKey(ServiceRequest, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="service_request_events",
    )
    actor_label = models.CharField(max_length=160, blank=True, default="")
    message = models.TextField(blank=True, default="")
    guest_visible = models.BooleanField(default=False, db_index=True)
    previous_status = models.CharField(max_length=20, blank=True, default="")
    new_status = models.CharField(max_length=20, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["request", "created_at"]),
            models.Index(fields=["request", "guest_visible", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Service-request events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Service-request events are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.request.reference} · {self.type}"
