# apps/audit/serializers.py
from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            "id", "actor_email", "actor_name", "action", "object_type", "object_id",
            "summary", "changes", "metadata", "ip_address", "created_at",
        ]
        read_only_fields = fields

    def get_actor_email(self, obj) -> str | None:
        return obj.actor.email if obj.actor else None

    def get_actor_name(self, obj) -> str:
        return obj.actor.full_name if obj.actor else "System"
