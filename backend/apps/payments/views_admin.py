# apps/payments/views_admin.py
"""Staff payment management: list / detail / record offline payments."""
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.bookings.services import booking_service
from apps.core.permissions import IsStaffRole
from apps.core.responses import success_response
from apps.core.utils import money

from .models import Payment, Refund
from .serializers import PaymentSerializer, RecordOfflinePaymentSerializer, RefundSerializer
from .services import payment_service


@extend_schema(tags=["Admin · Payments"])
class AdminPaymentListView(generics.ListAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = PaymentSerializer

    def get_queryset(self):
        qs = Payment.objects.select_related("booking", "booking__guest", "user").order_by("-created_at")
        params = self.request.query_params
        if status_param := params.get("status"):
            qs = qs.filter(status=status_param.upper())
        if provider := params.get("provider"):
            qs = qs.filter(provider=provider.upper())
        if date_from := params.get("date_from"):
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to := params.get("date_to"):
            qs = qs.filter(created_at__date__lte=date_to)
        if date := params.get("date"):
            qs = qs.filter(created_at__date=date)
        if paid_on := params.get("paid_on"):
            qs = qs.filter(paid_at__date=paid_on)
        if search := params.get("search"):
            qs = qs.filter(
                Q(reference__icontains=search)
                | Q(transaction_id__icontains=search)
                | Q(booking__booking_reference__icontains=search)
                | Q(booking__guest__first_name__icontains=search)
                | Q(booking__guest__last_name__icontains=search)
                | Q(booking__guest__email__icontains=search)
            )
        return qs


@extend_schema(tags=["Admin · Payments"])
class AdminPaymentDetailView(generics.RetrieveAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = PaymentSerializer

    def get_queryset(self):
        return Payment.objects.select_related("booking", "booking__guest", "user")

    def get_object(self):
        lookup = self.kwargs["lookup"]
        payment = self.get_queryset().filter(
            Q(reference=lookup) | Q(pk=lookup if str(lookup).isdigit() else -1)
        ).first()
        if payment is None:
            raise NotFound()
        return payment

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)


@extend_schema(tags=["Admin · Payments"], summary="List refunds and their Paystack reconciliation status")
class AdminRefundListView(generics.ListAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = RefundSerializer

    def get_queryset(self):
        qs = Refund.objects.select_related(
            "booking", "booking__guest", "payment", "cancellation_request", "requested_by"
        ).order_by("-created_at")
        params = self.request.query_params
        if status_param := params.get("status"):
            qs = qs.filter(status=status_param.upper())
        if booking_reference := params.get("booking_reference"):
            qs = qs.filter(booking__booking_reference=booking_reference)
        if payment_reference := params.get("payment_reference"):
            qs = qs.filter(payment__reference=payment_reference)
        if search := params.get("search"):
            qs = qs.filter(
                Q(booking__booking_reference__icontains=search)
                | Q(payment__reference__icontains=search)
                | Q(payment__transaction_id__icontains=search)
                | Q(paystack_refund_id__icontains=search)
                | Q(paystack_refund_reference__icontains=search)
                | Q(cancellation_request__cancellation_reference__icontains=search)
                | Q(booking__guest__email__icontains=search)
            )
        return qs


@extend_schema(tags=["Admin · Payments"], summary="Retrieve a refund")
class AdminRefundDetailView(generics.RetrieveAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = RefundSerializer

    def get_queryset(self):
        return Refund.objects.select_related("booking", "booking__guest", "payment", "cancellation_request", "requested_by")

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)


@extend_schema(tags=["Admin · Payments"], summary="Record a cash/POS/transfer payment")
class AdminRecordOfflinePaymentView(APIView):
    permission_classes = [IsStaffRole]
    serializer_class = RecordOfflinePaymentSerializer

    def post(self, request):
        serializer = RecordOfflinePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = booking_service.get_booking_by_reference_or_id(data["booking_reference"])
        if booking is None:
            raise NotFound("Booking not found.")
        payment = payment_service.record_offline_payment(
            booking=booking,
            staff_user=request.user,
            amount=data["amount"],
            provider=data["provider"],
            notes=data.get("notes", ""),
            request=request,
        )
        # The payment modal must be able to refresh itself from THIS response
        # alone — no second round-trip and never a full bookings-table reload.
        booking.refresh_from_db()
        payload = PaymentSerializer(payment).data
        payload["booking"] = {
            "id": booking.pk,
            "booking_reference": booking.booking_reference,
            "status": booking.status,
            "payment_status": booking.payment_status,
            "currency": booking.currency,
            "total_amount": money(booking.total_amount),
            "amount_paid": money(booking.amount_paid),
            "amount_due": money(booking.amount_due),
            "guest_name": booking.guest.full_name,
            "guest_email": booking.guest.email,
            "guest_phone": booking.guest.phone,
            "room_type_name": booking.room_type.name,
            "room_numbers": [
                a.room.room_number
                for a in booking.room_assignments.select_related("room")
                .order_by("room__room_number")
            ],
            "check_in": booking.check_in.isoformat(),
            "check_out": booking.check_out.isoformat(),
            "nights": booking.nights,
            "number_of_guests": booking.number_of_guests,
        }
        payload["receipt_reference"] = payment.reference
        payload["has_receipt"] = True
        return success_response(
            payload,
            message="Payment recorded.",
            status=status.HTTP_201_CREATED,
        )
