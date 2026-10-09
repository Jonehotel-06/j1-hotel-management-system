"""Narrow public-intake and staff management contracts for service QR links."""
from rest_framework import serializers

from apps.guest_services.models import ServiceQRLink, ServiceRequest


class ServiceQRLinkSerializer(serializers.ModelSerializer):
    room_number = serializers.CharField(source="room.room_number", read_only=True, allow_null=True)

    class Meta:
        model = ServiceQRLink
        fields = [
            "reference", "target_type", "room", "room_number", "table_number", "label", "is_active",
            "last_used_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ServiceQRLinkCreateSerializer(serializers.Serializer):
    target_type = serializers.ChoiceField(choices=ServiceQRLink.TargetType.choices)
    room_id = serializers.IntegerField(required=False, min_value=1)
    table_number = serializers.CharField(required=False, allow_blank=True, max_length=40)
    label = serializers.CharField(required=False, allow_blank=True, max_length=160)

    def validate(self, attrs):
        target_type = attrs["target_type"]
        if target_type == ServiceQRLink.TargetType.ROOM:
            if not attrs.get("room_id") or attrs.get("table_number", "").strip():
                raise serializers.ValidationError({"target_type": "Choose one active room and leave table number blank."})
        elif attrs.get("room_id") or not attrs.get("table_number", "").strip():
            raise serializers.ValidationError({"target_type": "Enter a table number and do not select a room."})
        return attrs


class ServiceQRRequestCreateSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=ServiceRequest.Category.choices, required=False)
    summary = serializers.CharField(max_length=255, trim_whitespace=True)
    detail = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")
    idempotency_key = serializers.CharField(min_length=16, max_length=128, trim_whitespace=True)

    def validate(self, attrs):
        unexpected = set(self.initial_data or {}) - set(self.fields)
        if unexpected:
            raise serializers.ValidationError({"non_field_errors": "Only request text and category are accepted."})
        return attrs
