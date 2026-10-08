"""Bounded staff-operations representations and command validation."""
from rest_framework import serializers

from .models import (
    AttendanceEvent, AttendanceRecord, LeaveRequest, LeaveRequestEvent,
    ShiftAssignment, ShiftAssignmentEvent, ShiftTemplate, StaffProfile,
)


class StaffProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    role = serializers.CharField(source="user.role", read_only=True)

    class Meta:
        model = StaffProfile
        fields = [
            "id", "user", "email", "full_name", "role", "employee_code", "department", "job_title",
            "employment_status", "employment_start", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "user", "email", "full_name", "role", "employee_code", "created_at", "updated_at"]


class StaffProfileCreateSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1)
    department = serializers.CharField(required=False, allow_blank=True, max_length=120)
    job_title = serializers.CharField(required=False, allow_blank=True, max_length=120)
    employment_start = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)


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
