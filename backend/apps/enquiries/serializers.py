# apps/enquiries/serializers.py
import re

from django.db.models import Sum
from rest_framework import serializers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field

from apps.core.utils import money
from apps.payments.models import Refund

from .models import Enquiry
from .services import CANCELLATION_SUBJECT, is_cancellation_subject

_TAG_RE = re.compile(r"<[^>]*>")
_REF_RE = re.compile(r"^[A-Za-z0-9._:/#\-]{2,80}$")


def _plain(value: str) -> str:
    """Strip any HTML — enquiry content is plain text only (XSS defence)."""
    return _TAG_RE.sub("", value or "").strip()


def _clean_reference(value: str) -> str:
    value = _plain(value)[:80]
    if value and not _REF_RE.match(value):
        raise serializers.ValidationError("Use only letters, numbers, dashes and common reference characters.")
    return value


class EnquiryCreateSerializer(serializers.ModelSerializer):
    """Public contact form.

    `website` is an anti-spam honeypot, not an Enquiry column. Cancellation /
    refund requests use structured fields; references are stored for staff
    lookup but are not treated as authorization and are not exposed as valid or
    invalid to the guest.
    """

    website = serializers.CharField(required=False, allow_blank=True, write_only=True, default="")
    enquiry_type = serializers.ChoiceField(
        choices=Enquiry.EnquiryType.choices, required=False, default=Enquiry.EnquiryType.GENERAL
    )
    booking_reference = serializers.CharField(required=False, allow_blank=True, default="")
    payment_reference = serializers.CharField(required=False, allow_blank=True, default="")
    receipt_reference = serializers.CharField(required=False, allow_blank=True, default="")
    cancellation_reason = serializers.CharField(required=False, allow_blank=True, default="")
    preferred_contact_method = serializers.ChoiceField(
        choices=Enquiry.PreferredContactMethod.choices, required=False, allow_blank=True, default=""
    )
    refund_requested = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = Enquiry
        fields = [
            "name", "email", "phone", "subject", "message", "website",
            "enquiry_type", "booking_reference", "payment_reference", "receipt_reference",
            "cancellation_reason", "preferred_contact_method", "refund_requested",
        ]

    def is_honeypot_filled(self):
        initial = getattr(self, "initial_data", {}) or {}
        return bool(str(initial.get("website") or self.validated_data.get("website") or "").strip())

    def validate_name(self, value):
        value = _plain(value)
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value[:120]

    def validate_phone(self, value):
        return _plain(value)[:20]

    def validate_subject(self, value):
        value = _plain(value)
        if not value:
            raise serializers.ValidationError("Subject is required.")
        return value[:150]

    def validate_message(self, value):
        value = _plain(value)
        if len(value) < 5:
            raise serializers.ValidationError("Please enter a more detailed message.")
        return value[:5000]

    def validate_booking_reference(self, value):
        return _clean_reference(value)

    def validate_payment_reference(self, value):
        return _clean_reference(value)

    def validate_receipt_reference(self, value):
        return _clean_reference(value)

    def validate_cancellation_reason(self, value):
        return _plain(value)[:5000]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        subject = attrs.get("subject") or ""
        is_cancel = attrs.get("enquiry_type") == Enquiry.EnquiryType.CANCELLATION or is_cancellation_subject(subject)
        if is_cancel:
            attrs["enquiry_type"] = Enquiry.EnquiryType.CANCELLATION
            attrs["subject"] = CANCELLATION_SUBJECT
            if not attrs.get("booking_reference"):
                raise serializers.ValidationError({
                    "booking_reference": ["Please enter your booking reference for a cancellation/refund request."]
                })
            if not (attrs.get("cancellation_reason") or attrs.get("message")):
                raise serializers.ValidationError({
                    "cancellation_reason": ["Please tell us why you want to cancel."]
                })
        else:
            attrs["enquiry_type"] = Enquiry.EnquiryType.GENERAL
            for field in ("booking_reference", "payment_reference", "receipt_reference", "cancellation_reason", "preferred_contact_method"):
                attrs.pop(field, None)
            attrs["refund_requested"] = False
        return attrs


class CancellationActionSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=5000)
    resolution = serializers.CharField(required=False, allow_blank=True, default="", max_length=5000)
    customer_note = serializers.CharField(required=False, allow_blank=True, default="", max_length=5000)
    merchant_note = serializers.CharField(required=False, allow_blank=True, default="", max_length=5000)


