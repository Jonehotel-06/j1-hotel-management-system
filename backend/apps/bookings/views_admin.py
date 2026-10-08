# apps/bookings/views_admin.py
"""Staff booking & guest management endpoints (/api/admin/...)."""
import logging
from datetime import datetime

from django.db.models import Count, Max, Prefetch, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManagerOrAdmin, IsStaffRole
from apps.core.responses import success_response
from apps.core.serializers import EmptySerializer
from apps.core.emails import send_email_safe
from apps.rooms.models import Room

from .models import Booking, BookingRoom, Guest
from .serializers_admin import (
    AdminBookingCreateSerializer,
    AdminBookingDetailSerializer,
    AdminBookingListSerializer,
    AdminBookingModifySerializer,
    AdminGuestDetailSerializer,
    AdminGuestListSerializer,
    AdminGuestUpdateSerializer,
    AssignRoomSerializer,
    LateArrivalBookingSerializer,
    MissedBookingSerializer,
    RecordActionSerializer,
    RescheduleBookingSerializer,
)
from .serializers import ReceiptSerializer
from .services import booking_service
from .services.receipt_email import render_receipt_email

logger = logging.getLogger("apps")


def _admin_booking_queryset():
    return (
        Booking.objects.select_related("guest", "room_type", "offer")
        .prefetch_related(Prefetch("room_assignments", queryset=BookingRoom.objects.select_related("room"), to_attr="_assignments"))
        .order_by("-created_at")
    )


def apply_booking_search(queryset, search):
    """Operational "find it from any scrap of information" search.

    Staff should not need to know which column holds the value, so one term is
    matched against every identifier a receptionist would realistically type:
    booking reference, guest name (single token OR full name across first +
    last), email, phone, physical room number, room type, and the references of
    any payment/receipt recorded against the booking.

    Matching is case-insensitive and partial. Multi-word input is ANDed across
    the guest's name parts so "john doe" finds John Doe without also matching
    every guest whose surname is Doe.
    """
    if not search:
        return queryset
    term = str(search).strip()
    if not term:
        return queryset

    base = (
        Q(booking_reference__icontains=term)
        | Q(guest__first_name__icontains=term)
        | Q(guest__last_name__icontains=term)
        | Q(guest__email__icontains=term)
        | Q(guest__phone__icontains=term)
        | Q(room_type__name__icontains=term)
        | Q(room_type__slug__icontains=term)
        | Q(room_assignments__room__room_number__icontains=term)
        | Q(payments__reference__icontains=term)
        | Q(payments__transaction_id__icontains=term)
    )
    tokens = [t for t in term.split() if t]
    if len(tokens) > 1:
        # "amina yusuf" → every token must appear somewhere in the guest's
        # name. ORed with the whole-string match so a free-text value such as
        # an address is never lost when the term happens to contain a space.
        name_q = Q()
        for token in tokens:
            name_q &= (Q(guest__first_name__icontains=token) | Q(guest__last_name__icontains=token))
        queryset = queryset.filter(base | name_q)
    else:
        queryset = queryset.filter(base)

    # The payment/assignment joins can match a booking more than once.
    return queryset.distinct()


