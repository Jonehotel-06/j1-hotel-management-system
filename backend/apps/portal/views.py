"""Verified-email portal authentication and a deliberately narrow overview."""
from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.bookings.models import Booking
from apps.core.responses import success_response
from apps.stays.models import StayRoom
from apps.core.serializers import EmptySerializer

from .authentication import PortalSessionAuthentication
from .permissions import HasPortalSession
from .serializers import PortalAccessConsumeSerializer, PortalAccessRequestSerializer
from .services import consume_access_challenge, request_access_challenge, revoke_session


@extend_schema(tags=["Guest Portal"], summary="Request a non-enumerating email sign-in link")
class PortalAccessRequestView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PortalAccessRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "portal_access_request"

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_access_challenge(email=serializer.validated_data["email"], request=request)
        # Never reveal whether the email has a guest record, whether delivery
        # was throttled, or whether an email provider accepted the message.
        return success_response(
            message="If this email can access the guest portal, a secure sign-in link has been sent.",
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema(tags=["Guest Portal"], summary="Consume a one-use email link and create a short-lived portal session")
class PortalAccessConsumeView(APIView):
    permission_classes = [AllowAny]
    serializer_class = PortalAccessConsumeSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "portal_access_consume"

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        session, raw_session_token = consume_access_challenge(
            raw_token=serializer.validated_data["token"], request=request
        )
        return success_response(
            {
                "session_token": raw_session_token,
                "expires_at": session.expires_at.isoformat(),
                "transport": "memory_bearer",
            },
            message="Guest portal session created.",
        )


@extend_schema(tags=["Guest Portal"], summary="Revoke the active guest portal session")
class PortalLogoutView(APIView):
    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]
    serializer_class = EmptySerializer

    def post(self, request):
        revoke_session(request.portal_session, request=request)
        return success_response(message="Guest portal session ended.")


def _in_house_stay_summary(booking):
    """Expose only the verified guest's current stay handle and active rooms.

    The portal request command requires a stay reference (and optionally an
    actively assigned room ID). Supplying this scoped projection lets the UI
    use authoritative selections rather than asking a guest to type opaque
    operational identifiers. A non-current stay is deliberately omitted.
    """
    try:
        stay = booking.stay
    except AttributeError:  # absent reverse one-to-one is an AttributeError subclass
        return None
    if stay.status != stay.Status.IN_HOUSE:
        return None
    return {
        "reference": stay.reference,
        "rooms": [
            {"id": stay_room.room_id, "room_number": stay_room.room.room_number}
            for stay_room in stay.stay_rooms.all()
            if stay_room.released_at is None
        ],
    }


@extend_schema(tags=["Guest Portal"], summary="Read reservations belonging to the verified portal email")
class PortalOverviewView(APIView):
    """Bounded booking overview; owned folios are exposed under ``/portal/folios/``."""

    authentication_classes = [PortalSessionAuthentication]
    permission_classes = [HasPortalSession]
    serializer_class = EmptySerializer

    def get(self, request):
        # Fetch one extra row to surface a bounded, honest continuation signal;
        # portal home never downloads an unbounded booking history.
        rows = list(
            Booking.objects.filter(guest__email__iexact=request.portal_session.email)
            .select_related("guest", "room_type", "stay")
            .prefetch_related(
                "room_assignments__room",
                Prefetch("stay__stay_rooms", queryset=StayRoom.objects.filter(released_at__isnull=True).select_related("room")),
            )
            .order_by("-created_at")[:21]
        )
        has_more = len(rows) > 20
        rows = rows[:20]
        return success_response({
            "email": request.portal_session.email,
            "bookings": [
                {
                    "booking_reference": booking.booking_reference,
                    "status": booking.status,
                    "payment_status": booking.payment_status,
                    "check_in": booking.check_in.isoformat(),
                    "check_out": booking.check_out.isoformat(),
                    "room_type_name": booking.room_type.name,
                    "room_numbers": [assignment.room.room_number for assignment in booking.room_assignments.all()],
                    "in_house_stay": _in_house_stay_summary(booking),
                    "currency": booking.currency,
                    "total_amount": str(booking.total_amount),
                    "amount_paid": str(booking.amount_paid),
                }
                for booking in rows
            ],
            "has_more": has_more,
        })