class EnquiryAdminSerializer(serializers.ModelSerializer):
    related_booking_detail = serializers.SerializerMethodField()
    related_payment_detail = serializers.SerializerMethodField()
    refund_detail = serializers.SerializerMethodField()
    cancellation_policy = serializers.SerializerMethodField()
    actions = serializers.SerializerMethodField()
    processed_by_email = serializers.CharField(source="processed_by.email", read_only=True, allow_null=True)

    class Meta:
        model = Enquiry
        fields = [
            "id", "name", "email", "phone", "subject", "message", "status",
            "internal_notes", "enquiry_type", "cancellation_reference", "booking_reference",
            "payment_reference", "receipt_reference", "cancellation_reason",
            "preferred_contact_method", "refund_requested", "related_booking",
            "related_payment", "related_booking_detail", "related_payment_detail",
            "cancellation_status", "refund_status", "calculated_cancellation_fee",
            "calculated_refund_amount", "paystack_refund_reference", "processed_by",
            "processed_by_email", "processed_at", "resolution", "metadata", "refund_detail",
            "cancellation_policy", "actions", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "name", "email", "phone", "subject", "message", "enquiry_type",
            "cancellation_reference", "booking_reference", "payment_reference", "receipt_reference",
            "cancellation_reason", "preferred_contact_method", "refund_requested", "related_booking",
            "related_payment", "related_booking_detail", "related_payment_detail",
            "cancellation_status", "refund_status", "calculated_cancellation_fee",
            "calculated_refund_amount", "paystack_refund_reference", "processed_by",
            "processed_by_email", "processed_at", "resolution", "metadata", "refund_detail",
            "cancellation_policy", "actions", "created_at", "updated_at",
        ]

    def validate_status(self, value):
        if value not in Enquiry.Status.values:
            raise serializers.ValidationError("Unknown status.")
        return value

    def _booking(self, obj):
        return obj.related_booking

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_related_booking_detail(self, obj) -> dict[str, object] | None:
        b = self._booking(obj)
        if not b:
            return None
        return {
            "id": b.pk,
            "booking_reference": b.booking_reference,
            "status": b.status,
            "payment_status": b.payment_status,
            "guest_name": b.guest.full_name,
            "guest_email": b.guest.email,
            "guest_phone": b.guest.phone,
            "room_type_name": b.room_type.name,
            "check_in": b.check_in.isoformat(),
            "check_out": b.check_out.isoformat(),
            "nights": b.nights,
            "total_amount": money(b.total_amount),
            "amount_paid": money(b.amount_paid),
            "amount_due": money(b.amount_due),
            "refund_amount": money(b.refund_amount),
            "currency": b.currency,
            "cancelled_at": b.cancelled_at.isoformat() if b.cancelled_at else None,
        }

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_related_payment_detail(self, obj) -> dict[str, object] | None:
        p = obj.related_payment
        if not p:
            return None
        processed = p.refunds.filter(status=Refund.Status.PROCESSED).aggregate(total=Sum("amount"))["total"] or 0
        remaining = p.amount - processed
        return {
            "id": p.pk,
            "reference": p.reference,
            "paystack_reference": p.paystack_reference,
            "transaction_id": p.transaction_id,
            "provider": p.provider,
            "amount": money(p.amount),
            "currency": p.currency,
            "status": p.status,
            "channel": p.channel,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "gateway_response": p.gateway_response,
            "refunded_amount": money(processed),
            "refundable_amount": money(remaining if remaining > 0 else 0),
        }

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_refund_detail(self, obj) -> dict[str, object] | None:
        latest = obj.refunds.select_related("payment", "booking", "requested_by").order_by("-created_at").first()
        if not latest:
            return None
        return {
            "id": latest.pk,
            "amount": money(latest.amount),
            "currency": latest.currency,
            "status": latest.status,
            "paystack_transaction_reference": latest.paystack_transaction_reference,
            "paystack_transaction_id": latest.paystack_transaction_id,
            "paystack_refund_reference": latest.paystack_refund_reference,
            "paystack_refund_id": latest.paystack_refund_id,
            "requested_by": latest.requested_by.email if latest.requested_by_id else None,
            "requested_at": latest.requested_at.isoformat() if latest.requested_at else None,
            "processed_at": latest.processed_at.isoformat() if latest.processed_at else None,
            "failed_at": latest.failed_at.isoformat() if latest.failed_at else None,
            "failure_reason": latest.failure_reason,
        }

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_cancellation_policy(self, obj) -> dict[str, object] | None:
        if obj.enquiry_type != Enquiry.EnquiryType.CANCELLATION or not obj.related_booking_id:
            return None
        from apps.bookings.services.booking_service import calculate_cancellation_policy

        p = calculate_cancellation_policy(obj.related_booking)
        return {
            "cancellation_deadline": p["deadline"].isoformat(),
            "within_free_cancellation_window": p["within_free_cancellation_window"],
            "cancellation_fee_percent": money(p["fee_percent"]),
            "cancellation_fee": money(p["cancellation_fee"]),
            "refund_amount": money(p["refund_amount"]),
            "amount_paid": money(p["amount_paid"]),
            "currency": obj.related_booking.currency,
        }

    def get_actions(self, obj) -> list[str]:
        if obj.enquiry_type != Enquiry.EnquiryType.CANCELLATION:
            return []
        actions = []
        if obj.cancellation_status == Enquiry.CancellationStatus.NEW:
            actions.append("review")
        if obj.cancellation_status in (Enquiry.CancellationStatus.NEW, Enquiry.CancellationStatus.UNDER_REVIEW):
            actions.extend(["approve_cancellation", "reject_cancellation"])
        if (
            obj.cancellation_status in (Enquiry.CancellationStatus.CANCELLED, Enquiry.CancellationStatus.REFUND_FAILED)
            and obj.refund_status in (Enquiry.RefundStatus.DUE, Enquiry.RefundStatus.FAILED, Enquiry.RefundStatus.NEEDS_ATTENTION)
            and obj.related_payment_id
        ):
            actions.append("process_refund")
        if obj.cancellation_status not in (Enquiry.CancellationStatus.CLOSED,):
            actions.append("close")
        return actions


