# apps/payments/serializers.py
from rest_framework import serializers

from .models import Payment, Refund


class PaymentSerializer(serializers.ModelSerializer):
    """Safe payment representation — never contains gateway secrets/card data."""

    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True)
    guest_name = serializers.CharField(source="booking.guest.full_name", read_only=True)
    staff_email = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id", "reference", "booking_reference", "guest_name", "staff_email",
            "provider", "amount", "currency", "status", "channel",
            "gateway_response", "transaction_id", "paid_at", "notes", "created_at",
        ]
        read_only_fields = fields

    def get_staff_email(self, obj):
        return obj.user.email if obj.user_id else None


class RefundSerializer(serializers.ModelSerializer):
    booking_reference = serializers.CharField(source="booking.booking_reference", read_only=True)
    payment_reference = serializers.CharField(source="payment.reference", read_only=True)
    cancellation_reference = serializers.CharField(source="cancellation_request.cancellation_reference", read_only=True)
    requested_by_email = serializers.CharField(source="requested_by.email", read_only=True)

    class Meta:
        model = Refund
        fields = [
            "id", "booking_reference", "payment_reference", "cancellation_reference",
            "amount", "currency", "status", "paystack_transaction_id",
            "paystack_transaction_reference", "paystack_refund_id", "paystack_refund_reference",
            "customer_note", "merchant_note", "failure_reason", "requested_by_email",
            "submitted_at", "processed_at", "failed_at", "created_at",
        ]
        read_only_fields = fields


class InitializePaymentSerializer(serializers.Serializer):
    booking_reference = serializers.CharField()


class RecordOfflinePaymentSerializer(serializers.Serializer):
    """Front-desk payment capture (cash / POS / bank transfer).

    The amount is validated here for shape only — whether it is *acceptable*
    (i.e. not more than the booking's outstanding balance) is decided by the
    payment service against the database row, never against anything the
    browser computed.
    """

    booking_reference = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=1,
                                      max_value=99_999_999)
    provider = serializers.ChoiceField(
        choices=[Payment.Provider.CASH, Payment.Provider.POS, Payment.Provider.BANK_TRANSFER]
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    # ------------------------------------------------------------------
    # Whole-bill collection (additive; omitting all four keeps the historical
    # "settle the stay projection only" behaviour byte for byte).
    #
    # The desk's problem this solves: a guest has paid for the room but still
    # owes money for restaurant, bar, room-service or laundry charges posted to
    # their folio. ``record_offline_payment`` refused that money outright
    # ("This booking is already marked paid"), so the debt could only be cleared
    # by an operator with finance tools. Supplying any of these four fields asks
    # the service to settle the WHOLE bill in one transaction and apply the
    # folio part to the folio.
    # ------------------------------------------------------------------
    external_reference = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=160,
        help_text="The actual terminal slip or bank transfer reference. Required for POS/transfer.",
    )
    cash_session_reference = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=64,
        help_text="The acting cashier's own open drawer. Required for cash.",
    )
    include_unbilled = serializers.BooleanField(required=False, default=True)
    idempotency_key = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=128,
        help_text="One key per payment attempt; a retry with the same key returns the original payment.",
    )

    def validate_amount(self, value):
        from decimal import Decimal

        value = Decimal(value).quantize(Decimal("0.01"))
        if value <= 0:
            raise serializers.ValidationError("Enter an amount greater than zero.")
        return value

    def validate(self, attrs):
        provider = str(attrs.get("provider") or "").upper()
        external = str(attrs.get("external_reference") or "").strip()
        drawer = str(attrs.get("cash_session_reference") or "").strip()
        if provider == Payment.Provider.CASH and external:
            raise serializers.ValidationError(
                {"external_reference": "Cash does not carry a terminal or bank reference."}
            )
        if provider != Payment.Provider.CASH and drawer:
            raise serializers.ValidationError(
                {"cash_session_reference": "A cash session is only valid for a cash payment."}
            )
        # Opting in to the whole-bill path is explicit: a non-cash collection
        # must state the evidence it was actually paid with.
        if external or drawer or str(attrs.get("idempotency_key") or "").strip():
            if provider != Payment.Provider.CASH and not external:
                raise serializers.ValidationError(
                    {"external_reference": "Enter the actual POS/bank payment reference."}
                )
            if provider == Payment.Provider.CASH and not drawer:
                raise serializers.ValidationError(
                    {"cash_session_reference": "Cash must go into your own open drawer."}
                )
        return attrs
