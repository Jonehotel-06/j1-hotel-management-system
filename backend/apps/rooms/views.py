# apps/rooms/views.py
"""Public room catalog endpoints.

"Rooms" on the public site are bookable ROOM TYPES. A room type is also the
only thing a guest can search availability for; the individual physical rooms
behind it are exposed through one narrow, read-only endpoint
(``/api/rooms/{slug}/rooms/``) so that "Book this room — Room 203" can be
honoured. That endpoint deliberately returns the bare minimum (id, number,
floor and, when dates are supplied, whether the room is free) — housekeeping
state, notes, status flags and anything else operational stay internal.
"""
from datetime import datetime, timedelta

from django.db.models import Prefetch
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from drf_spectacular.types import OpenApiTypes
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle

from apps.bookings.services import availability
from apps.core.responses import success_response
from apps.offers.services import offers_for_room_type

from .models import Room, RoomType, RoomTypeImage
from .serializers import (
    PublicRoomOptionSerializer,
    RoomTypeDetailSerializer,
    RoomTypeListSerializer,
)


def _catalog_queryset():
    # Only active images are ever shown publicly; prefetching the filtered set
    # lets the serializers reuse one cache instead of querying per room type.
    return (
        RoomType.objects.filter(is_active=True)
        .prefetch_related(
            "amenities",
            Prefetch("images", queryset=RoomTypeImage.objects.filter(is_active=True).order_by("display_order", "id")),
        )
        .order_by("display_order", "name")
    )


@extend_schema(tags=["Rooms"], summary="List active room types")
class RoomTypeListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = RoomTypeListSerializer
    pagination_class = None

    def get_queryset(self):
        return _catalog_queryset()


@extend_schema(tags=["Rooms"], summary="Room type detail (images, amenities, applicable offers)")
class RoomTypeDetailView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = RoomTypeDetailSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return _catalog_queryset()

    def get_object(self):
        lookup = self.kwargs.get("slug", "")
        qs = self.get_queryset()
        obj = qs.filter(slug=lookup).first()
        if obj is None and lookup.isdigit():
            obj = qs.filter(pk=int(lookup)).first()
        if obj is None:
            raise NotFound()
        return obj

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        data = self.get_serializer(instance).data
        data["offers"] = offers_for_room_type(instance)
        return success_response(data)