class PublicCancellationStatusSerializer(serializers.ModelSerializer):
    status_label = serializers.SerializerMethodField()
    booking_status = serializers.SerializerMethodField()
    refund_amount = serializers.SerializerMethodField()
    refund_reference = serializers.SerializerMethodField()
    payment_provider_reference = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()

    class Meta:
        model = Enquiry
        fields = [
            "cancellation_reference", "booking_reference", "payment_reference",
            "cancellation_status", "status_label", "booking_status", "refund_status",
            "refund_amount", "refund_reference", "payment_provider_reference", "message",
            "created_at", "updated_at",
        ]

    def get_status_label(self, obj) -> str:
        return obj.get_cancellation_status_display()

    def get_booking_status(self, obj) -> str | None:
        return obj.related_booking.status if obj.related_booking_id else None

    def get_refund_amount(self, obj) -> str:
        if obj.refund_status in (
            Enquiry.RefundStatus.DUE,
            Enquiry.RefundStatus.PENDING,
            Enquiry.RefundStatus.PROCESSING,
            Enquiry.RefundStatus.PROCESSED,
            Enquiry.RefundStatus.FAILED,
            Enquiry.RefundStatus.NEEDS_ATTENTION,
        ):
            return money(obj.calculated_refund_amount)
        return "0.00"

    def get_refund_reference(self, obj) -> str:
        if obj.refund_status in (Enquiry.RefundStatus.PENDING, Enquiry.RefundStatus.PROCESSING, Enquiry.RefundStatus.PROCESSED):
            return obj.paystack_refund_reference or ""
        return ""

    def get_payment_provider_reference(self, obj) -> str:
        if obj.related_payment and obj.related_payment.provider == "PAYSTACK":
            return obj.related_payment.reference
        return obj.payment_reference or ""

    def get_message(self, obj) -> str:
        status = obj.cancellation_status
        refund = obj.refund_status
        if status == Enquiry.CancellationStatus.NEW:
            return "Your request has been received and is waiting for hotel review. Your booking has not been cancelled yet."
        if status == Enquiry.CancellationStatus.UNDER_REVIEW:
            return "The hotel team is reviewing your booking and payment details. Your booking has not been cancelled yet."
        if status == Enquiry.CancellationStatus.REJECTED:
            return "Your cancellation request was reviewed but not approved. Please contact the hotel if you need more information."
        if status in (Enquiry.CancellationStatus.CANCELLED, Enquiry.CancellationStatus.REFUND_PENDING):
            if refund == Enquiry.RefundStatus.DUE:
                return "Your booking has been cancelled. A refund amount has been calculated but has not yet been completed."
            if refund == Enquiry.RefundStatus.PENDING:
                return "Your refund request has been submitted to Paystack. We are awaiting confirmation."
            return "Your booking has been cancelled."
        if status == Enquiry.CancellationStatus.REFUND_PROCESSING:
            return "Paystack is processing your refund. Bank/card settlement can still take several business days."
        if status == Enquiry.CancellationStatus.REFUNDED:
            return "Paystack has confirmed that your refund was processed. Your bank/card provider may take additional business days to reflect it."
        if status == Enquiry.CancellationStatus.REFUND_FAILED:
            return "The refund needs further attention from the hotel team. Your booking remains cancelled; staff will follow up."
        return "Please contact the hotel for the latest update on this request."
