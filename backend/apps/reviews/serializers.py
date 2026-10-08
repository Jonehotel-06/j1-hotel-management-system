# apps/reviews/serializers.py
"""Review serializers.

Public serializers expose the absolute minimum needed for the guest flow;
the full review payload only ever travels through the administrator-only
management serializer. Review content is plain text — any HTML is stripped
on the way in (stored-XSS defence, same policy as enquiries).
"""
import re

from rest_framework import serializers

from .models import COMMENT_MAX_LENGTH, COMMENT_MIN_LENGTH, RATING_MAX, RATING_MIN, Review

_TAG_RE = re.compile(r"<[^>]*>")
_REF_RE = re.compile(r"^[A-Za-z0-9._:/#\-]{2,80}$")


def _plain(value: str) -> str:
    """Strip any HTML — review content is plain text only (XSS defence)."""
    return _TAG_RE.sub("", value or "").strip()


class ReviewVerifySerializer(serializers.Serializer):
    """Input for the eligibility check AND the submission endpoint."""

    booking_reference = serializers.CharField(max_length=80)
    email = serializers.EmailField(required=False, allow_blank=True, default="")

    def validate_booking_reference(self, value):
        value = _plain(value)[:80]
        if not value or not _REF_RE.match(value):
            raise serializers.ValidationError("Enter a valid booking reference.")
        return value


class ReviewSubmitSerializer(ReviewVerifySerializer):
    rating = serializers.IntegerField(min_value=RATING_MIN, max_value=RATING_MAX)
    comment = serializers.CharField()
    # Anti-spam honeypot — real guests never see or fill this field.
    website = serializers.CharField(required=False, allow_blank=True, write_only=True, default="")

    def validate_comment(self, value):
        value = _plain(value)
        if len(value) < COMMENT_MIN_LENGTH:
            raise serializers.ValidationError(
                f"Please write at least {COMMENT_MIN_LENGTH} characters about your stay."
            )
        if len(value) > COMMENT_MAX_LENGTH:
            raise serializers.ValidationError(
                f"Reviews are limited to {COMMENT_MAX_LENGTH} characters."
            )
        return value

    def is_honeypot_filled(self):
        initial = getattr(self, "initial_data", {}) or {}
        return bool(str(initial.get("website") or "").strip())


class EligibleStaySerializer(serializers.Serializer):
    """Safe booking summary returned ONLY after successful verification.

    Never includes money, contact details or any other guest's data.
    """

    booking_reference = serializers.CharField()
    room_type_name = serializers.CharField()
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    nights = serializers.IntegerField()
    guest_first_name = serializers.CharField()
    already_reviewed = serializers.BooleanField()


class ReviewAdminSerializer(serializers.ModelSerializer):
    """Full management payload — administrator-only endpoints."""

    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True)
    room_type_name = serializers.CharField(source="booking.room_type.name", read_only=True)
    check_in = serializers.DateField(source="booking.check_in", read_only=True)
    check_out = serializers.DateField(source="booking.check_out", read_only=True)
    guest_email = serializers.EmailField(source="guest.email", read_only=True)
    guest_phone = serializers.CharField(source="guest.phone", read_only=True)
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            "id", "booking_reference", "room_type_name", "check_in", "check_out",
            "guest_name", "guest_email", "guest_phone", "rating", "comment",
            "status", "internal_notes", "reviewed_by_name", "reviewed_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "booking_reference", "room_type_name", "check_in", "check_out",
            "guest_name", "guest_email", "guest_phone", "rating", "comment",
            "reviewed_by_name", "reviewed_at", "created_at", "updated_at",
        ]

    def get_reviewed_by_name(self, obj):
        return obj.reviewed_by.full_name if obj.reviewed_by else ""

    def validate_internal_notes(self, value):
        return _plain(value)[:2000]
