# apps/notifications/serializers.py
from rest_framework import serializers

from .models import EmailLog, Notification


class EmailLogSerializer(serializers.ModelSerializer):
    """Delivery status of a transactional email (staff/admin visibility)."""

    status_label = serializers.CharField(source="get_status_display", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    failure_stage_label = serializers.CharField(
        source="get_failure_stage_display", read_only=True
    )

    class Meta:
        model = EmailLog
        fields = [
            "id", "kind", "kind_label", "to_email", "subject",
            "booking_reference", "payment_reference",
            "status", "status_label",
            "error_message", "failure_stage", "failure_stage_label",
            "provider_message_id",
            "created_at", "sent_at", "failed_at",
        ]
        read_only_fields = fields


class UnreadCountSerializer(serializers.Serializer):
    unread_count = serializers.IntegerField(read_only=True)


class NotificationSerializer(serializers.ModelSerializer):
    type_label = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "type", "type_label", "title", "message", "link", "is_read", "created_at"]
        read_only_fields = fields

    def get_type_label(self, obj):
        return obj.get_type_display()


class NotificationDetailSerializer(NotificationSerializer):
    """Full notification for the dashboard detail page.

    ``related`` is derived from the deep link the backend itself generated, so
    the detail page can offer a real "View booking" style action instead of a
    broken hand-built link.
    """

    related = serializers.SerializerMethodField()

    class Meta(NotificationSerializer.Meta):
        fields = NotificationSerializer.Meta.fields + ["related"]

    def get_related(self, obj):
        link = (obj.link or "").strip()
        if not link:
            return None
        page = link.split("?")[0].rstrip("/").split("/")[-1] or ""
        reference = ""
        if "?" in link:
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(link).query)
            reference = (
                query.get("ref") or query.get("reference") or query.get("id") or [""]
            )[0]
        kind = "system"
        lowered = page.lower()
        if "booking" in lowered:
            kind = "booking"
        elif "payment" in lowered or "receipt" in lowered:
            kind = "payment"
        elif "guest" in lowered:
            kind = "guest"
        elif "room" in lowered:
            kind = "room"
        elif "enquir" in lowered:
            kind = "enquiry"
        elif "notification" in lowered:
            kind = "notification"
        if kind == "system" and not reference:
            return None
        return {"kind": kind, "reference": reference or None, "link": link}
