"""Explicit staff and portal contracts for guest-service operations."""
from rest_framework import serializers

from .models import ServiceRequest, ServiceRequestEvent


class ServiceRequestEventStaffSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = ServiceRequestEvent
        fields = [
            "id", "type", "actor_email", "actor_label", "message", "guest_visible",
            "previous_status", "new_status", "details", "created_at",
        ]
        read_only_fields = fields


class ServiceRequestEventPortalSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceRequestEvent
        fields = ["id", "type", "message", "previous_status", "new_status", "created_at"]
        read_only_fields = fields


class ServiceRequestListSerializer(serializers.ModelSerializer):
    guest_name = serializers.CharField(source="guest.full_name", read_only=True, allow_null=True)
    guest_email = serializers.EmailField(source="guest.email", read_only=True, allow_null=True)
    stay_reference = serializers.CharField(source="stay.reference", read_only=True, allow_null=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True, allow_null=True)
    qr_link_reference = serializers.CharField(source="qr_link.reference", read_only=True, allow_null=True)
    assigned_to_name = serializers.CharField(source="assigned_to.full_name", read_only=True, allow_null=True)
    assigned_to_email = serializers.EmailField(source="assigned_to.email", read_only=True, allow_null=True)
    housekeeping_task_reference = serializers.CharField(source="housekeeping_task.reference", read_only=True, allow_null=True)
    maintenance_work_order_reference = serializers.CharField(source="maintenance_work_order.reference", read_only=True, allow_null=True)
    pos_order_reference = serializers.CharField(source="pos_order.reference", read_only=True, allow_null=True)

    class Meta:
        model = ServiceRequest
        fields = [
            "reference", "guest_name", "guest_email", "stay_reference", "room_number", "table_number",
            "qr_link_reference", "category", "priority", "channel", "status", "owner_team", "assigned_to_name",
            "assigned_to_email", "summary", "due_at",
            "acknowledged_at", "resolved_at", "closed_at", "housekeeping_task_reference",
            "maintenance_work_order_reference", "pos_order_reference", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ServiceRequestDetailSerializer(ServiceRequestListSerializer):
    events = ServiceRequestEventStaffSerializer(many=True, read_only=True)
    detail = serializers.CharField(read_only=True)

    class Meta(ServiceRequestListSerializer.Meta):
        fields = ServiceRequestListSerializer.Meta.fields + ["detail", "events"]


class PortalServiceRequestListSerializer(serializers.ModelSerializer):
    stay_reference = serializers.CharField(source="stay.reference", read_only=True, allow_null=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True, allow_null=True)

    class Meta:
        model = ServiceRequest
        fields = [
            "reference", "stay_reference", "room_number", "category", "priority", "status", "summary",
            "due_at", "acknowledged_at", "resolved_at", "closed_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class PortalServiceRequestDetailSerializer(PortalServiceRequestListSerializer):
    detail = serializers.CharField(read_only=True)
    events = ServiceRequestEventPortalSerializer(source="guest_visible_events", many=True, read_only=True)

    class Meta(PortalServiceRequestListSerializer.Meta):
        fields = PortalServiceRequestListSerializer.Meta.fields + ["detail", "events"]


class StaffServiceRequestCreateSerializer(serializers.Serializer):
    guest_id = serializers.IntegerField(min_value=1)
    stay_reference = serializers.CharField(required=False, allow_blank=False, max_length=56)
    room_id = serializers.IntegerField(required=False, min_value=1)
    category = serializers.ChoiceField(choices=ServiceRequest.Category.choices)
    priority = serializers.ChoiceField(choices=ServiceRequest.Priority.choices, default=ServiceRequest.Priority.NORMAL)
    channel = serializers.ChoiceField(choices=ServiceRequest.Channel.choices, default=ServiceRequest.Channel.FRONT_DESK)
    owner_team = serializers.ChoiceField(choices=ServiceRequest.OwnerTeam.choices, required=False)
    assigned_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    due_at = serializers.DateTimeField(required=False)
    summary = serializers.CharField(max_length=255, trim_whitespace=True)
    detail = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class ServiceRequestAssignSerializer(serializers.Serializer):
    assigned_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    owner_team = serializers.ChoiceField(choices=ServiceRequest.OwnerTeam.choices, required=False)
    due_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)


class ServiceRequestStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ServiceRequest.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    guest_visible = serializers.BooleanField(required=False, default=True)


class ServiceRequestCommentSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=5000, trim_whitespace=True)
    guest_visible = serializers.BooleanField(required=False, default=False)


class PortalServiceRequestCreateSerializer(serializers.Serializer):
    stay_reference = serializers.CharField(max_length=56)
    room_id = serializers.IntegerField(required=False, min_value=1)
    category = serializers.ChoiceField(choices=ServiceRequest.Category.choices)
    priority = serializers.ChoiceField(choices=ServiceRequest.Priority.choices, default=ServiceRequest.Priority.NORMAL)
    summary = serializers.CharField(max_length=255, trim_whitespace=True)
    detail = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)


class PortalServiceRequestCommentSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=5000, trim_whitespace=True)


class PortalServiceRequestCancelSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000)
