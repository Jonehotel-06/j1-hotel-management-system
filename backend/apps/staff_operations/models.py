"""Operational workforce records with immutable attendance/leave evidence."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.utils import hotel_today


class Department(TimeStampedModel):
    """Configurable hotel department registry supporting multi-staff & subdomain routing."""

    code = models.CharField(max_length=40, unique=True, db_index=True)
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=60, unique=True, db_index=True)
    subdomains = models.JSONField(default=list, blank=True)
    dashboard_path = models.CharField(max_length=160, blank=True, default="/dashboard/index.html")
    supported_roles = models.JSONField(default=list, blank=True)
    job_positions = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default="")
    head = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="headed_departments"
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["name", "code"]

    def __str__(self):
        return f"{self.code} · {self.name}"


class StaffProfile(TimeStampedModel):
    """Comprehensive operational employee profile; authentication and role remain on User."""

    class EmploymentStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ON_LEAVE = "ON_LEAVE", "On leave"
        SUSPENDED = "SUSPENDED", "Suspended"
        INACTIVE = "INACTIVE", "Inactive"

    class EmploymentType(models.TextChoices):
        FULL_TIME = "FULL_TIME", "Full-time"
        PART_TIME = "PART_TIME", "Part-time"
        CONTRACT = "CONTRACT", "Contract"
        PROBATION = "PROBATION", "Probation"
        TEMPORARY = "TEMPORARY", "Temporary / Casual"

    class ProbationStatus(models.TextChoices):
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"
        ON_PROBATION = "ON_PROBATION", "On probation"
        COMPLETED = "COMPLETED", "Probation completed"
        EXTENDED = "EXTENDED", "Probation extended"

    class CredentialDeliveryStatus(models.TextChoices):
        NOT_SENT = "NOT_SENT", "Not sent"
        PENDING_DELIVERY = "PENDING_DELIVERY", "Pending delivery"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Delivery failed"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_profile")
    employee_code = models.CharField(max_length=40, unique=True, db_index=True)
    department = models.CharField(max_length=120, blank=True, default="", db_index=True)
    department_ref = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="staff_profiles"
    )
    job_title = models.CharField(max_length=120, blank=True, default="")
    employment_type = models.CharField(
        max_length=24, choices=EmploymentType.choices, default=EmploymentType.FULL_TIME, db_index=True
    )
    employment_status = models.CharField(
        max_length=16, choices=EmploymentStatus.choices, default=EmploymentStatus.ACTIVE, db_index=True
    )
    employment_start = models.DateField(null=True, blank=True)
    probation_status = models.CharField(
        max_length=24, choices=ProbationStatus.choices, default=ProbationStatus.NOT_APPLICABLE
    )
    probation_end_date = models.DateField(null=True, blank=True)
    reporting_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="subordinate_profiles"
    )
    work_location = models.CharField(max_length=120, blank=True, default="Main Hotel Building")
    default_shift_template = models.ForeignKey(
        "ShiftTemplate", null=True, blank=True, on_delete=models.SET_NULL, related_name="default_staff_profiles"
    )
    assigned_workstation = models.ForeignKey(
        "accounts.Workstation", null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_staff_profiles"
    )
    job_description = models.TextField(blank=True, default="")
    employment_end_date = models.DateField(null=True, blank=True)
    employment_end_reason = models.CharField(max_length=255, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=24, blank=True, default="")
    residential_address = models.TextField(blank=True, default="")
    emergency_contact_name = models.CharField(max_length=160, blank=True, default="")
    emergency_contact_relationship = models.CharField(max_length=80, blank=True, default="")
    emergency_contact_phone = models.CharField(max_length=40, blank=True, default="")
    emergency_contact_address = models.TextField(blank=True, default="")
    credential_delivery_status = models.CharField(
        max_length=24, choices=CredentialDeliveryStatus.choices, default=CredentialDeliveryStatus.NOT_SENT
    )
    credential_delivered_at = models.DateTimeField(null=True, blank=True)
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


class StaffDisciplinaryRecord(TimeStampedModel):
    """Controlled offense, investigation, disciplinary action, and deduction register.

    An open allegation or investigation never automatically reduces salary. A
    monetary deduction affects payroll only after explicit approval by an
    authorized approver following separation-of-duties rules.
    """

    class Category(models.TextChoices):
        ATTENDANCE = "ATTENDANCE", "Attendance / lateness"
        ABSENCE = "ABSENCE", "Unauthorised absence"
        MISCONDUCT = "MISCONDUCT", "Workplace misconduct"
        POLICY_VIOLATION = "POLICY_VIOLATION", "Policy violation"
        CASH_VARIANCE = "CASH_VARIANCE", "Cash / billing variance"
        STOCK_SHORTAGE = "STOCK_SHORTAGE", "Inventory / stock shortage"
        DAMAGE = "DAMAGE", "Property / asset damage"
        GUEST_COMPLAINT = "GUEST_COMPLAINT", "Verified guest complaint"
        INSUBORDINATION = "INSUBORDINATION", "Insubordination"
        SECURITY_BREACH = "SECURITY_BREACH", "Security breach"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open / reported"
        UNDER_REVIEW = "UNDER_REVIEW", "Under investigation"
        RESOLVED = "RESOLVED", "Resolved / action approved"
        DISMISSED = "DISMISSED", "Dismissed / exonerated"
        APPEALED = "APPEALED", "Appeal under review"
        CLOSED = "CLOSED", "Closed"

    class ActionType(models.TextChoices):
        NONE = "NONE", "None / pending"
        VERBAL_WARNING = "VERBAL_WARNING", "Verbal warning"
        WRITTEN_WARNING = "WRITTEN_WARNING", "Written warning"
        FINAL_WARNING = "FINAL_WARNING", "Final written warning"
        MONETARY_DEDUCTION = "MONETARY_DEDUCTION", "Approved monetary deduction"
        SUSPENSION = "SUSPENSION", "Suspension"
        TERMINATION = "TERMINATION", "Termination"
        EXONERATED = "EXONERATED", "Exonerated / no action"

    class WarningLevel(models.TextChoices):
        NONE = "NONE", "None"
        VERBAL = "VERBAL", "Verbal"
        FIRST_WRITTEN = "FIRST_WRITTEN", "First written"
        FINAL_WRITTEN = "FINAL_WRITTEN", "Final written"

    class AppealStatus(models.TextChoices):
        NONE = "NONE", "No appeal"
        SUBMITTED = "SUBMITTED", "Appeal submitted"
        UPHELD = "UPHELD", "Decision upheld"
        OVERTURNED = "OVERTURNED", "Decision overturned"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="disciplinary_records")
    incident_at = models.DateTimeField(default=timezone.now, db_index=True)
    department = models.CharField(max_length=120, blank=True, default="")
    location = models.CharField(max_length=120, blank=True, default="")
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.MISCONDUCT, db_index=True)
    allegation_summary = models.CharField(max_length=255)
    allegation_detail = models.TextField(blank=True, default="")
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="disciplinary_reports_filed"
    )
    evidence_notes = models.TextField(blank=True, default="")
    evidence_attachment = models.FileField(upload_to="disciplinary/%Y/%m/", null=True, blank=True)
    employee_response = models.TextField(blank=True, default="")
    employee_responded_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="disciplinary_records_reviewed"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    investigation_outcome = models.TextField(blank=True, default="")
    disciplinary_action = models.CharField(max_length=32, choices=ActionType.choices, default=ActionType.NONE)
    warning_level = models.CharField(max_length=24, choices=WarningLevel.choices, default=WarningLevel.NONE)
    recommended_deduction_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))]
    )
    approved_deduction_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))]
    )
    deduction_approved = models.BooleanField(default=False, db_index=True)
    deduction_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="disciplinary_deductions_approved"
    )
    deduction_approved_at = models.DateTimeField(null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True, db_index=True)
    applied_payroll_line = models.ForeignKey(
        "PayrollLine", null=True, blank=True, on_delete=models.SET_NULL, related_name="disciplinary_deductions"
    )
    appeal_status = models.CharField(max_length=24, choices=AppealStatus.choices, default=AppealStatus.NONE)
    appeal_notes = models.TextField(blank=True, default="")
    appealed_at = models.DateTimeField(null=True, blank=True)
    appeal_decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="disciplinary_appeals_decided"
    )
    appeal_decided_at = models.DateTimeField(null=True, blank=True)
    audit_trail = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-incident_at", "-created_at", "-pk"]
        indexes = [
            models.Index(fields=["staff", "status", "incident_at"]),
            models.Index(fields=["deduction_approved", "effective_date"]),
        ]

    def __str__(self):
        return f"{self.reference} · {self.staff.full_name} · {self.status}"


class StaffDocument(TimeStampedModel):
    """Employee HR, contract, certification, asset, and exit document records."""

    class Category(models.TextChoices):
        EMPLOYMENT_LETTER = "EMPLOYMENT_LETTER", "Employment letter"
        CONTRACT = "CONTRACT", "Employment contract"
        IDENTIFICATION = "IDENTIFICATION", "Identification / KYC"
        CERTIFICATION = "CERTIFICATION", "Certification / license"
        TRAINING = "TRAINING", "Training record"
        PERFORMANCE_REVIEW = "PERFORMANCE_REVIEW", "Performance review"
        EQUIPMENT_ASSIGNMENT = "EQUIPMENT_ASSIGNMENT", "Assigned equipment / asset"
        POLICY_ACKNOWLEDGEMENT = "POLICY_ACKNOWLEDGEMENT", "Policy acknowledgement"
        EXIT_DOCUMENT = "EXIT_DOCUMENT", "Exit / handover document"
        OTHER = "OTHER", "Other document"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_documents")
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.CONTRACT, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    file = models.FileField(upload_to="staff_documents/%Y/%m/", null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True, default="")
    file_size = models.PositiveIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True, default="")
    issued_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_confidential = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="staff_documents_uploaded"
    )
    replaced_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces_documents"
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["staff", "category", "is_active"])]

    def __str__(self):
        return f"{self.reference} · {self.staff.full_name} · {self.title}"


class StaffPerformanceReview(TimeStampedModel):
    """Manager performance review; only FINALIZED reviews reach the employee.

    This is the smallest maintainable review record: the system previously
    had no performance-review model at all (only a PERFORMANCE_REVIEW
    document category). Drafts stay manager-side, employees can never edit
    a review through the self-service profile, and there is deliberately no
    free-text confidential-notes field on the employee-facing serializer.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        FINALIZED = "FINALIZED", "Finalized"

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="performance_reviews")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="performance_reviews_given"
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    review_date = models.DateField(default=hotel_today, db_index=True)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    strengths = models.TextField(blank=True, default="")
    areas_for_improvement = models.TextField(blank=True, default="")
    objectives = models.TextField(blank=True, default="")
    improvement_actions = models.TextField(blank=True, default="")
    follow_up_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)

    class Meta:
        ordering = ["-review_date", "-pk"]
        indexes = [models.Index(fields=["staff", "status", "review_date"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(rating__isnull=True) | (Q(rating__gte=1) & Q(rating__lte=5)),
                name="performance_review_rating_1_to_5",
            ),
            models.CheckConstraint(
                condition=Q(period_start__isnull=True) | Q(period_end__isnull=True) | Q(period_end__gte=models.F("period_start")),
                name="performance_review_period_order",
            ),
        ]

    def __str__(self):
        return f"{self.reference} · {self.staff.full_name} · {self.status}"


# Kept in a focused module because compensation and payroll evidence have
# different privacy and lifecycle rules from ordinary workforce records.
from .payroll_models import (  # noqa: E402,F401
    LatenessDeductionPolicy, PayrollEvent, PayrollLine, PayrollPeriod, PayrollSalaryPayment,
    PayrollStatutoryRuleEvent, PayrollStockShortageAllocation, PayrollStockShortageCase,
    PayrollStatutoryRuleSet, StaffCompensation, StaffLatenessDeduction, PayrollTaxIdentity,
)

# Facial-verification records live in their own module (descriptor-only, retention-bound).
from .face_models import FaceTemplate, FaceVerificationAttempt  # noqa: E402,F401

# Fingerprint biometrics and temporary credentials.
from .biometric_models import (  # noqa: E402,F401
    FingerprintTemplate, FingerprintVerificationAttempt, TemporaryWorkstationCredential,
    FingerprintDevice, FingerprintDeviceEvent,
)
