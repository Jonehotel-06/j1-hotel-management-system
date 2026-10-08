# apps/hotel/serializers.py
from django.conf import settings as django_settings
from rest_framework import serializers

from apps.core.storage import absolute_media_url

from .models import Facility, HotelPolicy, HotelSettings


class HotelPublicSerializer(serializers.ModelSerializer):
    """Everything the public website needs to render hotel information."""

    # The hotel operates in the server's configured timezone (Africa/Lagos).
    # The frontend uses this to compute "today"/date constraints correctly
    # for visitors in other timezones — it must come from the backend, not
    # from the browser clock.
    timezone = serializers.SerializerMethodField()

    class Meta:
        model = HotelSettings
        fields = [
            "hotel_name", "tagline", "description", "address", "city", "state",
            "country", "phone", "email", "google_maps_url", "google_review_url",
            "social_links", "check_in_time", "check_out_time", "currency",
            "min_stay_nights", "max_stay_nights", "timezone",
        ]
        read_only_fields = fields

    def get_timezone(self, obj):
        return django_settings.TIME_ZONE


class HotelPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = HotelPolicy
        fields = ["id", "key", "title", "content", "is_active", "display_order", "updated_at"]
        read_only_fields = ["id", "updated_at"]


class FacilitySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Facility
        fields = ["id", "name", "slug", "description", "icon", "image_url", "is_active", "display_order"]
        read_only_fields = ["id"]

    def get_image_url(self, obj):
        return absolute_media_url(obj.image, self.context.get("request"))


class HotelSettingsAdminSerializer(serializers.ModelSerializer):
    """Full settings payload for staff; only ADMIN may write."""

    class Meta:
        model = HotelSettings
        fields = [
            "hotel_name", "tagline", "description", "address", "city", "state",
            "country", "phone", "email", "google_maps_url", "google_review_url",
            "social_links",
            "check_in_time", "check_out_time", "min_stay_nights", "max_stay_nights",
            "currency", "tax_rate_percent", "service_fee", "deposit_percent",
            "pending_booking_minutes", "cancellation_deadline_hours",
            "cancellation_fee_percent", "restrict_check_in_to_booked_date",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def validate_social_links(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of objects.")
        for item in value:
            if not isinstance(item, dict) or "platform" not in item or "url" not in item:
                raise serializers.ValidationError('Each entry needs "platform" and "url".')
        return value

    def validate_pending_booking_minutes(self, value):
        # 0 would expire every new unpaid hold instantly; the model field is a
        # PositiveIntegerField (allows 0), so the operational floor lives here.
        if value < 1:
            raise serializers.ValidationError("The pending window must be at least 1 minute.")
        return value
