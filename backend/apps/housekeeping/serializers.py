from rest_framework import serializers

from .models import HousekeepingTask, HousekeepingTaskEvent


class HousekeepingTaskEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = HousekeepingTaskEvent
        fields = ["id", "type", "actor_email", "message", "previous_status", "new_status", "details", "created_at"]
        read_only_fields = fields


class HousekeepingTaskListSerializer(serializers.ModelSerializer):
    room_number = serializers.CharField(source="room.room_number", read_only=True)
    stay_reference = serializers.CharField(source="stay.reference", read_only=True, allow_null=True)
    service_request_reference = serializers.CharField(source="service_request.reference", read_only=True, allow_null=True)
    assigned_to_name = serializers.CharField(source="assigned_to.full_name", read_only=True, allow_null=True)
    assigned_to_email = serializers.EmailField(source="assigned_to.email", read_only=True, allow_null=True)

    class Meta:
        model = HousekeepingTask
        fields = [
            "reference", "room_number", "stay_reference", "service_request_reference", "type", "priority", "status",
            "summary", "due_at", "assigned_to_name", "assigned_to_email", "accepted_at", "started_at",
            "ready_for_inspection_at", "completed_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class HousekeepingTaskDetailSerializer(HousekeepingTaskListSerializer):
    detail = serializers.CharField(read_only=True)
    events = HousekeepingTaskEventSerializer(many=True, read_only=True)

    class Meta(HousekeepingTaskListSerializer.Meta):
        fields = HousekeepingTaskListSerializer.Meta.fields + ["detail", "events"]


class HousekeepingTaskCreateSerializer(serializers.Serializer):
    room_id = serializers.IntegerField(min_value=1)
    stay_reference = serializers.CharField(required=False, max_length=56)
    service_request_reference = serializers.CharField(required=False, max_length=64)
    type = serializers.ChoiceField(choices=HousekeepingTask.Type.choices)
    priority = serializers.ChoiceField(choices=HousekeepingTask.Priority.choices, default=HousekeepingTask.Priority.NORMAL)
    summary = serializers.CharField(max_length=255, trim_whitespace=True)
    detail = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    due_at = serializers.DateTimeField(required=False)
    assigned_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class HousekeepingAssignSerializer(serializers.Serializer):
    assigned_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    due_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class HousekeepingStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=HousekeepingTask.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class HousekeepingCommentSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=5000, trim_whitespace=True)
