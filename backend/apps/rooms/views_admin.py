# apps/rooms/views_admin.py
"""Staff management for amenities, room types, images and physical rooms."""
import logging
from datetime import timedelta

from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.audit.services import log_action
from apps.core.permissions import IsStaffReadOnlyManagerWrite
from apps.core.responses import success_response

from .models import Amenity, Room, RoomImage, RoomType, RoomTypeImage
from .serializers import AmenitySerializer, RoomSerializer, RoomTypeAdminSerializer, RoomTypeImageSerializer, RoomImageSerializer

logger = logging.getLogger("apps")


class _AuditedModelViewSet(viewsets.ModelViewSet):
    permission_classes = [IsStaffReadOnlyManagerWrite]

    def perform_create(self, serializer):
        instance = serializer.save()
        log_action(actor=self.request.user, action=f"{instance._meta.model_name.upper()}_CREATED",
                   instance=instance, request=self.request)

    def perform_update(self, serializer):
        # Record before/after for the fields that actually changed.
        before = {f.name: getattr(serializer.instance, f.name) for f in serializer.instance._meta.fields}
        instance = serializer.save()
        changes = {
            name: [str(before.get(name)), str(getattr(instance, name))]
            for name in before
            if str(before.get(name)) != str(getattr(instance, name))
        }
        log_action(actor=self.request.user, action=f"{instance._meta.model_name.upper()}_UPDATED",
                   instance=instance, changes=changes, request=self.request)


@extend_schema(tags=["Admin · Amenities"])
class AmenityAdminViewSet(_AuditedModelViewSet):
    serializer_class = AmenitySerializer
    pagination_class = None

    def get_queryset(self):
        return Amenity.objects.all().order_by("name")


@extend_schema(tags=["Admin · Room Types"])
class RoomTypeAdminViewSet(_AuditedModelViewSet):
    serializer_class = RoomTypeAdminSerializer
    # JSON keeps working for plain field edits; multipart carries the cover image.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return (
            RoomType.objects.all()
            .annotate(room_count=Count("rooms", filter=Q(rooms__is_active=True)))
            .prefetch_related(
                "amenities",
                Prefetch("images", queryset=RoomTypeImage.objects.filter(is_active=True)),
            )
            .order_by("display_order", "name")
        )

    def perform_destroy(self, instance):
        # Soft-deactivate instead of deleting so historical bookings stay readable.
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        log_action(actor=self.request.user, action="ROOMTYPE_DEACTIVATED", instance=instance,
                   request=self.request)


@extend_schema(tags=["Admin · Room Types"])
class RoomTypeImageUploadView(generics.CreateAPIView):
    """POST multipart: image, optional alt_text/caption/display_order/is_primary."""

    serializer_class = RoomTypeImageSerializer
    permission_classes = [IsStaffReadOnlyManagerWrite]
    parser_classes = [MultiPartParser, FormParser]

    def create(self, request, room_type_id=None, *args, **kwargs):
        from rest_framework.exceptions import NotFound

        room_type = RoomType.objects.filter(pk=room_type_id).first()
        if room_type is None:
            raise NotFound()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = serializer.save(room_type=room_type)
        log_action(actor=request.user, action="ROOMTYPE_IMAGE_ADDED", instance=room_type,
                   metadata={"image_id": image.pk}, request=request)
        return success_response(self.get_serializer(image).data, message="Image uploaded.",
                                status=status.HTTP_201_CREATED)


@extend_schema(tags=["Admin · Room Types"])
class RoomTypeImageDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RoomTypeImageSerializer
    permission_classes = [IsStaffReadOnlyManagerWrite]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        return RoomTypeImage.objects.select_related("room_type")

    def perform_destroy(self, instance):
        room_type = instance.room_type
        image_id = instance.pk
        instance.delete()
        log_action(actor=self.request.user, action="ROOMTYPE_IMAGE_REMOVED", instance=room_type,
                   metadata={"image_id": image_id}, request=self.request)


@extend_schema(tags=["Admin · Rooms"])
class RoomAdminViewSet(_AuditedModelViewSet):
    serializer_class = RoomSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        from apps.bookings.models import Booking, BookingRoom
        from apps.bookings.services import availability

        now = timezone.now()
        today = timezone.localdate(now)
        tomorrow = today + timedelta(days=1)
        tonight = (
            BookingRoom.objects.filter(room=OuterRef("pk"))
            .filter(availability.blocking_booking_q(now=now, prefix="booking"))
            .filter(availability.overlap_q(today, tomorrow))
        )
        qs = (
            Room.objects.select_related("room_type")
            .prefetch_related(
                Prefetch("images", queryset=RoomImage.objects.filter(is_active=True), to_attr="_active_images"),
                # Cover image of the parent type powers primary_image_url's fallback.
                Prefetch("room_type__images", queryset=RoomTypeImage.objects.filter(is_active=True)),
            )
            .annotate(
                _reserved_tonight=Exists(tonight),
                _occupied_tonight=Exists(tonight.filter(booking__status=Booking.Status.CHECKED_IN)),
            )
            .order_by("room_number")
        )
        params = self.request.query_params
        if status_param := params.get("status"):
            qs = qs.filter(status=status_param.upper())
        if hk := params.get("housekeeping_status"):
            qs = qs.filter(housekeeping_status=hk.upper())
        if rt := params.get("room_type"):
            qs = qs.filter(Q(room_type__slug=rt) | Q(room_type__pk=rt if str(rt).isdigit() else -1))
        if is_active := params.get("is_active"):
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))
        if search := params.get("search"):
            qs = qs.filter(room_number__icontains=search)
        return qs

    def perform_destroy(self, instance):
        # Rooms are deactivated rather than deleted to preserve booking history.
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        log_action(actor=self.request.user, action="ROOM_DEACTIVATED", instance=instance,
                   request=self.request)

@extend_schema(tags=["Admin · Rooms"])
class RoomImageUploadView(generics.CreateAPIView):
    serializer_class = RoomImageSerializer
    permission_classes = [IsStaffReadOnlyManagerWrite]
    parser_classes = [MultiPartParser, FormParser]

    def create(self, request, room_id=None, *args, **kwargs):
        room = Room.objects.filter(pk=room_id).first()
        if room is None:
            from rest_framework.exceptions import NotFound
            raise NotFound()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = serializer.save(room=room)
        log_action(actor=request.user, action="ROOM_IMAGE_ADDED", instance=room,
                   metadata={"image_id": image.pk}, request=request)
        return success_response(self.get_serializer(image).data, message="Room image uploaded.",
                                status=status.HTTP_201_CREATED)


@extend_schema(tags=["Admin · Rooms"])
class RoomImageDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RoomImageSerializer
    permission_classes = [IsStaffReadOnlyManagerWrite]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        return RoomImage.objects.select_related("room")

    def perform_update(self, serializer):
        instance = serializer.save()
        log_action(actor=self.request.user, action="ROOM_IMAGE_UPDATED", instance=instance.room,
                   metadata={"image_id": instance.pk}, request=self.request)

    def perform_destroy(self, instance):
        room = instance.room
        image_id = instance.pk
        instance.delete()
        log_action(actor=self.request.user, action="ROOM_IMAGE_REMOVED", instance=room,
                   metadata={"image_id": image_id}, request=self.request)
