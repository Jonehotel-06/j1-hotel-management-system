# apps/offers/serializers.py
from django.utils import timezone
from rest_framework import serializers

from apps.core.storage import absolute_media_url
from apps.rooms.models import RoomType

from .models import GuestDiscount, GuestDiscountApplication, Offer, OfferGuestRestriction


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
        if value is not None and value <= 0:
            raise serializers.ValidationError({"discount_value": ["Enter a discount greater than zero."]})
        if discount_type == Offer.DiscountType.PERCENTAGE and value is not None and value > 100:
            raise serializers.ValidationError({"discount_value": ["Percentage discounts cannot exceed 100."]})
        min_n = attrs.get("min_nights", getattr(self.instance, "min_nights", 1))
        max_n = attrs.get("max_nights", getattr(self.instance, "max_nights", None))
        if min_n is not None and min_n < 1:
            raise serializers.ValidationError({"min_nights": ["Minimum nights must be at least one."]})
        if max_n is not None and max_n < 1:
            raise serializers.ValidationError({"max_nights": ["Maximum nights must be at least one."]})
        if max_n is not None and min_n and max_n < min_n:
            raise serializers.ValidationError({"max_nights": ["Maximum nights must be >= minimum nights."]})
        return attrs


class OfferGuestRestrictionSerializer(serializers.ModelSerializer):
    """One guest barred from one offer.

    ``guest`` is written as an id; everything else about the guest (name,
    email) is read-only and resolved from the database, so a request can never
    assert who the guest is. ``offer`` is writeable on create so the
    restrictions collection can be used from either side of the relationship
    (the offer card or the guest card) with one endpoint.
    """

    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    guest_email = serializers.CharField(source="guest.email", read_only=True)
    offer_title = serializers.CharField(source="offer.title", read_only=True)
    offer_code = serializers.CharField(source="offer.code", read_only=True, allow_null=True)
    created_by_email = serializers.CharField(source="created_by.email", read_only=True,
                                             allow_null=True)
    removed_by_email = serializers.CharField(source="removed_by.email", read_only=True,
                                             allow_null=True)

    class Meta:
        model = OfferGuestRestriction
        fields = [
            "id", "offer", "offer_title", "offer_code", "guest", "guest_name",
            "guest_email", "reason", "is_active", "created_by", "created_by_email",
            "removed_by", "removed_by_email", "removed_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "offer_title", "offer_code", "guest_name", "guest_email",
            "created_by", "created_by_email", "removed_by", "removed_by_email",
            "removed_at", "created_at", "updated_at",
        ]

    def get_unique_together_validators(self):
        """Allow a repeat CREATE for the same (offer, guest) pair.

        DRF turns the model's ``unique_offer_guest_restriction`` constraint into
        a validator that runs BEFORE ``perform_create``, so "restrict this guest
        again" would come back as a 400 instead of re-imposing a restriction
        that was lifted earlier. Re-restricting is a legitimate, common action
        (the desk lifts a bar while a dispute is settled, then puts it back),
        so the request is allowed through and the view updates the existing row.

        Uniqueness is still enforced where it belongs — by the database
        constraint, which cannot be raced by two terminals. A duplicate row can
        therefore never be written; the worst case is a constraint error, not
        two live restrictions for one pair.
        """
        return []

    def validate(self, attrs):
        # Editing an existing row may not silently re-point it at another
        # guest/offer pair: the audit trail has to describe one decision.
        if self.instance is not None:
            if "guest" in attrs and attrs["guest"].pk != self.instance.guest_id:
                raise serializers.ValidationError(
                    {"guest": ["A restriction cannot be moved to a different guest. "
                               "Remove it and create a new one."]}
                )
            if "offer" in attrs and attrs["offer"].pk != self.instance.offer_id:
                raise serializers.ValidationError(
                    {"offer": ["A restriction cannot be moved to a different offer. "
                               "Remove it and create a new one."]}
                )
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