@extend_schema(tags=["Admin · Bookings"])
class AdminBookingListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsStaffRole]

    def get_serializer_class(self):
        return AdminBookingCreateSerializer if self.request.method == "POST" else AdminBookingListSerializer

    def get_queryset(self):
        # Fallback sweep (throttled to once a minute via the cache): keeps the
        # staff console truthful about expired unpaid holds even when the
        # Celery beat/worker pair is not running. Celery remains the primary
        # expiration driver; this is a cheap no-op between intervals.
        booking_service.maybe_expire_stale_pending_bookings()
        qs = _admin_booking_queryset()
        params = self.request.query_params
        if status_param := params.get("status"):
            # Accept comma-separated statuses for operational screens (for
            # example CONFIRMED,CHECKED_IN) without loading all bookings.
            statuses = [value.strip().upper() for value in status_param.split(",") if value.strip()]
            qs = qs.filter(status__in=statuses) if len(statuses) > 1 else qs.filter(status=statuses[0])
        if payment_status := params.get("payment_status"):
            qs = qs.filter(payment_status=payment_status.upper())
        if source := params.get("source"):
            qs = qs.filter(source=source.upper())
        if room_type := params.get("room_type"):
            qs = qs.filter(Q(room_type__slug=room_type) | Q(room_type__pk=room_type if str(room_type).isdigit() else -1))
        if date_from := params.get("date_from"):
            qs = qs.filter(check_in__gte=date_from)
        if date_to := params.get("date_to"):
            qs = qs.filter(check_in__lte=date_to)
        if check_in_on := params.get("check_in"):
            qs = qs.filter(check_in=check_in_on)
        if check_out_on := params.get("check_out"):
            qs = qs.filter(check_out=check_out_on)
        qs = apply_booking_search(qs, params.get("search"))
        ordering = params.get("ordering", "-created_at")
        allowed = {"created_at", "-created_at", "check_in", "-check_in", "check_out", "-check_out", "total_amount", "-total_amount"}
        if ordering in allowed:
            qs = qs.order_by(ordering)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = booking_service.create_booking(
            room_type_value=data["room_type"],
            check_in=data["check_in"],
            check_out=data["check_out"],
            rooms=data["rooms"],
            adults=data["adults"],
            children=data["children"],
            offer_code=data.get("offer_code") or None,
            special_requests=data.get("special_requests", ""),
            user=None,
            guest_data=data["guest"],
            source=data["source"],
            require_payment=data["status"] == Booking.Status.PENDING,
            actor=request.user,
            request=request,
            room_id=data.get("room_id"),
        )
        if data.get("internal_notes"):
            booking.internal_notes = data["internal_notes"]
            booking.save(update_fields=["internal_notes", "updated_at"])
        logger.info("Staff %s created manual booking %s", request.user.id, booking.booking_reference)
        payload = AdminBookingDetailSerializer(booking, context={"request": request}).data
        substitution = getattr(booking, "room_substitution", None)
        if substitution:
            payload["room_substitution"] = substitution
        return success_response(
            payload,
            message="Booking created.",
            status=status.HTTP_201_CREATED,
        )


def _get_admin_booking(lookup):
    booking = _admin_booking_queryset().filter(
        Q(booking_reference=lookup) | Q(pk=lookup if str(lookup).isdigit() else -1)
    ).first()
    if booking is None:
        raise NotFound()
    return booking


@extend_schema(tags=["Admin · Bookings"])
class AdminBookingDetailView(APIView):
    permission_classes = [IsStaffRole]
    serializer_class = AdminBookingDetailSerializer  # for schema introspection

    def get(self, request, lookup):
        booking = _get_admin_booking(lookup)
        return success_response(AdminBookingDetailSerializer(booking, context={"request": request}).data)

    def patch(self, request, lookup):
        booking = _get_admin_booking(lookup)
        serializer = AdminBookingModifySerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        booking = booking_service.modify_booking(
            booking, staff_user=request.user, data=serializer.validated_data, request=request
        )
        booking = _get_admin_booking(booking.booking_reference)
        return success_response(
            AdminBookingDetailSerializer(booking, context={"request": request}).data,
            message="Booking updated.",
        )


class _BookingActionView(APIView):
    permission_classes = [IsStaffRole]
    # NOTE: do NOT define an `action` attribute on this class — spectacular
    # introspection reads `view.action` (ViewSet concept) and crashes when it
    # is a non-string leftover like `action = None`.
    serializer_class = RecordActionSerializer

    def post(self, request, lookup):
        booking = _get_admin_booking(lookup)
        serializer = RecordActionSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        return self.perform(booking, request, serializer.validated_data)


@extend_schema(tags=["Admin · Bookings"], summary="Confirm a pending booking (pay at hotel)")
class AdminBookingConfirmView(_BookingActionView):
    def perform(self, booking, request, data):
        booking = booking_service.confirm_manual_booking(booking, staff_user=request.user, request=request)
        return success_response(message="Booking confirmed.",
                                data={"booking_reference": booking.booking_reference, "status": booking.status})


@extend_schema(tags=["Admin · Bookings"], summary="Staff-cancel a booking")
class AdminBookingCancelView(_BookingActionView):
    def perform(self, booking, request, data):
        booking = booking_service.cancel_booking(
            booking, reason=data.get("reason", ""), by_user=request.user, staff=True, request=request
        )
        return success_response(message="Booking cancelled.",
                                data={"booking_reference": booking.booking_reference, "status": booking.status})


@extend_schema(tags=["Admin · Bookings"], summary="Check a guest in")
class AdminBookingCheckInView(_BookingActionView):
    def perform(self, booking, request, data):
        booking = booking_service.check_in_booking(booking, staff_user=request.user, request=request)
        return success_response(
            {
                "booking_reference": booking.booking_reference,
                "status": booking.status,
                "checked_in_at": booking.checked_in_at,
            },
            message="Guest checked in.",
        )


