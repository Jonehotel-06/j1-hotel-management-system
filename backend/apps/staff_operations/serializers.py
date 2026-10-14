"""Bounded staff-operations representations and command validation."""
from rest_framework import serializers

from apps.core.storage import absolute_media_url

from .models import (
    AttendanceEvent, AttendanceRecord, Department, LeaveRequest, LeaveRequestEvent, PayrollEvent, PayrollLine,
    PayrollPeriod, ShiftAssignment, ShiftAssignmentEvent, ShiftTemplate, StaffCompensation,
    StaffDisciplinaryRecord, StaffDocument, StaffPerformanceReview, StaffProfile,
)


class DepartmentSerializer(serializers.ModelSerializer):
    head_name = serializers.CharField(source="head.full_name", read_only=True, allow_null=True)
    head_email = serializers.EmailField(source="head.email", read_only=True, allow_null=True)
    staff_count = serializers.SerializerMethodField()
    active_staff_count = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "code", "name", "slug", "subdomains", "dashboard_path",
            "supported_roles", "job_positions", "description",
            "head", "head_name", "head_email", "is_active",
            "staff_count", "active_staff_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "head_name", "head_email", "staff_count", "active_staff_count", "created_at", "updated_at"]

    def get_staff_count(self, obj):
        from django.db.models import Q
        return StaffProfile.objects.filter(
            Q(department_ref=obj) | Q(department__iexact=obj.name) | Q(department__iexact=obj.code)
        ).count()

    def get_active_staff_count(self, obj):
        from django.db.models import Q
        return StaffProfile.objects.filter(
            Q(department_ref=obj) | Q(department__iexact=obj.name) | Q(department__iexact=obj.code),
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
            user__is_active=True,
        ).count()


class StaffProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    middle_name = serializers.CharField(source="user.middle_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    role = serializers.CharField(source="user.role", read_only=True)
    role_label = serializers.CharField(source="user.get_role_display", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    must_change_password = serializers.BooleanField(source="user.must_change_password", read_only=True)
    last_login = serializers.DateTimeField(source="user.last_login", read_only=True)
    date_joined = serializers.DateTimeField(source="user.date_joined", read_only=True)
    profile_image_url = serializers.SerializerMethodField()
    department_name = serializers.SerializerMethodField()
    reporting_manager_name = serializers.CharField(source="reporting_manager.full_name", read_only=True, allow_null=True)
    default_shift_template_name = serializers.CharField(source="default_shift_template.name", read_only=True, allow_null=True)
    assigned_workstation_name = serializers.CharField(source="assigned_workstation.name", read_only=True, allow_null=True)
    fingerprint_status = serializers.SerializerMethodField()
    attendance_status = serializers.SerializerMethodField()

    class Meta:
        model = StaffProfile
        fields = [
            "id", "user", "email", "first_name", "middle_name", "last_name", "full_name",
            "phone", "role", "role_label", "is_active", "must_change_password",
            "last_login", "date_joined", "profile_image_url",
            "employee_code", "department", "department_ref", "department_name",
            "job_title", "employment_type", "employment_status", "employment_start",
            "probation_status", "probation_end_date",
            "reporting_manager", "reporting_manager_name",
            "work_location", "default_shift_template", "default_shift_template_name",
            "assigned_workstation", "assigned_workstation_name",
            "job_description", "employment_end_date", "employment_end_reason",
            "date_of_birth", "gender", "residential_address",
            "emergency_contact_name", "emergency_contact_relationship",
            "emergency_contact_phone", "emergency_contact_address",
            "credential_delivery_status", "credential_delivered_at",
            "fingerprint_status", "attendance_status",
            "notes", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "user", "email", "first_name", "middle_name", "last_name", "full_name",
            "phone", "role", "role_label", "is_active", "must_change_password",
            "last_login", "date_joined", "profile_image_url",
            "employee_code", "department_name", "reporting_manager_name",
            "default_shift_template_name", "assigned_workstation_name",
            "fingerprint_status", "attendance_status", "created_at", "updated_at",
        ]

    def get_profile_image_url(self, obj):
        return absolute_media_url(obj.user.profile_image, self.context.get("request"))

    def get_department_name(self, obj):
        if obj.department_ref_id and obj.department_ref:
            return obj.department_ref.name
        return obj.department or ""

    def get_fingerprint_status(self, obj):
        from .biometric_models import FingerprintTemplate
        active = False
        pending = False
        revoked = False
        for t in FingerprintTemplate.objects.filter(staff_id=obj.user_id).values_list("status", flat=True):
            if t == FingerprintTemplate.Status.ACTIVE:
                active = True
            elif t == FingerprintTemplate.Status.PENDING:
                pending = True
            elif t == FingerprintTemplate.Status.REVOKED:
                revoked = True
        if active:
            return "ENROLLED"
        if pending:
            return "PENDING_APPROVAL"
        if revoked:
            return "REVOKED"
        return "NOT_ENROLLED"

    def get_attendance_status(self, obj):
        open_att = (
            AttendanceRecord.objects.filter(staff_id=obj.user_id, clock_out_at__isnull=True)
            .order_by("-clock_in_at")
            .values_list("status", flat=True)
            .first()
        )
        return open_att or "NOT_CLOCKED_IN"


class StaffProfileCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    email = serializers.EmailField(required=False)
    first_name = serializers.CharField(required=False, max_length=100)
    middle_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    last_name = serializers.CharField(required=False, max_length=100)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    role = serializers.CharField(required=False, max_length=32)
    password = serializers.CharField(required=False, allow_blank=True, min_length=8, trim_whitespace=False)
    must_change_password = serializers.BooleanField(required=False, default=True)
    send_credentials_email = serializers.BooleanField(required=False, default=False)
    department = serializers.CharField(required=False, allow_blank=True, max_length=120)
    department_ref = serializers.IntegerField(required=False, allow_null=True)
    job_title = serializers.CharField(required=False, allow_blank=True, max_length=120)
    employment_type = serializers.ChoiceField(choices=StaffProfile.EmploymentType.choices, required=False)
    employment_status = serializers.ChoiceField(choices=StaffProfile.EmploymentStatus.choices, required=False)
    employment_start = serializers.DateField(required=False, allow_null=True)
    probation_status = serializers.ChoiceField(choices=StaffProfile.ProbationStatus.choices, required=False)
    probation_end_date = serializers.DateField(required=False, allow_null=True)
    reporting_manager = serializers.IntegerField(required=False, allow_null=True)
    work_location = serializers.CharField(required=False, allow_blank=True, max_length=120)
    default_shift_template = serializers.IntegerField(required=False, allow_null=True)
    assigned_workstation = serializers.IntegerField(required=False, allow_null=True)
    job_description = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    gender = serializers.CharField(required=False, allow_blank=True, max_length=24)
    residential_address = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    emergency_contact_name = serializers.CharField(required=False, allow_blank=True, max_length=160)
    emergency_contact_relationship = serializers.CharField(required=False, allow_blank=True, max_length=80)
    emergency_contact_phone = serializers.CharField(required=False, allow_blank=True, max_length=40)
    emergency_contact_address = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    basic_salary = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    housing_allowance = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    transport_allowance = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    overtime_rate = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get("user_id") and not attrs.get("email"):
            raise serializers.ValidationError({"user_id": "Supply either an existing user_id or email + first_name + last_name to register a new staff member."})
        if not attrs.get("user_id"):
            if not attrs.get("first_name") or not attrs.get("last_name"):
                raise serializers.ValidationError({"first_name": "First name and last name are required when registering a new staff member."})
        return attrs


class StaffDisciplinaryRecordSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    employee_code = serializers.SerializerMethodField()
    reported_by_name = serializers.CharField(source="reported_by.full_name", read_only=True, allow_null=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.full_name", read_only=True, allow_null=True)
    deduction_approved_by_name = serializers.CharField(source="deduction_approved_by.full_name", read_only=True, allow_null=True)
    appeal_decided_by_name = serializers.CharField(source="appeal_decided_by.full_name", read_only=True, allow_null=True)
    evidence_attachment_url = serializers.SerializerMethodField()

    class Meta:
        model = StaffDisciplinaryRecord
        fields = [
            "id", "reference", "staff", "staff_name", "staff_email", "employee_code",
            "incident_at", "department", "location", "category",
            "allegation_summary", "allegation_detail",
            "reported_by", "reported_by_name",
            "evidence_notes", "evidence_attachment", "evidence_attachment_url",
            "employee_response", "employee_responded_at",
            "status", "reviewed_by", "reviewed_by_name", "reviewed_at",
            "investigation_outcome", "disciplinary_action", "warning_level",
            "recommended_deduction_amount", "approved_deduction_amount",
            "deduction_approved", "deduction_approved_by", "deduction_approved_by_name", "deduction_approved_at",
            "effective_date", "applied_payroll_line",
            "appeal_status", "appeal_notes", "appealed_at",
            "appeal_decided_by", "appeal_decided_by_name", "appeal_decided_at",
            "audit_trail", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "staff_name", "staff_email", "employee_code",
            "reported_by", "reported_by_name", "evidence_attachment_url",
            "reviewed_by", "reviewed_by_name", "reviewed_at",
            "deduction_approved", "deduction_approved_by", "deduction_approved_by_name", "deduction_approved_at",
            "applied_payroll_line", "appealed_at", "appeal_decided_by", "appeal_decided_by_name", "appeal_decided_at",
            "audit_trail", "created_at", "updated_at",
        ]

    def get_employee_code(self, obj):
        p = getattr(obj.staff, "staff_profile", None)
        return p.employee_code if p else ""

    def get_evidence_attachment_url(self, obj):
        return absolute_media_url(obj.evidence_attachment, self.context.get("request"))


class StaffDocumentSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    uploaded_by_name = serializers.CharField(source="uploaded_by.full_name", read_only=True, allow_null=True)
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = StaffDocument
        fields = [
            "id", "reference", "staff", "staff_name", "staff_email",
            "category", "title", "description",
            "file", "file_url", "file_name", "file_size", "mime_type",
            "issued_date", "expiry_date", "is_confidential", "is_active",
            "uploaded_by", "uploaded_by_name", "replaced_by",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "staff_name", "staff_email",
            "file_url", "uploaded_by", "uploaded_by_name",
            "created_at", "updated_at",
        ]

    def get_file_url(self, obj):
        return absolute_media_url(obj.file, self.context.get("request"))


class ShiftTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftTemplate
        fields = [
            "id", "code", "name", "department", "start_time", "end_time", "unpaid_break_minutes",
            "is_active", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        start = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if start is not None and start == end:
            raise serializers.ValidationError({"end_time": "A shift template cannot have identical start and end times."})
        return attrs


class ShiftAssignmentEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = ShiftAssignmentEvent
        fields = ["type", "actor_email", "details", "created_at"]
        read_only_fields = fields


class ShiftAssignmentListSerializer(serializers.ModelSerializer):
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    template_code = serializers.CharField(source="template.code", read_only=True, allow_null=True)
    assigned_by_email = serializers.EmailField(source="assigned_by.email", read_only=True, allow_null=True)

    class Meta:
        model = ShiftAssignment
        fields = [
            "reference", "staff", "staff_email", "staff_name", "template", "template_code", "status",
            "planned_start_at", "planned_end_at", "actual_start_at", "actual_end_at", "actual_worked_minutes",
            "department", "location", "assigned_by_email", "cancelled_at", "cancellation_reason", "created_at",
        ]
        read_only_fields = fields


class ShiftAssignmentDetailSerializer(ShiftAssignmentListSerializer):
    events = ShiftAssignmentEventSerializer(many=True, read_only=True)
    notes = serializers.CharField(read_only=True)

    class Meta(ShiftAssignmentListSerializer.Meta):
        fields = ShiftAssignmentListSerializer.Meta.fields + ["notes", "events"]


class ShiftAssignmentCreateSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField(min_value=1)
    template_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    planned_start_at = serializers.DateTimeField()
    planned_end_at = serializers.DateTimeField()
    department = serializers.CharField(required=False, allow_blank=True, max_length=120)
    location = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    idempotency_key = serializers.CharField(max_length=128)


class ShiftCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class AttendanceEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = AttendanceEvent
        fields = ["type", "occurred_at", "actor_email", "metadata"]
        read_only_fields = fields


class AttendanceRecordListSerializer(serializers.ModelSerializer):
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    shift_reference = serializers.CharField(source="shift.reference", read_only=True, allow_null=True)

    class Meta:
        model = AttendanceRecord
        fields = [
            "reference", "staff", "staff_email", "staff_name", "shift_reference", "business_date", "status",
            "clock_in_at", "clock_out_at", "active_break_started_at", "accumulated_break_minutes", "worked_minutes", "last_event_at",
        ]
        read_only_fields = fields


class AttendanceRecordDetailSerializer(AttendanceRecordListSerializer):
    events = AttendanceEventSerializer(many=True, read_only=True)

    class Meta(AttendanceRecordListSerializer.Meta):
        fields = AttendanceRecordListSerializer.Meta.fields + ["events"]


class AttendanceClockSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=AttendanceEvent.Type.choices)
    shift_reference = serializers.CharField(required=False, allow_blank=True, max_length=64)
    idempotency_key = serializers.CharField(max_length=128)


class LeaveRequestEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = LeaveRequestEvent
        fields = ["type", "actor_email", "details", "created_at"]
        read_only_fields = fields


class LeaveRequestListSerializer(serializers.ModelSerializer):
    staff_email = serializers.EmailField(source="staff.email", read_only=True)
    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    reviewer_email = serializers.EmailField(source="reviewer.email", read_only=True, allow_null=True)

    class Meta:
        model = LeaveRequest
        fields = [
            "reference", "staff", "staff_email", "staff_name", "type", "status", "start_date", "end_date", "reason",
            "reviewer_email", "reviewed_at", "review_note", "cancelled_at", "cancellation_note", "created_at",
        ]
        read_only_fields = fields


class LeaveRequestDetailSerializer(LeaveRequestListSerializer):
    events = LeaveRequestEventSerializer(many=True, read_only=True)

    class Meta(LeaveRequestListSerializer.Meta):
        fields = LeaveRequestListSerializer.Meta.fields + ["events"]


class LeaveRequestCreateSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=LeaveRequest.Type.choices, default=LeaveRequest.Type.ANNUAL)
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    idempotency_key = serializers.CharField(max_length=128)


class LeaveReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class LeaveCancelSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class StaffPerformanceReviewSerializer(serializers.ModelSerializer):
    """Manager-authored review. No confidential/internal-notes field exists
    on this representation, so it is safe to reuse for the employee's own
    finalized reviews."""

    staff_name = serializers.CharField(source="staff.full_name", read_only=True)
    reviewer_name = serializers.CharField(source="reviewer.full_name", read_only=True, allow_null=True)

    class Meta:
        model = StaffPerformanceReview
        fields = [
            "id", "reference", "staff", "staff_name", "reviewer", "reviewer_name",
            "period_start", "period_end", "review_date", "rating",
            "strengths", "areas_for_improvement", "objectives", "improvement_actions",
            "follow_up_date", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "reference", "staff_name", "reviewer", "reviewer_name", "created_at", "updated_at"]

    def validate(self, attrs):
        start = attrs.get("period_start", getattr(self.instance, "period_start", None))
        end = attrs.get("period_end", getattr(self.instance, "period_end", None))
        if start and end and end < start:
            raise serializers.ValidationError({"period_end": "Review period must end on or after it starts."})
        return attrs
