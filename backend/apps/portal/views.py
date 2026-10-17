"""Verified-email portal authentication and a deliberately narrow overview."""
from django.db.models import Prefetch, Q
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


def _rooms_for_portal_email(booking, email):
    """The rooms this signed-in address may act for inside one booking.

    The booking contact is not restricted. A room guest (``RoomOccupant``) is
    restricted to the room(s) their address is recorded on — otherwise a guest
    staying in Room 3 could raise a request against Room 1, which belongs to
    somebody else's party.
    """
    assignments = list(booking.room_assignments.all())
    contact_email = str(getattr(booking.guest, "email", "") or "").strip().lower()
    if email == contact_email:
        return assignments
    return [
        assignment
        for assignment in assignments
        if any(
            str(occupant.email or "").strip().lower() == email
            for occupant in assignment.occupants.all()
        )
    ]


def _in_house_stay_summary(booking, *, portal_email):
    """Expose only the verified guest's current stay handle and their rooms.

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
    allowed_room_ids = {assignment.room_id for assignment in _rooms_for_portal_email(booking, portal_email)}
    return {
        "reference": stay.reference,
        "rooms": [
            {"id": stay_room.room_id, "room_number": stay_room.room.room_number}
            for stay_room in stay.stay_rooms.all()
            if stay_room.released_at is None and stay_room.room_id in allowed_room_ids
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
        #
        # A booking belongs to the portal identity either because it IS their
        # reservation (booking contact) or because they are recorded as the
        # guest of one of its rooms. Both are verified identities: the address
        # received a one-time link, so no unverified third party can see a
        # booking by guessing an email.
        portal_email = str(request.portal_session.email or "").strip().lower()
        rows = list(
            Booking.objects.filter(
                Q(guest__email__iexact=portal_email)
                | Q(room_assignments__occupants__email__iexact=portal_email)
            )
            .select_related("guest", "room_type", "stay")
            .prefetch_related(
                "room_assignments__room",
                "room_assignments__occupants",
                Prefetch("stay__stay_rooms", queryset=StayRoom.objects.filter(released_at__isnull=True).select_related("room")),
            )
            .distinct()
            .order_by("-created_at")[:21]
        )
        has_more = len(rows) > 20
        rows = rows[:20]
        payload = []
        for booking in rows:
            my_rooms = _rooms_for_portal_email(booking, portal_email)
            is_contact = str(booking.guest.email or "").strip().lower() == portal_email
            payload.append({
                "booking_reference": booking.booking_reference,
                "status": booking.status,
                "payment_status": booking.payment_status,
                "check_in": booking.check_in.isoformat(),
                "check_out": booking.check_out.isoformat(),
                "room_type_name": booking.room_type.name,
                # Keep the full room list for context, and state which of
                # them this address may raise a request for.
                "room_numbers": [assignment.room.room_number for assignment in booking.room_assignments.all()],
                "your_room_numbers": [assignment.room.room_number for assignment in my_rooms],
                "is_booking_contact": is_contact,
                "in_house_stay": _in_house_stay_summary(booking, portal_email=portal_email),
                "currency": booking.currency,
                "total_amount": str(booking.total_amount),
                "amount_paid": str(booking.amount_paid),
            })
        return success_response({
            "email": request.portal_session.email,
            "bookings": payload,
            "has_more": has_more,
        })