@extend_schema(tags=["Admin · Bookings"], summary="Check a guest out")
class AdminBookingCheckOutView(_BookingActionView):
    def perform(self, booking, request, data):
        booking = booking_service.check_out_booking(
            booking,
            staff_user=request.user,
            allow_balance_due=data.get("allow_balance_due", False),
            request=request,
        )
        return success_response(
            {
                "booking_reference": booking.booking_reference,
                "status": booking.status,
                "checked_out_at": booking.checked_out_at,
            },
            message="Guest checked out.",
        )


@extend_schema(tags=["Admin · Bookings"], summary="Mark a booking as no-show")
class AdminBookingNoShowView(_BookingActionView):
    def perform(self, booking, request, data):
        booking = booking_service.mark_no_show(booking, staff_user=request.user, request=request)
        return success_response(message="Booking marked as no-show.",
                                data={"booking_reference": booking.booking_reference, "status": booking.status})


@extend_schema(tags=["Admin · Bookings"], summary="Assign/change a physical room")
class AdminBookingAssignRoomView(_BookingActionView):
    def perform(self, booking, request, data):
        serializer = AssignRoomSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        assignment = booking.room_assignments.select_related("room").filter(pk=payload["assignment_id"]).first()
        if assignment is None:
            raise NotFound("Room assignment not found on this booking.")
        room = Room.objects.filter(pk=payload["room_id"]).first()
        if room is None:
            raise NotFound("Room not found.")
        assignment = booking_service.assign_room(
            assignment, new_room=room, staff_user=request.user, request=request
        )
        return success_response(
            {"assignment_id": assignment.pk, "room_number": assignment.room.room_number},
            message="Room assigned.",
        )


# ---------------------------------------------------------------------------
# Guests
# ---------------------------------------------------------------------------
@extend_schema(tags=["Admin · Guests"])
class AdminGuestListView(generics.ListAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = AdminGuestListSerializer

    def get_queryset(self):
        # Booking count excludes cancelled and expired bookings: those never
        # became a stay, so counting them would overstate a guest's history.
        # Annotated in ONE query — no per-row counting in the serializer.
        qs = Guest.objects.annotate(
            bookings_count=Count(
                "bookings",
                filter=~Q(bookings__status__in=booking_service.UNCOUNTED_BOOKING_STATUSES),
                distinct=True,
            ),
            last_booking_at=Max("bookings__created_at"),
        ).order_by("-last_booking_at", "-created_at")
        params = self.request.query_params
        if search := params.get("search"):
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone__icontains=search)
            )
        return qs


@extend_schema(tags=["Admin · Guests"])
class AdminGuestDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsStaffRole]
    http_method_names = ["get", "patch", "head", "options"]

    def get_serializer_class(self):
        return AdminGuestUpdateSerializer if self.request.method == "PATCH" else AdminGuestDetailSerializer

    def get_queryset(self):
        return (
            Guest.objects.prefetch_related(
                Prefetch(
                    "bookings",
                    queryset=Booking.objects.select_related("room_type").prefetch_related(
                        Prefetch(
                            "room_assignments",
                            queryset=BookingRoom.objects.select_related("room"),
                        )
                    ),
                )
            )
        )

    def retrieve(self, request, *args, **kwargs):
        return success_response(self.get_serializer(self.get_object(), context={"request": request}).data)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        from apps.audit.services import log_action

        before = {f: str(getattr(instance, f)) for f in serializer.validated_data}
        instance = serializer.save()
        changes = {
            f: [before[f], str(getattr(instance, f))]
            for f in before
            if before[f] != str(getattr(instance, f))
        }
        if changes:
            log_action(actor=request.user, action="GUEST_UPDATED", instance=instance,
                       changes=changes, request=request)
        return success_response(
            AdminGuestDetailSerializer(instance, context={"request": request}).data,
            message="Guest updated.",
        )

