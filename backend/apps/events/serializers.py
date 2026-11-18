# apps/events/serializers.py
from rest_framework import serializers

from .models import EventLocation, EventPayment, HotelEvent


class EventLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventLocation
        fields = ["id", "code", "name", "description", "is_active", "display_order"]


class EventPaymentSerializer(serializers.ModelSerializer):
    received_by_name = serializers.CharField(source="received_by.full_name", read_only=True, default="")
    event_reference = serializers.CharField(source="event.reference", read_only=True)

    class Meta:
        model = EventPayment
        fields = [
            "id", "event", "event_reference", "amount", "currency", "method",
            "reference", "external_reference", "status", "received_by",
            "received_by_name", "paid_at", "notes", "created_at",
        ]
        read_only_fields = fields


class HotelEventSerializer(serializers.ModelSerializer):
    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    guest_phone = serializers.CharField(source="guest.phone", read_only=True)
    guest_email = serializers.CharField(source="guest.email", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)
    outstanding_balance = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default="")

    class Meta:
        model = HotelEvent
        fields = [
            "id", "reference", "guest", "guest_name", "guest_phone", "guest_email",
            "location", "location_name", "event_type", "event_date", "start_time",
            "end_time", "expected_guests", "currency", "total_charged", "amount_paid",
            "outstanding_balance", "payment_status", "status", "notes", "created_by",
            "created_by_name", "cancelled_at", "cancellation_reason", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_outstanding_balance(self, obj):
        return str(obj.outstanding_balance)


class HotelEventDetailSerializer(HotelEventSerializer):
    payments = EventPaymentSerializer(many=True, read_only=True)

    class Meta(HotelEventSerializer.Meta):
        fields = HotelEventSerializer.Meta.fields + ["payments"]


class EventCreateSerializer(serializers.Serializer):
    guest_id = serializers.IntegerField(required=False, allow_null=True)
    # New-guest fields (used only when guest_id is absent).
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)
    location_id = serializers.IntegerField()
    event_type = serializers.CharField(required=False, allow_blank=True, max_length=120)
    event_date = serializers.DateField()
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    expected_guests = serializers.IntegerField(min_value=1, default=1)
    total_charged = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=96)
    override = serializers.BooleanField(required=False, default=False)
    override_reason = serializers.CharField(required=False, allow_blank=True)
    # Optional initial payment recorded with the registration.
    amount_paid = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, default=None)
    payment_method = serializers.CharField(required=False, allow_blank=True, max_length=20)
    payment_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    payment_idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=96)

    def validate(self, attrs):
        if not attrs.get("guest_id"):
            if not (attrs.get("first_name") and attrs.get("last_name") and attrs.get("phone")):
                raise serializers.ValidationError({
                    "guest_id": "Select an existing guest or provide first name, last name and phone for a new guest."
                })
        if attrs.get("amount_paid") is not None and attrs["amount_paid"] > 0 and not attrs.get("payment_method"):
            raise serializers.ValidationError({"payment_method": "Choose a payment method for the amount paid."})
        return attrs


class EventPaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    method = serializers.CharField(max_length=20)
    external_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=96)


class EventCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