@extend_schema(
    tags=["Rooms"],
    summary="Per-date availability calendar for one room type",
    description=(
        "Every date in the window reports whether the room type has at least "
        "one sellable physical room free that NIGHT. A date some booking "
        "merely touches is NOT unavailable while another physical room of the "
        "same type is still free; a date is unavailable only when every "
        "sellable room is blocked. A checkout day never blocks the next "
        "guest's check-in (half-open [check_in, check_out) nights)."
    ),
    parameters=[
        OpenApiParameter("start_date", OpenApiTypes.DATE, required=False,
                         description="Window start, inclusive. Defaults to today."),
        OpenApiParameter("end_date", OpenApiTypes.DATE, required=False,
                         description="Window end, EXCLUSIVE (check-out semantics). "
                                     "Required together with start_date; the window "
                                     "may span at most 366 days."),
        OpenApiParameter("days", int, required=False,
                         description="Legacy shorthand: window length from today "
                                     "(1–366, default 365). Ignored when both "
                                     "start_date and end_date are supplied."),
    ],
)
class RoomTypeUnavailableDatesView(generics.GenericAPIView):
    """Calendar inventory for one room type, computed by the availability engine.

    The sweep itself lives in ``apps.bookings.services.availability`` — the
    same blocking rules as the range search and booking creation, resolved
    per night. This view only parses the window and shapes the response.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "availability"
    serializer_class = PublicRoomOptionSerializer  # schema marker only

    def _room_type(self, slug):
        qs = _catalog_queryset()
        room_type = qs.filter(slug=slug).first()
        if room_type is None and str(slug).isdigit():
            room_type = qs.filter(pk=int(slug)).first()
        if room_type is None:
            raise NotFound()
        return room_type

    def _window(self, request):
        params = request.query_params
        start_raw = params.get("start_date")
        end_raw = params.get("end_date")
        days_raw = params.get("days")

        if start_raw or end_raw:
            if not (start_raw and end_raw):
                raise ValidationError(
                    {"start_date": ["Provide both start_date and end_date."]}
                )
            try:
                start = datetime.strptime(start_raw, "%Y-%m-%d").date()
                end = datetime.strptime(end_raw, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise ValidationError(
                    {"start_date": ["Dates must use YYYY-MM-DD format."]}
                )
            if end <= start:
                raise ValidationError(
                    {"end_date": ["end_date must be after start_date."]}
                )
            if (end - start).days > availability.MAX_CALENDAR_WINDOW_DAYS:
                raise ValidationError(
                    {"end_date": [
                        f"The window may span at most {availability.MAX_CALENDAR_WINDOW_DAYS} days."
                    ]}
                )
            return start, end

        try:
            days = int(days_raw) if days_raw is not None else 365
        except (TypeError, ValueError):
            raise ValidationError({"days": ["Days must be a whole number."]})
        today = timezone.localdate()
        return today, today + timedelta(days=min(max(days, 1), availability.MAX_CALENDAR_WINDOW_DAYS))

    def get(self, request, slug):
        room_type = self._room_type(slug)
        start, end = self._window(request)
        inventory = availability.nightly_inventory(room_type=room_type, start=start, end=end)
        dates = inventory["dates"]
        unavailable = [day for day, info in dates.items() if not info["available"]]
        return success_response({
            "room_type": {
                "id": room_type.pk,
                "name": room_type.name,
                "slug": room_type.slug,
            },
            "total_rooms": inventory["total_sellable"],
            "from": start.isoformat(),
            "through": (end - timedelta(days=1)).isoformat(),
            "dates": dates,
            "unavailable_dates": unavailable,
        })


@extend_schema(
    tags=["Rooms"],
    summary='Physical rooms of a room type (for book-this-room)',
    parameters=[
        OpenApiParameter("check_in", OpenApiTypes.DATE, required=False,
                         description="Optional. When both dates are given each "
                                     "room reports whether it is free."),
        OpenApiParameter("check_out", OpenApiTypes.DATE, required=False),
    ],
)
class RoomTypeRoomsView(generics.ListAPIView):
    """The physical rooms that make up one room type.

    Only rooms the hotel can actually sell are listed: the type must be active,
    the room must be active and it must not be under maintenance or out of
    service. Availability is answered by the authoritative availability engine,
    never by a client-side hint.
    """

    permission_classes = [AllowAny]
    serializer_class = PublicRoomOptionSerializer
    pagination_class = None

    def get_room_type(self):
        if hasattr(self, "_public_room_type"):
            return self._public_room_type
        lookup = self.kwargs.get("slug", "")
        qs = _catalog_queryset()
        obj = qs.filter(slug=lookup).first()
        if obj is None and str(lookup).isdigit():
            obj = qs.filter(pk=int(lookup)).first()
        if obj is None:
            raise NotFound()
        self._public_room_type = obj
        return obj

    def _window(self):
        # ListAPIView may ask for the queryset and serializer context separately.
        # Parse once so validation and the exact "now" boundary stay consistent.
        if hasattr(self, "_public_room_window"):
            return self._public_room_window
        params = self.request.query_params
        raw_in, raw_out = params.get("check_in"), params.get("check_out")
        if not raw_in and not raw_out:
            self._public_room_window = None
            return None
        if not raw_in or not raw_out:
            raise ValidationError({"check_in": ["Provide both dates to check availability."]})
        try:
            check_in = datetime.strptime(raw_in, "%Y-%m-%d").date()
            check_out = datetime.strptime(raw_out, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ValidationError({"check_in": ["Dates must use YYYY-MM-DD format."]})
        if check_out <= check_in:
            raise ValidationError({"check_out": ["Check-out must be after check-in."]})
        self._public_room_window = (check_in, check_out, timezone.now())
        return self._public_room_window

    def _blocked_room_ids(self):
        """Compute one authoritative blocked-ID set for the whole response."""
        if hasattr(self, "_public_blocked_room_ids"):
            return self._public_blocked_room_ids
        window = self._window()
        if not window:
            self._public_blocked_room_ids = frozenset()
            return self._public_blocked_room_ids
        check_in, check_out, now = window
        self._public_blocked_room_ids = frozenset(
            availability.blocked_room_ids(
                room_type_id=self.get_room_type().pk,
                check_in=check_in,
                check_out=check_out,
                now=now,
            )
        )
        return self._public_blocked_room_ids

    def get_serializer_context(self):
        context = super().get_serializer_context()
        window = self._window()
        context["window"] = window
        if window:
            context["blocked_room_ids"] = self._blocked_room_ids()
        return context

    def get_queryset(self):
        return (
            Room.objects.filter(room_type=self.get_room_type(), is_active=True)
            .exclude(status__in=availability.OPERATIONALLY_BLOCKED)
            .select_related("room_type")
            .order_by("room_number")
        )