class AdminBookingSendReceiptView(APIView):
    """Send the confirmed payment receipt (with PDF) to the guest — NOW.

    Delivery is SYNCHRONOUS: the provider request (Brevo HTTPS API in
    production) happens during this HTTP request, and the response reports the
    REAL outcome. "Sent" is only returned after the provider accepted the
    message; anything else is an honest failure with a safe reason.
    """
    permission_classes = [IsStaffRole]
    serializer_class = EmptySerializer

    def post(self, request, lookup):
        from rest_framework.exceptions import ValidationError
        from apps.core.validators import validate_email_address
        from apps.notifications.models import EmailLog

        booking = _get_admin_booking(lookup)
        recipient = (booking.guest.email or "").strip()
        # Server-side validation: never trust a frontend-supplied recipient;
        # resolve it from the trusted booking record and reject if unusable.
        if not recipient or not validate_email_address(recipient):
            raise ValidationError({"email": ["This guest has no valid email address on file."]})

        # Idempotency: block a duplicate while one is already in flight for
        # this booking. A previous FAILED/SENT does not block an intentional
        # resend (staff explicitly clicked again). Sends interrupted by a
        # deploy/timeout leave PENDING/SENDING rows behind with no worker to
        # resolve them — mark stale ones FAILED first so they can never lock
        # this booking's receipt button forever.
        EmailLog.resolve_stale(
            EmailLog.objects.filter(booking_reference=booking.booking_reference)
        )
        in_flight = EmailLog.objects.filter(
            booking_reference=booking.booking_reference,
            kind=EmailLog.Kind.RECEIPT,
            status__in=[EmailLog.Status.PENDING, EmailLog.Status.SENDING],
        ).exists()
        if in_flight:
            return success_response(
                {
                    "booking_reference": booking.booking_reference,
                    "recipient": recipient,
                    "status": "IN_PROGRESS",
                },
                message="A receipt email for this booking is already being processed.",
            )

        receipt = ReceiptSerializer().to_representation(booking)
        latest_ref = receipt.get("receipt_reference") or receipt["booking_reference"]
        # Presentation lives in the reusable email builder + Django templates:
        # a professional subject, a plain-text fallback, and a styled HTML body
        # (all dynamic values auto-escaped). The itemised PDF is still attached
        # by the delivery service via ``attach_receipt_pdf=True``.
        #
        # Rendering happens BEFORE anything is sent, so a rendering failure
        # (bad data, a template bug, a cross-platform date bug, …) must be
        # recorded as a tracked FAILED receipt and reported truthfully — it must
        # never surface as an untracked 500 and must never be reported as SENT.
        try:
            subject, text_body, html_body = render_receipt_email(receipt)
        except Exception as exc:  # noqa: BLE001 - recorded + surfaced below
            logger.exception(
                "Receipt render failed for booking %s (%s)",
                booking.booking_reference, exc.__class__.__name__,
            )
            failed_log = EmailLog.objects.create(
                to_email=recipient,
                subject=f"Payment Receipt — {booking.booking_reference}",
                kind=EmailLog.Kind.RECEIPT,
                booking_reference=booking.booking_reference,
                payment_reference=latest_ref,
                booking_id=booking.id,
                attach_receipt_pdf=True,
                created_by=request.user if getattr(request.user, "pk", None) else None,
                status=EmailLog.Status.FAILED,
                failure_stage=EmailLog.FailureStage.RENDER,
                error_class=exc.__class__.__name__,
                error_message="The receipt could not be generated. No email was sent.",
                failed_at=timezone.now(),
            )
            return Response(
                {
                    "success": False,
                    "code": "RECEIPT_RENDER_FAILED",
                    "message": "The receipt could not be generated, so no email was sent.",
                    "data": {
                        "booking_reference": booking.booking_reference,
                        "recipient": recipient,
                        "email_log_id": failed_log.pk,
                        "status": EmailLog.Status.FAILED,
                        "failure_stage": EmailLog.FailureStage.RENDER,
                    },
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        log = send_email_safe(
            subject,
            text_body,
            [recipient],
            html_message=html_body,
            kind=EmailLog.Kind.RECEIPT,
            booking_reference=booking.booking_reference,
            payment_reference=latest_ref,
            booking_id=booking.id,
            attach_receipt_pdf=True,
            created_by=request.user,
        )

        from apps.audit.services import log_action
        log_action(actor=request.user, action="RECEIPT_EMAIL_SENT", instance=booking,
                   changes={"recipient": ["", recipient]}, request=request)

        # Delivery already happened synchronously inside send_email_safe —
        # surface the REAL provider outcome, never a blanket "queued".
        fresh = EmailLog.objects.filter(pk=log.pk).first() if log else None
        current = fresh.status if fresh else EmailLog.Status.FAILED
        payload = {
            "booking_reference": booking.booking_reference,
            "recipient": recipient,
            "email_log_id": log.pk if log else None,
            "status": current,
        }
        if current == EmailLog.Status.SENT:
            if fresh and fresh.provider_message_id:
                payload["provider_message_id"] = fresh.provider_message_id
            return Response(
                {"success": True,
                 "message": f"Receipt email sent successfully to {recipient}.",
                 "data": payload},
                status=status.HTTP_200_OK,
            )
        payload["error"] = (fresh.error_message if fresh else "") or \
            "Email could not be sent. The email provider rejected the request."
        return Response(
            {"success": False, "code": "EMAIL_DELIVERY_FAILED",
             "message": payload["error"], "data": payload},
            status=status.HTTP_502_BAD_GATEWAY,
        )


@extend_schema(tags=["Admin · Bookings"], summary="Reschedule a booking to new dates")
class AdminBookingRescheduleView(_BookingActionView):
    """Move an existing booking (typically a no-show) to new dates.

    Managers/administrators only: rescheduling re-prices the stay and can move
    money into a balance-due position, so it is not a receptionist action.
    """

    permission_classes = [IsManagerOrAdmin]
    serializer_class = RescheduleBookingSerializer

    def perform(self, booking, request, data):
        serializer = RescheduleBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        booking = booking_service.reschedule_booking(
            booking,
            check_in=payload["check_in"],
            check_out=payload["check_out"],
            staff_user=request.user,
            request=request,
        )
        result = getattr(booking, "reschedule_result", {})
        return success_response(
            {
                "booking": AdminBookingDetailSerializer(booking, context={"request": request}).data,
                "reschedule": result,
            },
            message=(
                "Booking rescheduled. A balance is now due."
                if result.get("balance_due") not in (None, "", "0.00")
                else "Booking rescheduled."
            ),
        )


@extend_schema(tags=["Admin · Bookings"], summary="Missed / no-show bookings")
class AdminMissedBookingListView(generics.ListAPIView):
    """Guests who booked but never checked in.

    The state is derived by the backend from booking status + arrival deadline
    (see booking_service.missed_bookings_queryset) — never from a stored flag a
    client could set.
    """

    permission_classes = [IsStaffRole]
    serializer_class = MissedBookingSerializer

    def get_queryset(self):
        booking_service.maybe_expire_stale_pending_bookings()
        qs = booking_service.missed_bookings_queryset()
        params = self.request.query_params
        if status_param := params.get("status"):
            qs = qs.filter(status=status_param.upper())
        if date_from := params.get("date_from"):
            qs = qs.filter(check_in__gte=date_from)
        if date_to := params.get("date_to"):
            qs = qs.filter(check_in__lte=date_to)
        return apply_booking_search(qs, params.get("search"))


class AdminLateArrivalBookingListView(generics.ListAPIView):
    """Confirmed guests who missed their first night but may still check in.

    Derived by the backend (booking_service.late_arrival_bookings_queryset):
    the check-in date has passed, the check-out date has not, the booking was
    never checked in and it is longer than one night.
    """

    permission_classes = [IsStaffRole]
    serializer_class = LateArrivalBookingSerializer

    def get_queryset(self):
        booking_service.maybe_expire_stale_pending_bookings()
        qs = booking_service.late_arrival_bookings_queryset()
        params = self.request.query_params
        if date_from := params.get("date_from"):
            qs = qs.filter(check_in__gte=date_from)
        if date_to := params.get("date_to"):
            qs = qs.filter(check_in__lte=date_to)
        return apply_booking_search(qs, params.get("search"))


@extend_schema(tags=["Admin · Bookings"], summary="Monthly occupancy calendar")
class AdminOccupancyCalendarView(APIView):
    """Room-by-date occupancy for one month, built from real room assignments.

    Query: ?year=2026&month=9 (defaults to the current hotel month).
    """

    permission_classes = [IsStaffRole]
    serializer_class = EmptySerializer

    def get(self, request):
        from apps.core.utils import hotel_today
        from rest_framework.exceptions import ValidationError

        today = hotel_today()
        try:
            year = int(request.query_params.get("year") or today.year)
            month = int(request.query_params.get("month") or today.month)
        except (TypeError, ValueError):
            raise ValidationError({"month": ["Provide a numeric year and month."]})
        if not 1 <= month <= 12:
            raise ValidationError({"month": ["Month must be between 1 and 12."]})
        if not 2000 <= year <= 2100:
            raise ValidationError({"year": ["Year is out of range."]})

        data = booking_service.occupancy_calendar(year=year, month=month)
        return success_response(data, message="Occupancy calendar retrieved.")
