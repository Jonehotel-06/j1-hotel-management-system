# apps/enquiries/views.py
import logging

from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import IsManagerOrAdmin, IsStaffRole
from apps.core.responses import success_response

from .models import Enquiry
from .serializers import (
    CancellationActionSerializer,
    EnquiryAdminSerializer,
    EnquiryCreateSerializer,
    PublicCancellationStatusSerializer,
)
from . import services

logger = logging.getLogger("apps")


@extend_schema(tags=["Enquiries"], summary="Submit a contact/enquiry message")
class EnquiryCreateView(APIView):
    permission_classes = [AllowAny]
    serializer_class = EnquiryCreateSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "enquiry"

    def post(self, request):
        serializer = EnquiryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.is_honeypot_filled():
            # Silently "accept" spam: no row, no signal to the bot.
            logger.info("Honeypot enquiry dropped from %s", request.META.get("REMOTE_ADDR"))
            return success_response(
                message="Thank you for your message. The hotel team will respond shortly.",
                status=status.HTTP_201_CREATED,
            )

        enquiry = services.create_enquiry(serializer.validated_data, request=request)
        payload = {}
        message = "Thank you for your message. The hotel team will respond shortly."
        if enquiry.enquiry_type == Enquiry.EnquiryType.CANCELLATION:
            payload = {
                "cancellation_reference": enquiry.cancellation_reference,
                "status_url": services.cancellation_status_link(
                    enquiry, getattr(enquiry, "raw_public_access_token", "")
                ),
                "booking_reference": enquiry.booking_reference,
                "status": enquiry.cancellation_status,
            }
            message = "Cancellation/refund request received. Your booking has not been cancelled until the hotel reviews and approves it."
        return success_response(payload, message=message, status=status.HTTP_201_CREATED)


@extend_schema(tags=["Enquiries"], summary="Safe public cancellation request status")
class CancellationStatusView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PublicCancellationStatusSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "enquiry"

    def get(self, request, reference):
        token = request.query_params.get("token") or request.headers.get("X-Cancellation-Access-Token", "")
        enquiry = services.get_public_cancellation_status(reference, token)
        return success_response(PublicCancellationStatusSerializer(enquiry).data)


@extend_schema(tags=["Admin · Enquiries"])
class EnquiryAdminListView(generics.ListAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = EnquiryAdminSerializer

    def get_queryset(self):
        qs = (
            Enquiry.objects.select_related(
                "related_booking", "related_booking__guest", "related_booking__room_type",
                "related_payment", "processed_by",
            )
            .prefetch_related("refunds")
            .order_by("-created_at")
        )
        params = self.request.query_params
        if status_param := params.get("status"):
            qs = qs.filter(status=status_param.upper())
        if enquiry_type := params.get("type") or params.get("enquiry_type"):
            qs = qs.filter(enquiry_type=enquiry_type.upper())
        if cancellation_status := params.get("cancellation_status"):
            qs = qs.filter(cancellation_status=cancellation_status.upper())
        if refund_status := params.get("refund_status"):
            qs = qs.filter(refund_status=refund_status.upper())
        if search := params.get("search"):
            from django.db.models import Q

            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone__icontains=search)
                | Q(subject__icontains=search)
                | Q(message__icontains=search)
                | Q(cancellation_reference__icontains=search)
                | Q(booking_reference__icontains=search)
                | Q(payment_reference__icontains=search)
                | Q(related_booking__booking_reference__icontains=search)
                | Q(related_payment__reference__icontains=search)
                | Q(related_payment__transaction_id__icontains=search)
            ).distinct()
        return qs


@extend_schema(tags=["Admin · Enquiries"])
class EnquiryAdminDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsStaffRole]
    http_method_names = ["get", "patch", "head", "options"]
    serializer_class = EnquiryAdminSerializer

    def get_queryset(self):
        return Enquiry.objects.select_related(
            "related_booking", "related_booking__guest", "related_booking__room_type",
            "related_payment", "processed_by",
        ).prefetch_related("refunds")

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object()).data)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        before = {"status": instance.status, "internal_notes": instance.internal_notes}
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        if before["status"] != instance.status:
            from apps.audit.services import log_action

            log_action(actor=request.user, action="ENQUIRY_STATUS_CHANGED", instance=instance,
                       changes={"status": [before["status"], instance.status]}, request=request)
        return success_response(self.get_serializer(instance).data, message="Enquiry updated.")


