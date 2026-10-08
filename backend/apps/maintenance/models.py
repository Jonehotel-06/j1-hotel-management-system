"""Controlled maintenance work orders and explicit room-outage evidence."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class MaintenanceWorkOrder(TimeStampedModel):
    class Category(models.TextChoices):
        ELECTRICAL = "ELECTRICAL", "Electrical"
        PLUMBING = "PLUMBING", "Plumbing"
        HVAC = "HVAC", "HVAC"
        FURNITURE = "FURNITURE", "Furniture / fixtures"
        SAFETY = "SAFETY", "Safety"
        GENERAL = "GENERAL", "General maintenance"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        TRIAGED = "TRIAGED", "Triaged"
        ASSIGNED = "ASSIGNED", "Assigned"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PENDING_PARTS = "PENDING_PARTS", "Pending parts"
        READY_FOR_VERIFICATION = "READY_FOR_VERIFICATION", "Ready for verification"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    source_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    room = models.ForeignKey("rooms.Room", null=True, blank=True, on_delete=models.PROTECT, related_name="maintenance_work_orders")
    service_request = models.OneToOneField(
        "guest_services.ServiceRequest", null=True, blank=True, on_delete=models.PROTECT,
        related_name="maintenance_work_order",
    )
    category = models.CharField(max_length=20, choices=Category.choices, db_index=True)
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.OPEN, db_index=True)
    summary = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="maintenance_work_orders_reported",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="maintenance_work_orders_assigned",
    )
    triaged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="maintenance_work_orders_triaged",
    )
    triaged_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="maintenance_work_orders_completed",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["due_at", "-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "assigned_to", "due_at"]),
            models.Index(fields=["room", "status"]),
            models.Index(fields=["category", "status", "due_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.summary}"


class RoomOutage(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CLEARED = "CLEARED", "Cleared"

    room = models.ForeignKey("rooms.Room", on_delete=models.PROTECT, related_name="maintenance_outages")
    work_order = models.OneToOneField(
        MaintenanceWorkOrder, on_delete=models.PROTECT, related_name="room_outage"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    outage_status = models.CharField(max_length=20, choices=[("MAINTENANCE", "Maintenance"), ("OUT_OF_SERVICE", "Out of service")])
    reason = models.CharField(max_length=500, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    cleared_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="room_outages_created"
    )
    cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="room_outages_cleared"
    )

    class Meta:
        ordering = ["-started_at", "-pk"]
        indexes = [models.Index(fields=["room", "status"])]

    def __str__(self):
        return f"{self.room.room_number} · {self.status}"


class MaintenanceEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        TRIAGED = "TRIAGED", "Triaged"
        ASSIGNED = "ASSIGNED", "Assigned"
        CLAIMED = "CLAIMED", "Claimed"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        COMMENT = "COMMENT", "Comment"
        OUTAGE_STARTED = "OUTAGE_STARTED", "Room outage started"
        OUTAGE_CLEARED = "OUTAGE_CLEARED", "Room outage cleared"
        VERIFIED = "VERIFIED", "Verified completed"

    work_order = models.ForeignKey(MaintenanceWorkOrder, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="maintenance_events"
    )
    message = models.TextField(blank=True, default="")
    previous_status = models.CharField(max_length=30, blank=True, default="")
    new_status = models.CharField(max_length=30, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["work_order", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Maintenance events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Maintenance events are append-only and cannot be deleted.")
