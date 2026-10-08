# apps/offers/serializers.py
from django.utils import timezone
from rest_framework import serializers

from apps.core.storage import absolute_media_url
from apps.rooms.models import RoomType

from .models import GuestDiscount, GuestDiscountApplication, Offer


def _abs(serializer, image_field):
    return absolute_media_url(image_field, serializer.context.get("request"))


class OfferPublicSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    applicable_room_types = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            # `code` is the guest-facing promo code (already public by design —
            # guests type it at booking); no internal/admin field is exposed.
            "id", "title", "slug", "short_description", "description",
            "discount_type", "discount_value", "code", "start_date", "end_date",
            "min_nights", "max_nights", "applicable_room_types", "is_featured",
            "image_url", "terms",
        ]

    def get_image_url(self, obj):
        return _abs(self, obj.image)

    def get_applicable_room_types(self, obj):
        types = obj.room_types.filter(is_active=True)
        if not types.exists():
            return []
        return [{"id": t.pk, "name": t.name, "slug": t.slug} for t in types]


class OfferAdminSerializer(serializers.ModelSerializer):
    room_type_ids = serializers.PrimaryKeyRelatedField(
        queryset=RoomType.objects.all(), many=True, required=False, source="room_types"
    )
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id", "title", "slug", "description", "short_description", "code",
            "discount_type", "discount_value", "start_date", "end_date",
            "min_nights", "max_nights", "room_type_ids", "is_active", "is_featured",
            "image", "image_url", "terms", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]
        extra_kwargs = {
            "image": {"required": False, "write_only": True},
            # Blank input means "no promo code" and must reach the database as
            # NULL — "" would violate the UNIQUE constraint on the second offer.
            "code": {"required": False, "allow_blank": True, "allow_null": True},
        }

    def get_image_url(self, obj):
        return _abs(self, obj.image)

    def validate_code(self, value):
        value = (value or "").strip()
        return value.upper() or None

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": ["End date cannot be before the start date."]})
        discount_type = attrs.get("discount_type", getattr(self.instance, "discount_type", None))
        value = attrs.get("discount_value", getattr(self.instance, "discount_value", None))
        if discount_type == Offer.DiscountType.PERCENTAGE and value is not None and value > 100:
            raise serializers.ValidationError({"discount_value": ["Percentage discounts cannot exceed 100."]})
        min_n = attrs.get("min_nights", getattr(self.instance, "min_nights", 1))
        max_n = attrs.get("max_nights", getattr(self.instance, "max_nights", None))
        if max_n is not None and min_n and max_n < min_n:
            raise serializers.ValidationError({"max_nights": ["Maximum nights must be >= minimum nights."]})
        return attrs


class GuestDiscountSerializer(serializers.ModelSerializer):
    """Staff-facing individual guest discount.

    Every money decision stays server-side: this serializer only records the
    rule. The discount actually applied to a booking is computed by
    apps.offers.services and snapshotted in GuestDiscountApplication.
    """

    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    guest_email = serializers.CharField(source="guest.email", read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True,
                                             allow_null=True)
    is_currently_valid = serializers.SerializerMethodField()
    times_used = serializers.SerializerMethodField()

    class Meta:
        model = GuestDiscount
        fields = [
            "id", "guest", "guest_name", "guest_email", "discount_type",
            "discount_value", "start_date", "end_date", "is_active", "reason",
            "notes", "created_by", "created_by_email", "is_currently_valid",
            "times_used", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_by_email", "guest_name",
                            "guest_email", "is_currently_valid", "times_used",
                            "created_at", "updated_at"]

    def get_is_currently_valid(self, obj) -> bool:
        return obj.is_valid_on(timezone.localdate())

    def get_times_used(self, obj) -> int:
        # Annotated by the viewset queryset — avoids an N+1 across the list.
        count = getattr(obj, "applications_count", None)
        return count if count is not None else obj.applications.count()

    def validate_discount_value(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Enter a discount greater than zero.")
        return value

    def validate(self, attrs):
        instance = self.instance
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": ["End date cannot be before the start date."]}
            )
        discount_type = attrs.get("discount_type", getattr(instance, "discount_type", None))
        value = attrs.get("discount_value", getattr(instance, "discount_value", None))
        if (
            discount_type == GuestDiscount.DiscountType.PERCENTAGE
            and value is not None
            and value > 100
        ):
            raise serializers.ValidationError(
                {"discount_value": ["Percentage discounts cannot exceed 100."]}
            )
        return attrs


class GuestDiscountApplicationSerializer(serializers.ModelSerializer):
    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True)

    class Meta:
        model = GuestDiscountApplication
        fields = [
            "id", "booking", "booking_reference", "guest", "discount_type",
            "discount_value", "amount", "currency", "reason", "created_at",
        ]
        read_only_fields = fields
