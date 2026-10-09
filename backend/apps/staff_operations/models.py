"""Operational workforce records with immutable attendance/leave evidence."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.utils import hotel_today


class StaffProfile(TimeStampedModel):
    """Minimal operational profile; authentication and role remain on User."""

    class EmploymentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ON_LEAVE = "ON_LEAVE", "On leave"
        SUSPENDED = "SUSPENDED", "Suspended"
        INACTIVE = "INACTIVE", "Inactive"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_profile")
    employee_code = models.CharField(max_length=40, unique=True, db_index=True)
    department = models.CharField(max_length=120, blank=True, default="", db_index=True)
    job_title = models.CharField(max_length=120, blank=True, default="")
    employment_status = models.CharField(max_length=16, choices=EmploymentStatus.choices, default=EmploymentStatus.ACTIVE, db_index=True)
    employment_start = models.DateField(null=True, blank=True)
    emergency_contact_name = models.CharField(max_length=160, blank=True, default="")
    emergency_contact_phone = models.CharField(max_length=40, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["user__last_name", "user__first_name", "pk"]
        indexes = [models.Index(fields=["employment_status", "department"])]

    def __str__(self):
        return f"{self.employee_code} · {self.user.full_name}"


class ShiftTemplate(TimeStampedModel):
    """Reusable nominal shift definition; assignments preserve actual times."""

    code = models.CharField(max_length=40, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    department = models.CharField(max_length=120, blank=True, default="", db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    unpaid_break_minutes = models.PositiveSmallIntegerField(default=0, validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["department", "start_time", "name"]
        indexes = [models.Index(fields=["is_active", "department"])]

    def __str__(self):
        return f"{self.code} · {self.name}"


class ShiftAssignment(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        CANCELLED = "CANCELLED", "Cancelled"
        COMPLETED = "COMPLETED", "Completed"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=180, null=True, blank=True, unique=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shift_assignments")
    template = models.ForeignKey(ShiftTemplate, null=True, blank=True, on_delete=models.PROTECT, related_name="assignments")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    planned_start_at = models.DateTimeField(db_index=True)
    planned_end_at = models.DateTimeField(db_index=True)
    actual_start_at = models.DateTimeField(null=True, blank=True)
    actual_end_at = models.DateTimeField(null=True, blank=True)
    actual_worked_minutes = models.PositiveIntegerField(null=True, blank=True)
    department = models.CharField(max_length=120, blank=True, default="", db_index=True)
    location = models.CharField(max_length=120, blank=True, default="")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="shifts_assigned")
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="shifts_cancelled")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=500, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["planned_start_at", "pk"]
        indexes = [
            models.Index(fields=["staff", "status", "planned_start_at"]),
            models.Index(fields=["status", "planned_start_at"]),
            models.Index(fields=["department", "planned_start_at"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.staff.full_name}"


class ShiftAssignmentEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "CREATED", "Created"
        CANCELLED = "CANCELLED", "Cancelled"
        COMPLETED = "COMPLETED", "Completed"

    shift = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="shift_assignment_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["shift", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Shift-assignment events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Shift-assignment events are append-only and cannot be deleted.")


class AttendanceRecord(TimeStampedModel):
    """Lockable current-state projection over immutable attendance events."""

    class Status(models.TextChoices):
        ON_DUTY = "ON_DUTY", "On duty"
        ON_BREAK = "ON_BREAK", "On break"
        COMPLETED = "COMPLETED", "Completed"
        EXCEPTION = "EXCEPTION", "Exception"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attendance_records")
    shift = models.OneToOneField(ShiftAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="attendance_record")
    business_date = models.DateField(default=hotel_today, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ON_DUTY, db_index=True)
    clock_in_at = models.DateTimeField()
    clock_out_at = models.DateTimeField(null=True, blank=True)
    active_break_started_at = models.DateTimeField(null=True, blank=True)
    accumulated_break_minutes = models.PositiveIntegerField(default=0)
    worked_minutes = models.PositiveIntegerField(null=True, blank=True)
    last_event_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-clock_in_at", "-pk"]
        indexes = [
            models.Index(fields=["staff", "status", "last_event_at"]),
            models.Index(fields=["business_date", "status"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.staff.full_name} · {self.status}"


class AttendanceEvent(models.Model):
    class Type(models.TextChoices):
        CLOCK_IN = "CLOCK_IN", "Clock in"
        BREAK_STARTED = "BREAK_STARTED", "Break started"
        BREAK_ENDED = "BREAK_ENDED", "Break ended"
        CLOCK_OUT = "CLOCK_OUT", "Clock out"

    record = models.ForeignKey(AttendanceRecord, on_delete=models.PROTECT, related_name="events")
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attendance_events")
    shift = models.ForeignKey(ShiftAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="attendance_events")
    type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    source_key = models.CharField(max_length=180, unique=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="attendance_events_recorded")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at", "pk"]
        indexes = [models.Index(fields=["staff", "occurred_at"]), models.Index(fields=["shift", "occurred_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Attendance events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Attendance events are append-only and cannot be deleted.")


class LeaveRequest(TimeStampedModel):
    class Type(models.TextChoices):
        ANNUAL = "ANNUAL", "Annual"
        SICK = "SICK", "Sick"
        PERSONAL = "PERSONAL", "Personal"
        UNPAID = "UNPAID", "Unpaid"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.CharField(max_length=180, null=True, blank=True, unique=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="leave_requests")
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.ANNUAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    reason = models.TextField(blank=True, default="")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="leave_requests_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="leave_requests_cancelled")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-start_date", "-created_at", "-pk"]
        indexes = [
            models.Index(fields=["staff", "status", "start_date"]),
            models.Index(fields=["status", "start_date", "end_date"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.staff.full_name} · {self.status}"


class LeaveRequestEvent(models.Model):
    class Type(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    leave_request = models.ForeignKey(LeaveRequest, on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=16, choices=Type.choices, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="leave_request_events")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["leave_request", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Leave-request events are append-only and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Leave-request events are append-only and cannot be deleted.")


# Kept in a focused module because compensation and payroll evidence have
# different privacy and lifecycle rules from ordinary workforce records.
from .payroll_models import (  # noqa: E402,F401
    PayrollEvent, PayrollLine, PayrollPeriod, PayrollStatutoryRuleEvent,
    PayrollStatutoryRuleSet, PayrollTaxIdentity, StaffCompensation,
)
