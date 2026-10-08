"""Narrow guest-facing folio statement representations."""
from rest_framework import serializers

from .models import Folio, FolioPosting


class PortalFolioSerializer(serializers.ModelSerializer):
    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True, allow_null=True)
    stay_reference = serializers.CharField(source="stay.reference", read_only=True, allow_null=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Folio
        fields = [
            "reference", "kind", "status", "currency", "booking_reference", "stay_reference",
            "balance", "opened_at", "closed_at",
        ]
        read_only_fields = fields


class PortalFolioPostingSerializer(serializers.ModelSerializer):
    class Meta:
        model = FolioPosting
        fields = [
            "id", "kind", "effect", "amount", "currency", "description", "business_date",
            "source_reference", "created_at",
        ]
        read_only_fields = fields
