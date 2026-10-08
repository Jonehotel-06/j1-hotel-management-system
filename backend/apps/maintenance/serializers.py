from rest_framework import serializers

from .models import MaintenanceEvent, MaintenanceWorkOrder, RoomOutage


class MaintenanceEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)
    class Meta:
        model = MaintenanceEvent
        fields = ["id", "type", "actor_email", "message", "previous_status", "new_status", "details", "created_at"]
        read_only_fields = fields


class RoomOutageSerializer(serializers.ModelSerializer):
    room_number = serializers.CharField(source="room.room_number", read_only=True)
    cleared_by_email = serializers.EmailField(source="cleared_by.email", read_only=True, allow_null=True)
    class Meta:
        model = RoomOutage
        fields = ["id", "room_number", "status", "outage_status", "reason", "started_at", "cleared_at", "cleared_by_email"]
        read_only_fields = fields


class MaintenanceWorkOrderListSerializer(serializers.ModelSerializer):
    room_number = serializers.CharField(source="room.room_number", read_only=True, allow_null=True)
    service_request_reference = serializers.CharField(source="service_request.reference", read_only=True, allow_null=True)
    assigned_to_name = serializers.CharField(source="assigned_to.full_name", read_only=True, allow_null=True)
    assigned_to_email = serializers.EmailField(source="assigned_to.email", read_only=True, allow_null=True)

    class Meta:
        model = MaintenanceWorkOrder
        fields = [
            "reference", "room_number", "service_request_reference", "category", "priority", "status", "summary",
            "due_at", "assigned_to_name", "assigned_to_email", "triaged_at", "completed_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class MaintenanceWorkOrderDetailSerializer(MaintenanceWorkOrderListSerializer):
    description = serializers.CharField(read_only=True)
    events = MaintenanceEventSerializer(many=True, read_only=True)
    room_outage = RoomOutageSerializer(read_only=True, allow_null=True)
    class Meta(MaintenanceWorkOrderListSerializer.Meta):
        fields = MaintenanceWorkOrderListSerializer.Meta.fields + ["description", "room_outage", "events"]


class MaintenanceCreateSerializer(serializers.Serializer):
    room_id = serializers.IntegerField(required=False, min_value=1)
    service_request_reference = serializers.CharField(required=False, max_length=64)
    category = serializers.ChoiceField(choices=MaintenanceWorkOrder.Category.choices)
    priority = serializers.ChoiceField(choices=MaintenanceWorkOrder.Priority.choices, default=MaintenanceWorkOrder.Priority.NORMAL)
    summary = serializers.CharField(max_length=255, trim_whitespace=True)
    description = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    due_at = serializers.DateTimeField(required=False)
    assigned_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class MaintenanceAssignSerializer(serializers.Serializer):
    assigned_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    due_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class MaintenanceStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=MaintenanceWorkOrder.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class MaintenanceCommentSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=5000, trim_whitespace=True)


class StartOutageSerializer(serializers.Serializer):
    outage_status = serializers.ChoiceField(choices=["MAINTENANCE", "OUT_OF_SERVICE"], default="MAINTENANCE")
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ClearOutageSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)
