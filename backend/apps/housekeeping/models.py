"""Room-readiness tasks with append-only workflow evidence."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class HousekeepingTask(TimeStampedModel):
    class Type(models.TextChoices):
        CHECKOUT_CLEAN = "CHECKOUT_CLEAN", "Checkout clean"
        STAYOVER_SERVICE = "STAYOVER_SERVICE", "Stayover service"
        TURNDOWN = "TURNDOWN", "Turndown"
        INSPECTION = "INSPECTION", "Inspection"
        GUEST_REQUEST = "GUEST_REQUEST", "Guest request"
        DEEP_CLEAN = "DEEP_CLEAN", "Deep clean"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ASSIGNED = "ASSIGNED", "Assigned"
        ACCEPTED = "ACCEPTED", "Accepted"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        READY_FOR_INSPECTION = "READY_FOR_INSPECTION", "Ready for inspection"
        COMPLETED = "COMPLETED", "Completed"
        BLOCKED = "BLOCKED", "Blocked"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    source_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    room = models.ForeignKey("rooms.Room", on_delete=models.PROTECT, related_name="housekeeping_tasks")
    stay = models.ForeignKey(
        "stays.Stay", null=True, blank=True, on_delete=models.PROTECT, related_name="housekeeping_tasks"
    )
    service_request = models.OneToOneField(
        "guest_services.ServiceRequest", null=True, blank=True, on_delete=models.PROTECT,
        related_name="housekeeping_task",
    )
    type = models.CharField(max_length=30, choices=Type.choices, db_index=True)
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.OPEN, db_index=True)
    summary = models.CharField(max_length=255)
    detail = models.TextField(blank=True, default="")
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="housekeeping_tasks_assigned",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="housekeeping_tasks_created",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ready_for_inspection_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="housekeeping_tasks_inspected",
    )
    inspected_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["due_at", "-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "assigned_to", "due_at"]),
            models.Index(fields=["room", "status"]),
            models.Index(fields=["stay", "created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.room.room_number} · {self.status}"


class HousekeepingTaskEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        ASSIGNED = "ASSIGNED", "Assigned"
        CLAIMED = "CLAIMED", "Claimed"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        COMMENT = "COMMENT", "Comment"
        INSPECTED = "INSPECTED", "Inspected"

    task = models.ForeignKey(HousekeepingTask, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="housekeeping_task_events",
    )
    message = models.TextField(blank=True, default="")
    previous_status = models.CharField(max_length=30, blank=True, default="")
    new_status = models.CharField(max_length=30, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["task", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Housekeeping-task events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Housekeeping-task events are append-only and cannot be deleted.")


class ReusableItem(models.Model):
    """Explicit classification keeps consumables out of asset checklists."""
    name = models.CharField(max_length=120)
    normalized_name = models.CharField(max_length=120, unique=True, editable=False)
    category = models.CharField(max_length=12, choices=[('REUSABLE', 'Reusable asset'), ('CONSUMABLE', 'Consumable')], default='REUSABLE')

    def save(self, *args, **kwargs):
        self.name = ' '.join(self.name.split())
        if not self.name:
            raise ValidationError('Item name is required.')
        self.normalized_name = self.name.casefold()
        super().save(*args, **kwargs)


class TypeAsset(models.Model):
    room_type = models.ForeignKey('rooms.RoomType', on_delete=models.PROTECT)
    item = models.ForeignKey(ReusableItem, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['room_type', 'item'], name='hk_type_asset_unique')]


class RoomAsset(models.Model):
    # A zero quantity explicitly removes a type-level item from this room.
    room = models.ForeignKey('rooms.Room', on_delete=models.PROTECT)
    item = models.ForeignKey(ReusableItem, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['room', 'item'], name='hk_room_asset_unique')]


class AssetInspection(models.Model):
    assignment = models.ForeignKey('bookings.BookingRoom', on_delete=models.PROTECT, related_name='asset_inspections')
    room = models.ForeignKey('rooms.Room', on_delete=models.PROTECT)
    booking = models.ForeignKey('bookings.Booking', on_delete=models.PROTECT)
    stay_room = models.ForeignKey('stays.StayRoom', null=True, blank=True, on_delete=models.PROTECT)
    room_number_snapshot = models.CharField(max_length=100)
    room_type_snapshot = models.CharField(max_length=120)
    booking_reference_snapshot = models.CharField(max_length=40)
    phase = models.CharField(max_length=4, choices=[('PRE', 'Pre check-in'), ('POST', 'Post checkout')])
    # Snapshots include names and quantities; subsequent catalog edits never alter evidence.
    checklist = models.JSONField(default=list)
    draft = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='asset_inspections_created')
    created_at = models.DateTimeField(default=timezone.now)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='asset_inspections_completed')
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['assignment', 'phase'], name='hk_asset_inspection_once')]
        indexes = [models.Index(fields=['phase', 'completed_at'])]

    def save(self, *args, **kwargs):
        if self.pk and AssetInspection.objects.filter(pk=self.pk, completed_at__isnull=False).exists():
            raise ValidationError('Completed inspections are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Inspections cannot be deleted.')


class AssetInspectionLine(models.Model):
    inspection = models.ForeignKey(AssetInspection, on_delete=models.PROTECT, related_name='lines')
    item_id_snapshot = models.PositiveIntegerField()
    item_name = models.CharField(max_length=120)
    expected = models.PositiveIntegerField()
    before = models.PositiveIntegerField(null=True)
    actual = models.PositiveIntegerField()
    condition = models.CharField(max_length=12, choices=[('GOOD', 'Good'), ('DAMAGED', 'Damaged')])
    replaced = models.BooleanField(default=False)
    remarks = models.TextField(blank=True, default='')
    evidence = models.TextField(blank=True, default='', help_text='Evidence reference, not an uploaded file.')
    missing = models.PositiveIntegerField(default=0)
    discrepancy = models.BooleanField(default=False, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['inspection', 'item_id_snapshot'], name='hk_asset_line_unique')]

    def save(self, *args, **kwargs):
        if self.pk or (self.inspection_id and AssetInspection.objects.filter(pk=self.inspection_id, completed_at__isnull=False).exists()):
            raise ValidationError('Inspection evidence is immutable.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Inspection evidence cannot be deleted.')


class AssetReviewEvent(models.Model):
    line = models.ForeignKey(AssetInspectionLine, on_delete=models.PROTECT, related_name='reviews')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=[('INVESTIGATING', 'Investigating'), ('RESOLVED', 'Resolved')])
    explanation = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at', 'pk']
        indexes = [models.Index(fields=['line', 'created_at'])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Review events are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Review events cannot be deleted.')
