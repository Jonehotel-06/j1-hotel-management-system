"""Input validation for the guest portal's verified-email entry flow."""
from rest_framework import serializers

from .services import normalize_portal_email


class PortalAccessRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return normalize_portal_email(value)


class PortalAccessConsumeSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True, min_length=32, max_length=256)