class _CancellationActionView(APIView):
    permission_classes = [IsStaffRole]
    serializer_class = CancellationActionSerializer

    def post(self, request, pk):
        serializer = CancellationActionSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        enquiry = self.perform(pk, request, serializer.validated_data)
        enquiry = Enquiry.objects.select_related(
            "related_booking", "related_booking__guest", "related_booking__room_type",
            "related_payment", "processed_by",
        ).prefetch_related("refunds").get(pk=enquiry.pk)
        return success_response(EnquiryAdminSerializer(enquiry).data, message=self.success_message)


@extend_schema(tags=["Admin · Enquiries"], summary="Mark a cancellation request under review")
class CancellationReviewView(_CancellationActionView):
    success_message = "Cancellation request marked under review."

    def perform(self, pk, request, data):
        try:
            return services.mark_under_review(pk, staff_user=request.user, notes=data.get("notes", ""), request=request)
        except Enquiry.DoesNotExist:
            raise NotFound()


@extend_schema(tags=["Admin · Enquiries"], summary="Approve a cancellation request and cancel its booking")
class CancellationApproveView(_CancellationActionView):
    success_message = "Cancellation approved and booking cancelled. Refund is not complete until provider confirmation."

    def perform(self, pk, request, data):
        try:
            return services.approve_cancellation(
                pk,
                staff_user=request.user,
                notes=data.get("notes", ""),
                resolution=data.get("resolution", ""),
                request=request,
            )
        except Enquiry.DoesNotExist:
            raise NotFound()


@extend_schema(tags=["Admin · Enquiries"], summary="Reject a cancellation request")
class CancellationRejectView(_CancellationActionView):
    success_message = "Cancellation request rejected."

    def perform(self, pk, request, data):
        try:
            return services.reject_cancellation(
                pk,
                staff_user=request.user,
                notes=data.get("notes", ""),
                resolution=data.get("resolution", ""),
                request=request,
            )
        except Enquiry.DoesNotExist:
            raise NotFound()


@extend_schema(tags=["Admin · Enquiries"], summary="Close a cancellation request")
class CancellationCloseView(_CancellationActionView):
    success_message = "Cancellation request closed."

    def perform(self, pk, request, data):
        try:
            return services.close_request(pk, staff_user=request.user, notes=data.get("notes", ""), request=request)
        except Enquiry.DoesNotExist:
            raise NotFound()


@extend_schema(tags=["Admin · Enquiries"], summary="Initiate an approved Paystack refund")
class CancellationProcessRefundView(APIView):
    permission_classes = [IsManagerOrAdmin]
    serializer_class = CancellationActionSerializer

    def post(self, request, pk):
        serializer = CancellationActionSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        from apps.payments.services import payment_service

        try:
            enquiry = Enquiry.objects.get(pk=pk)
        except Enquiry.DoesNotExist:
            raise NotFound()
        refund = payment_service.initiate_cancellation_refund(
            enquiry=enquiry,
            staff_user=request.user,
            customer_note=serializer.validated_data.get("customer_note", ""),
            merchant_note=serializer.validated_data.get("merchant_note", ""),
            request=request,
        )
        enquiry = Enquiry.objects.select_related(
            "related_booking", "related_booking__guest", "related_booking__room_type",
            "related_payment", "processed_by",
        ).prefetch_related("refunds").get(pk=enquiry.pk)
        return success_response(
            {
                "refund": {
                    "id": refund.pk,
                    "status": refund.status,
                    "amount": str(refund.amount),
                    "paystack_refund_reference": refund.paystack_refund_reference,
                    "paystack_transaction_reference": refund.paystack_transaction_reference,
                },
                "cancellation_request": EnquiryAdminSerializer(enquiry).data,
            },
            message="Refund request submitted. Awaiting Paystack confirmation.",
            status=status.HTTP_202_ACCEPTED,
        )
