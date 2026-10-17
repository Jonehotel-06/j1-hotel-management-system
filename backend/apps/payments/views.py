# apps/payments/views.py
"""Guest payment endpoints + Paystack webhook receiver."""
import json
import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps.bookings.access import GUEST_TOKEN_HEADER, can_access_booking
from apps.bookings.models import Booking
from apps.bookings.services import booking_service
from apps.core.authentication import LenientJWTAuthentication
from apps.core.responses import success_response
from apps.core.serializers import EmptySerializer

from .services import payment_service
from .serializers import InitializePaymentSerializer

logger = logging.getLogger("apps")


def _client_address(request):
    """Client IP for the refusal log only — never echoed back to the caller."""
    from apps.core.client_ip import resolve_client_ip

    try:
        return resolve_client_ip(request) or "-"
    except Exception:  # pragma: no cover - diagnostics must never raise
        return "-"


@extend_schema(tags=["Payments"], summary="Initialize a Paystack payment for a booking")
class InitializePaymentView(APIView):
    permission_classes = [AllowAny]
    # This view is open to anonymous callers and authorizes with the booking
    # token, so an expired dashboard session must not turn a guest's payment
    # into 401 "this device is not recognised".
    authentication_classes = [LenientJWTAuthentication]
    serializer_class = InitializePaymentSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_init"

    def post(self, request):
        serializer = InitializePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reference = serializer.validated_data["booking_reference"]
        booking = booking_service.get_booking_by_reference_or_id(reference)
        if booking is None:
            # A 404 alone cannot tell the difference between "no such booking"
            # and "the guest's credential was not accepted". Both must stay 404
            # to the caller (references are not secrets to probe with), so the
            # REASON is recorded server-side — this is the line to read when a
            # guest reports "payment fails".
            logger.warning(
                "Payment init refused: no booking matches reference %r "
                "(authenticated=%s, guest_token_present=%s, ip=%s)",
                reference,
                request.user.is_authenticated,
                bool(request.headers.get(GUEST_TOKEN_HEADER)),
                _client_address(request),
            )
            raise NotFound("Booking not found.")
        if not can_access_booking(
            request, booking, staff_required_capabilities=("booking.manage", "payment.capture")
        ):
            logger.warning(
                "Payment init refused: credential rejected for booking %s "
                "(status=%s, authenticated=%s, staff=%s, guest_token_present=%s, ip=%s)",
                booking.booking_reference,
                booking.status,
                request.user.is_authenticated,
                bool(getattr(request.user, "is_staff_member", False)),
                bool(request.headers.get(GUEST_TOKEN_HEADER)),
                _client_address(request),
            )
            # 404 (not 403) so references alone cannot probe other bookings.
            raise NotFound("Booking not found.")
        payload = payment_service.initialize_booking_payment(
            booking=booking, user=request.user if request.user.is_authenticated else None, request=request
        )
        return success_response(payload, message="Payment initialized.",
                                status=status.HTTP_201_CREATED)


@extend_schema(tags=["Payments"], summary="Verify a transaction server-side (idempotent)")
class VerifyPaymentView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [LenientJWTAuthentication]
    serializer_class = InitializePaymentSerializer  # response shape documented in contract
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_verify"

    def _resolve_reference(self, request, reference=None):
        if reference:
            return str(reference).strip()
        data = getattr(request, "data", None) or {}
        if isinstance(data, dict):
            ref = (
                data.get("reference")
                or data.get("trxref")
                or data.get("payment_reference")
                or data.get("booking_reference")
            )
            if ref:
                return str(ref).strip()
        qp = getattr(request, "query_params", None) or getattr(request, "GET", {})
        ref = (
            qp.get("reference")
            or qp.get("trxref")
            or qp.get("payment_reference")
            or qp.get("booking_reference")
        )
        return str(ref or "").strip()

    def _handle_verify(self, request, reference=None):
        resolved_ref = self._resolve_reference(request, reference)
        if not resolved_ref:
            raise NotFound("Payment not found.")
        payment = payment_service.resolve_payment_for_verification(resolved_ref)
        if payment is None or not can_access_booking(
            request, payment.booking, staff_required_capabilities=("booking.manage", "payment.capture")
        ):
            # 404 (not 403) so payment references alone expose nothing.
            raise NotFound("Payment not found.")
        result = payment_service.process_verification(reference=payment.reference, request=request)
        return success_response(result, message="Payment verified.")

    def get(self, request, reference=None):
        return self._handle_verify(request, reference=reference)

    def post(self, request, reference=None):
        return self._handle_verify(request, reference=reference)


@method_decorator(csrf_exempt, name="dispatch")
@extend_schema(tags=["Payments"], summary="Paystack webhook (signature verified)")
class PaystackWebhookView(APIView):
    """Receives charge events. Signature validation happens BEFORE parsing.

    CSRF is exempt because the request is authenticated via the HMAC signature,
    not a browser session.
    """

    serializer_class = EmptySerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    # Public, unauthenticated endpoint: throttle by client IP to blunt
    # DoS/log-flood abuse. Generous in production (300/min) because Paystack
    # legitimately retries/bursts webhook deliveries.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "paystack_webhook"

    def post(self, request):
        signature = request.META.get("HTTP_X_PAYSTACK_SIGNATURE", "")
        if not payment_service.verify_webhook_signature(request.body, signature):
            logger.warning("Rejected webhook with invalid signature from %s",
                           request.META.get("REMOTE_ADDR"))
            return Response(
                {"success": False, "code": "INVALID_SIGNATURE", "message": "Invalid webhook signature."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response(
                {"success": False, "code": "INVALID_PAYLOAD", "message": "Malformed webhook body."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = payment_service.process_webhook(payload, request=request)
        return Response({"success": True, "message": "Webhook processed.", "data": result})
