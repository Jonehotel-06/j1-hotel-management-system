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


# ---------------------------------------------------------------------------
# Laundry
# ---------------------------------------------------------------------------
from .models import LaundryItem, LaundryOrder


class LaundryItemSerializer(serializers.ModelSerializer):
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = LaundryItem
        fields = ["id", "description", "quantity", "service_type", "unit_price", "line_total"]
        read_only_fields = fields

    def get_line_total(self, obj):
        return str(obj.line_total)


class LaundryOrderSerializer(serializers.ModelSerializer):
    items = LaundryItemSerializer(many=True, read_only=True)
    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    stay_reference = serializers.CharField(source="stay.reference", read_only=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True, allow_null=True)
    assigned_to_email = serializers.EmailField(source="assigned_to.email", read_only=True, allow_null=True)
    received_by_email = serializers.EmailField(source="received_by.email", read_only=True, allow_null=True)
    folio_reference = serializers.CharField(source="folio.reference", read_only=True, allow_null=True)
    financial_reference = serializers.CharField(source="financial_transaction.reference", read_only=True, allow_null=True)

    class Meta:
        model = LaundryOrder
        fields = [
            "reference", "status", "stay_reference", "guest_name", "room_number", "items",
            "total_amount", "currency", "assigned_to_email", "received_by_email",
            "billed_at", "folio_reference", "financial_reference", "ready_at",
            "delivered_at", "delivered_to", "notes", "created_at",
        ]
        read_only_fields = fields


class LaundryItemInputSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=160)
    quantity = serializers.IntegerField(min_value=1, max_value=500, default=1)
    service_type = serializers.ChoiceField(choices=LaundryItem.ServiceType.choices, default=LaundryItem.ServiceType.WASH)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)


class LaundryCreateSerializer(serializers.Serializer):
    stay_reference = serializers.CharField(max_length=64)
    room_id = serializers.IntegerField(required=False, allow_null=True)
    items = LaundryItemInputSerializer(many=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")
    idempotency_key = serializers.CharField(max_length=160)


class LaundryAssignSerializer(serializers.Serializer):
    assignee_id = serializers.IntegerField()


class LaundryStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=LaundryOrder.Status.choices)
    delivered_to = serializers.CharField(required=False, allow_blank=True, max_length=160, default="")
