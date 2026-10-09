# apps/audit/serializers.py
from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()
    terminal_reference = serializers.CharField(source="terminal.reference", read_only=True, allow_null=True)
    terminal_name = serializers.CharField(source="terminal.name", read_only=True, allow_null=True)

    class Meta:
        model = AuditLog
        fields = [
            "id", "actor_email", "actor_name", "actor_role", "actor_department", "terminal_reference",
            "terminal_name", "request_id", "action", "object_type", "object_id", "summary", "changes",
            "metadata", "ip_address", "created_at",
        ]
        read_only_fields = fields

    def get_actor_email(self, obj) -> str | None:
        return obj.actor.email if obj.actor else None

    def get_actor_name(self, obj) -> str:
        return obj.actor.full_name if obj.actor else "System"
