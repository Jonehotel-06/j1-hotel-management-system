# apps/bookings/views.py
"""Guest-facing booking endpoints: availability, quotes, booking lifecycle."""
import logging
from datetime import datetime

from django.db import IntegrityError
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from drf_spectacular.types import OpenApiTypes
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.exceptions import InvalidDatesError
from apps.core.exceptions import CancellationNotAllowedError
from apps.core.responses import success_response
from apps.hotel.models import HotelSettings

from django.core.cache import cache

from .access import can_access_booking
from .access_links import read_access_link_token
from .models import Booking
from .serializers import (
    AvailabilityQuerySerializer,
    BookingCreateSerializer,
    BookingDetailSerializer,
    BookingListSerializer,
    CancelBookingSerializer,
    GuestAccessExchangeSerializer,
    QuoteRequestSerializer,
    ReceiptSerializer,
)
from .services import booking_service, pricing

logger = logging.getLogger("apps")


def _parse_date(value, field_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise InvalidDatesError(f"Parameter '{field_name}' must be a date in YYYY-MM-DD format.")


@extend_schema(
    tags=["Bookings"],
    summary="Authoritative room availability search",
    parameters=[
        OpenApiParameter("check_in", OpenApiTypes.DATE, required=True),
        OpenApiParameter("check_out", OpenApiTypes.DATE, required=True),
        OpenApiParameter("guests", int, required=False),
        OpenApiParameter("rooms", int, required=False),
        OpenApiParameter("room_type", str, required=False, description="Room type slug or id"),
    ],
)
class AvailabilityView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "availability"
    serializer_class = AvailabilityQuerySerializer  # schema + date validation

    def get(self, request):
        params = request.query_params
        check_in_raw, check_out_raw = params.get("check_in"), params.get("check_out")
        if not check_in_raw or not check_out_raw:
            raise ValidationError(
                {"check_in": ["Required."], "check_out": ["Required."]}
            )
        check_in = _parse_date(check_in_raw, "check_in")
        check_out = _parse_date(check_out_raw, "check_out")
        try:
            guests = max(1, int(params.get("guests", 1)))
            rooms = max(1, int(params.get("rooms", 1)))
        except ValueError:
            raise ValidationError({"guests": ["Must be a number."], "rooms": ["Must be a number."]})

        # Date sanity is a pricing-engine rule, applied here too for clean errors.
        pricing.validate_stay_dates(check_in, check_out)

        results = booking_service.search_availability(
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            rooms=rooms,
            room_type_value=params.get("room_type") or None,
            request=request,   # absolute image URLs for the separately hosted frontend
        )
        return success_response(
            {
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "nights": (check_out - check_in).days,
                "guests": guests,
                "rooms": rooms,
                "results": results,
            },
            message="Availability retrieved." if results else "No room types available.",
        )


@extend_schema(tags=["Bookings"], summary="Authoritative price quote (no booking created)")
class QuoteView(APIView):
    permission_classes = [AllowAny]
    serializer_class = QuoteRequestSerializer

    def post(self, request):
        serializer = QuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        room_type = booking_service.resolve_room_type(data["room_type"])
        settings_obj = HotelSettings.get_settings()
        # A signed-in guest sees their personal discount in the quote exactly as
        # the booking will apply it. Anonymous quotes simply have no guest row,
        # and the discount is still applied authoritatively at creation time.
        quote_guest = None
        if request.user.is_authenticated:
            quote_guest = getattr(request.user, "guest_profile", None)
        quote = pricing.calculate_quote(
            room_type=room_type,
            check_in=data["check_in"],
            check_out=data["check_out"],
            rooms=data["rooms"],
            adults=data["adults"],
            children=data["children"],
            offer_code=data.get("offer_code") or None,
            settings_obj=settings_obj,
            guest=quote_guest,
        )
        payload = {
            "room_type": {"id": room_type.pk, "name": room_type.name, "slug": room_type.slug},
            "check_in": data["check_in"].isoformat(),
            "check_out": data["check_out"].isoformat(),
            **quote.to_api_dict(),
            "policies": {
                "check_in_time": settings_obj.check_in_time.strftime("%H:%M"),
                "check_out_time": settings_obj.check_out_time.strftime("%H:%M"),
                "cancellation_deadline_hours": settings_obj.cancellation_deadline_hours,
                "cancellation_fee_percent": str(settings_obj.cancellation_fee_percent),
            },
            "hold_info": {
                "pending_booking_minutes": settings_obj.pending_booking_minutes,
                "note": "Inventory is held for this many minutes once the booking is created.",
            },
        }
        return success_response(payload, message="Quote calculated.")


@extend_schema(tags=["Bookings"])
class MyBookingsView(generics.ListCreateAPIView):
    """GET: my bookings (paginated). POST: create a pending booking."""

    serializer_class = BookingListSerializer

    def get_permissions(self):
        # Public checkout creates a standalone Guest. Listing is intentionally
        # not a public operation; secure lookup uses the bearer token below.
        return [AllowAny()] if self.request.method == "POST" else [IsAuthenticated()]

    def get_queryset(self):
        qs = (
            Booking.objects.filter(guest__user=self.request.user)
            .select_related("guest", "room_type")
            .order_by("-created_at")
        )
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        return qs

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "booking_create"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    # --- Idempotency (safe retry after a lost response) ----------------------
    # The browser generates ONE key per logical booking submission and reuses
    # it across retries of that same submission. If the first attempt's 201 is
    # lost (client timeout / network drop) the server already created the
    # booking — a retry carrying the same key must return that ORIGINAL
    # booking, never a duplicate reservation. The unique constraint on
    # Booking.idempotency_key makes this race-safe.
    @staticmethod
    def _clean_idempotency_key(request):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            return None
        if len(key) < 8 or len(key) > 64 or not all(c.isalnum() or c in "-_" for c in key):
            raise ValidationError(
                {"idempotency_key": ["Must be 8-64 characters (letters, digits, dash, underscore)."]}
            )
        return key

    def _booking_created_payload(self, request, booking, *, replayed=False):
        payload = BookingDetailSerializer(booking, context={"request": request}).data
        payload["guest_access_token"] = getattr(booking, "guest_access_token", None)
        payload["guest_access_expires_at"] = (
            booking.guest_access_expires_at.isoformat() if booking.guest_access_expires_at else None
        )
        substitution = getattr(booking, "room_substitution", None)
        if substitution:
            # Never switch rooms silently — the guest is told on the
            # confirmation screen (and the receipt shows the assigned room).
            payload["room_substitution"] = substitution
        if replayed:
            payload["idempotent_replay"] = True
        return payload

    def create(self, request, *args, **kwargs):
        idempotency_key = self._clean_idempotency_key(request)

        if idempotency_key:
            existing = (
                Booking.objects.filter(idempotency_key=idempotency_key)
                .select_related("guest", "room_type", "offer")
                .prefetch_related("room_assignments__room")
                .first()
            )
            if existing is not None:
                # Safe retry after a lost/aborted first response: return the
                # ORIGINAL booking. The one-time guest access token was likely
                # lost with the first response, so a fresh token is issued for
                # this response (the digest of the lost token is replaced).
                existing.room_substitution = None
                existing.guest_access_token = existing.issue_guest_access_token()
                return success_response(
                    self._booking_created_payload(request, existing, replayed=True),
                    message="This booking was already created. Continuing with the same reservation.",
                    status=status.HTTP_200_OK,
                )

        serializer = BookingCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            booking = booking_service.create_booking(
                room_type_value=data["room_type"],
                check_in=data["check_in"],
                check_out=data["check_out"],
                rooms=data["rooms"],
                adults=data["adults"],
                children=data["children"],
                offer_code=data.get("offer_code") or None,
                special_requests=data.get("special_requests", ""),
                user=request.user if request.user.is_authenticated else None,
                guest_data=data.get("guest") or None,
                request=request,
                room_id=data.get("room_id"),
                idempotency_key=idempotency_key,
                room_guests=data.get("room_guests") or None,
            )
        except IntegrityError:
            # Another request with the SAME idempotency key committed first
            # (double-click / racing retry) — return its booking, not a second one.
            existing = (
                Booking.objects.filter(idempotency_key=idempotency_key)
                .select_related("guest", "room_type", "offer")
                .prefetch_related("room_assignments__room")
                .first()
            )
            if existing is not None:
                existing.room_substitution = None
                existing.guest_access_token = existing.issue_guest_access_token()
                return success_response(
                    self._booking_created_payload(request, existing, replayed=True),
                    message="This booking was already created. Continuing with the same reservation.",
                    status=status.HTTP_200_OK,
                )
            raise
        return success_response(
            self._booking_created_payload(request, booking),
            message="Booking created. Complete payment to confirm your reservation.",
            status=status.HTTP_201_CREATED,
        )


class _OwnedBookingMixin:
    """Resolve a booking by pk or reference + enforce object ownership."""

    permission_classes = [AllowAny]

    def get_booking(self):
        lookup = self.kwargs["lookup"]
        booking = (
            Booking.objects.filter(
                Q(booking_reference=lookup) | Q(pk=lookup if str(lookup).isdigit() else -1)
            )
            .select_related("guest", "guest__user", "room_type", "offer")
            .prefetch_related("room_assignments__room", "payments")
            .first()
        )
        if booking is None or not can_access_booking(self.request, booking):
            # 404 (not 403) so booking references alone expose nothing.
            raise NotFound("Booking not found.")
        return booking_service.refresh_expired_pending(booking)


@extend_schema(tags=["Bookings"], summary="Booking detail by id or reference")
class BookingDetailView(_OwnedBookingMixin, APIView):
    serializer_class = BookingDetailSerializer

    def get(self, request, lookup):
        booking = self.get_booking()
        return success_response(
            BookingDetailSerializer(booking, context={"request": request}).data
        )


@extend_schema(tags=["Bookings"], summary="Guest self-cancellation is disabled; submit Contact cancellation request")
class BookingCancelView(_OwnedBookingMixin, APIView):
    serializer_class = CancelBookingSerializer

    def post(self, request, lookup):
        # Keep the legacy URL non-destructive for older links/clients while
        # forcing the new Contact-page review workflow for confirmed/paid stays.
        self.get_booking()  # still enforce owner/guest-token 404 privacy
        raise CancellationNotAllowedError(
            "Online self-cancellation is no longer available. Please submit a Cancellation / Refund Request from the Contact page."
        )


@extend_schema(tags=["Bookings"], summary="Receipt for a booking")
class BookingReceiptView(_OwnedBookingMixin, APIView):
    serializer_class = BookingDetailSerializer  # response envelope documented in contract

    def get(self, request, lookup):
        booking = self.get_booking()
        # ReceiptSerializer includes only SUCCESS payments. Before verification
        # this endpoint is an explicitly UNPAID booking folio, never a paid receipt.
        return success_response(ReceiptSerializer().to_representation(booking))

@extend_schema(
    tags=["Bookings"],
    summary="Exchange a signed emailed link for a guest access token",
    request=GuestAccessExchangeSerializer,
)
class BookingAccessExchangeView(APIView):
    """Turn the ``access`` value in an emailed link into a normal guest token.

    The guest-session token is a bearer secret that lives in one browser. The
    emailed link carries a signed, expiring value instead, so a guest can open
    their booking on a phone or after clearing site data and still pay. The
    exchange never widens access: the signature is bound to this booking
    reference and expires, and the response contains only what the booking's
    own guest could already read.
    """

    permission_classes = [AllowAny]
    serializer_class = GuestAccessExchangeSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "booking_access_exchange"

    def post(self, request, lookup):
        serializer = GuestAccessExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = booking_service.get_booking_by_reference_or_id(lookup)
        if booking is None or not read_access_link_token(
            serializer.validated_data["access"], booking.booking_reference
        ):
            # Same 404 as every other booking endpoint: a wrong or expired link
            # must not reveal whether the reference exists.
            logger.warning(
                "Guest access link rejected (reference=%s, booking_found=%s)",
                lookup,
                booking is not None,
            )
            raise NotFound("Booking not found.")
        booking = booking_service.refresh_expired_pending(booking)
        token = booking.issue_guest_access_token()
        logger.info(
            "Guest access link exchanged for a session token (booking=%s)",
            booking.booking_reference,
        )
        return success_response(
            {
                "booking_reference": booking.booking_reference,
                "guest_access_token": token,
                "guest_access_expires_at": (
                    booking.guest_access_expires_at.isoformat()
                    if booking.guest_access_expires_at
                    else None
                ),
            },
            message="Booking access restored.",
        )


@extend_schema(
    tags=["Bookings"],
    summary="Email a fresh secure link for a booking (non-enumerating)",
)
class BookingAccessLinkResendView(APIView):
    """Send the guest a new signed link for this booking.

    Always answers 200 with the same body, whether or not the reference exists,
    so the endpoint cannot be used to discover reservations. A booking-scoped
    cache counter caps how often one reference can trigger an email.
    """

    permission_classes = [AllowAny]
    serializer_class = GuestAccessExchangeSerializer

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "booking_access_resend"

    MAX_PER_HOUR = 3

    def post(self, request, lookup):
        booking = booking_service.get_booking_by_reference_or_id(lookup)
        delivered = False
        if booking is not None and booking.guest.email:
            key = f"booking.access_resend.{booking.pk}"
            sent = cache.get(key, 0) or 0
            if sent < self.MAX_PER_HOUR:
                cache.set(key, sent + 1, 60 * 60)
                self._send_link(booking)
                delivered = True
            else:
                logger.warning(
                    "Access-link resend cap reached for booking %s", booking.booking_reference
                )
        return success_response(
            {"sent": True},
            message=(
                "If that booking exists, a secure link is on its way to the "
                "email address on the reservation."
            ),
        )

    @staticmethod
    def _send_link(booking):
        from apps.core.email_design import render_notice_email
        from apps.core.emails import queue_email
        from apps.hotel.models import HotelSettings

        hotel = HotelSettings.get_settings()
        text_body, html_body = render_notice_email(
            category="Booking access",
            title="Your secure booking link",
            greeting=f"Hello {booking.guest.first_name},",
            paragraphs=[
                "Use the link below to open your booking. It works on this device "
                "or any other, and expires in a few days for your security.",
            ],
            details=[
                {"label": "Booking reference", "value": booking.booking_reference},
                {"label": "Check-in", "value": str(booking.check_in)},
            ],
            cta_label="Open My Booking",
            cta_url=booking_service.guest_booking_link(booking),
            footnote="If you did not ask for this link, you can ignore this email.",
            preheader=f"Secure access to booking {booking.booking_reference}.",
        )
        queue_email(
            kind="BOOKING_ACCESS_LINK",
            booking_reference=booking.booking_reference,
            booking_id=booking.pk,
            subject=f"Your secure booking link — {booking.booking_reference}",
            message=text_body,
            html_message=html_body,
            recipients=[booking.guest.email],
        )

