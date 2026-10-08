# apps/rooms/serializers.py
from django.db import transaction
from rest_framework import serializers
from rest_framework.relations import MANY_RELATION_KWARGS, ManyRelatedField

from apps.core.storage import absolute_media_url, delete_quietly, save_upload
from apps.core.validators import validate_image_upload

from .models import Amenity, Room, RoomType, RoomTypeImage, RoomImage


class _EmptyAwareManyRelated(ManyRelatedField):
    """Treats a single blank multipart value as "an empty list".

    An HTML form cannot express "no items selected" — the field is simply
    omitted, which in a PATCH means "leave unchanged". The dashboard therefore
    sends one empty string to mean "clear the selection"; normalise that here so
    the relation is emptied instead of failing with "Expected pk value".
    """

    def get_value(self, dictionary):
        value = super().get_value(dictionary)
        if isinstance(value, list) and len(value) == 1 and value[0] in ("", None):
            return []
        return value


class EmptyAwarePrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    @classmethod
    def many_init(cls, *args, **kwargs):
        list_kwargs = {"child_relation": cls(*args, **kwargs)}
        for key in MANY_RELATION_KWARGS:
            if key in kwargs:
                list_kwargs[key] = kwargs[key]
        return _EmptyAwareManyRelated(**list_kwargs)


class ValidatedImageField(serializers.ImageField):
    """ImageField that applies the project's shared upload validation.

    DRF's ImageField only checks that Pillow can open the file; the project rule
    set (extension allow-list, MAX_UPLOAD_MB, real image content) lives in
    apps.core.validators and is applied by the model fields. Running it here too
    means the API rejects a bad upload with a clean field error *before* anything
    is sent to Backblaze.
    """

    def to_internal_value(self, data):
        file = super().to_internal_value(data)
        validate_image_upload(file)
        return file


def _absolute(serializer, image_field):
    """Absolute, browser-usable media URL (B2/CDN URL, or request-absolute)."""
    return absolute_media_url(image_field, serializer.context.get("request"))


def _primary_image(images):
    """Pick the cover image from an iterable of images (primary first)."""
    actives = [img for img in images if img.is_active]
    if not actives:
        return None
    return next((img for img in actives if img.is_primary), actives[0])


class AmenitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Amenity
        fields = ["id", "name", "slug", "icon", "is_active"]


class RoomTypeImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = RoomTypeImage
        fields = ["id", "image", "image_url", "alt_text", "caption", "display_order", "is_primary", "is_active"]
        read_only_fields = ["id", "image_url"]

    def get_image_url(self, obj):
        return _absolute(self, obj.image)

    def create(self, validated_data):
        # Surface storage failures honestly instead of persisting a row that
        # points at a file the backend never managed to store.
        return save_upload(lambda: super(RoomTypeImageSerializer, self).create(validated_data),
                           context="room-type image")

    def update(self, instance, validated_data):
        return save_upload(lambda: super(RoomTypeImageSerializer, self).update(instance, validated_data),
                           context="room-type image")


class RoomImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = RoomImage
        fields = ["id", "image", "image_url", "alt_text", "caption", "display_order", "is_primary", "is_active"]
        read_only_fields = ["id", "image_url"]

    def get_image_url(self, obj):
        return _absolute(self, obj.image)

    def create(self, validated_data):
        return save_upload(lambda: super(RoomImageSerializer, self).create(validated_data),
                           context="room image")

    def update(self, instance, validated_data):
        return save_upload(lambda: super(RoomImageSerializer, self).update(instance, validated_data),
                           context="room image")


class RoomTypeListSerializer(serializers.ModelSerializer):
    """Lightweight representation for catalog/availability lists."""

    primary_image_url = serializers.SerializerMethodField()
    amenities = serializers.SerializerMethodField()

    class Meta:
        model = RoomType
        fields = [
            "id", "name", "slug", "short_description", "base_price", "max_guests",
            "bed_type", "bed_count", "room_size", "view", "is_featured",
            "primary_image_url", "amenities",
        ]

    def get_primary_image_url(self, obj):
        images = getattr(obj, "_prefetched_images", None)
        if images is None:
            # `images` is prefetched by the catalog/admin querysets; reuse that
            # cache instead of issuing a query per row.
            images = obj.images.all()
        primary = _primary_image(images)
        return _absolute(self, primary.image if primary else None)

    def get_amenities(self, obj):
        return [{"name": a.name, "icon": a.icon} for a in obj.amenities.all() if a.is_active]


class RoomTypeDetailSerializer(RoomTypeListSerializer):
    """Full representation for the room detail page."""

    images = serializers.SerializerMethodField()
    amenities = serializers.SerializerMethodField()

    class Meta(RoomTypeListSerializer.Meta):
        fields = RoomTypeListSerializer.Meta.fields + [
            "description", "smoking_policy", "children_allowed",
            "extra_guest_allowed", "extra_guest_fee", "images",
        ]

    def get_images(self, obj):
        images = sorted(
            (img for img in obj.images.all() if img.is_active),
            key=lambda i: (i.display_order, i.id),
        )
        return RoomTypeImageSerializer(images, many=True, context=self.context).data

    def get_amenities(self, obj):
        return AmenitySerializer(
            [a for a in obj.amenities.all() if a.is_active], many=True
        ).data


# ---------------------------------------------------------------------------
# Staff serializers
# ---------------------------------------------------------------------------
class RoomTypeAdminSerializer(serializers.ModelSerializer):
    """Staff CRUD for room types, including the cover image.

    The data model stores room-type photos in the related ``RoomTypeImage``
    gallery, so this serializer does NOT add a duplicate image column. Instead
    it exposes a write-only ``image`` upload that becomes the gallery's primary
    (cover) image, which is what the dashboard form submits as multipart. The
    existing ``images`` array and the dedicated image endpoints are untouched.
    """

    amenity_ids = EmptyAwarePrimaryKeyRelatedField(
        queryset=Amenity.objects.filter(is_active=True), many=True, required=False, source="amenities"
    )
    images = RoomTypeImageSerializer(many=True, read_only=True)
    room_count = serializers.IntegerField(read_only=True, required=False)

    # Cover-image upload (write) + its URL (read). Backward compatible: both are
    # additive, every previously returned field is still present.
    image = ValidatedImageField(
        write_only=True, required=False, allow_null=True,
        help_text="Cover image upload (multipart). Becomes the primary gallery image.",
    )
    image_alt_text = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=200,
        help_text="Alt text stored with the uploaded cover image.",
    )
    remove_image = serializers.BooleanField(
        write_only=True, required=False,
        help_text="Set true to delete the current cover image.",
    )
    primary_image_url = serializers.SerializerMethodField()

    class Meta:
        model = RoomType
        fields = [
            "id", "name", "slug", "description", "short_description", "base_price",
            "max_guests", "bed_type", "bed_count", "room_size", "view", "smoking_policy",
            "children_allowed", "extra_guest_allowed", "extra_guest_fee", "amenity_ids",
            "is_featured", "is_active", "display_order", "images", "room_count",
            "image", "image_alt_text", "remove_image", "primary_image_url",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]

    def get_primary_image_url(self, obj):
        primary = _primary_image(obj.images.all())
        return _absolute(self, primary.image if primary else None)

    # -- cover image helpers -------------------------------------------------
    def _pop_image_fields(self, validated_data):
        return (
            validated_data.pop("image", None),
            validated_data.pop("image_alt_text", ""),
            validated_data.pop("remove_image", False),
        )

    def _apply_cover_image(self, room_type, image_file, alt_text, remove):
        """Create/replace/delete the primary gallery image for this room type.

        Runs inside the caller's transaction. Any object written to remote
        storage is removed again if the surrounding transaction cannot commit,
        so the bucket never keeps a file the database does not reference.
        """
        current = _primary_image(room_type.images.all())

        if remove and current is not None and image_file is None:
            current.image.delete(save=False)   # drop the stored object
            current.delete()
            return

        if image_file is None:
            return

        def _create():
            return RoomTypeImage.objects.create(
                room_type=room_type,
                image=image_file,
                alt_text=alt_text or (current.alt_text if current else "") or room_type.name,
                display_order=current.display_order if current else 0,
                is_primary=True,
                is_active=True,
            )

        new_image = save_upload(_create, context=f"room type #{room_type.pk} cover image")
        # If anything later in this transaction fails, the DB row disappears —
        # remember the stored object so it can be removed too (no orphans).
        self._uploaded_for_cleanup = new_image.image

        if remove and current is not None:
            # Explicit "replace and delete the old file" request.
            current.image.delete(save=False)
            current.delete()

    def create(self, validated_data):
        image_file, alt_text, remove = self._pop_image_fields(validated_data)
        self._uploaded_for_cleanup = None
        try:
            with transaction.atomic():
                room_type = super().create(validated_data)
                self._apply_cover_image(room_type, image_file, alt_text, remove)
        except Exception:
            delete_quietly(getattr(self, "_uploaded_for_cleanup", None))
            raise
        return room_type

    def update(self, instance, validated_data):
        image_file, alt_text, remove = self._pop_image_fields(validated_data)
        self._uploaded_for_cleanup = None
        try:
            with transaction.atomic():
                room_type = super().update(instance, validated_data)
                self._apply_cover_image(room_type, image_file, alt_text, remove)
        except Exception:
            delete_quietly(getattr(self, "_uploaded_for_cleanup", None))
            raise
        return room_type


class RoomSerializer(serializers.ModelSerializer):
    room_type_name = serializers.CharField(source="room_type.name", read_only=True)
    room_type_slug = serializers.CharField(source="room_type.slug", read_only=True)
    # Additive, backward-compatible context so the dashboard can label a room
    # with its rate/capacity without a second request per row.
    room_type_base_price = serializers.DecimalField(
        source="room_type.base_price", max_digits=12, decimal_places=2, read_only=True
    )
    room_type_max_guests = serializers.IntegerField(source="room_type.max_guests", read_only=True)
    room_type_is_active = serializers.BooleanField(source="room_type.is_active", read_only=True)
    images = serializers.SerializerMethodField()
    primary_image_url = serializers.SerializerMethodField()
    effective_status = serializers.SerializerMethodField()

    # Optional image upload handled in one multipart request with the room
    # itself (the physical-room gallery lives in RoomImage — no duplicate field).
    image = ValidatedImageField(
        write_only=True, required=False, allow_null=True,
        help_text="Optional room photo (multipart). Becomes the room's primary image.",
    )

    def _active_image_list(self, obj):
        cached = getattr(obj, "_active_images", None)
        if cached is not None:
            return cached
        return [img for img in obj.images.all() if img.is_active]

    def get_images(self, obj):
        return RoomImageSerializer(self._active_image_list(obj), many=True, context=self.context).data

    def get_primary_image_url(self, obj):
        """Room's own photo, falling back to its room type's cover image.

        Rooms inherit their marketing imagery from the room type unless a
        specific photo of that physical room exists.
        """
        primary = _primary_image(self._active_image_list(obj))
        if primary is not None:
            return _absolute(self, primary.image)
        room_type = obj.room_type
        if room_type is None:
            return None
        rt_primary = _primary_image(room_type.images.all())
        return _absolute(self, rt_primary.image if rt_primary else None)

    def get_effective_status(self, obj) -> str:
        """Reservation-aware status for tonight (spec §12).

        Operational blocks (maintenance / out-of-service) win; otherwise a
        checked-in stay ⇒ OCCUPIED, an active reservation covering tonight ⇒
        RESERVED, else AVAILABLE. Uses the annotations computed by the admin
        viewset from the authoritative availability engine; falls back to the
        raw operational status when annotations are absent.
        """
        if obj.status in (Room.Status.MAINTENANCE, Room.Status.OUT_OF_SERVICE):
            return obj.status
        if not obj.is_active:
            return Room.Status.OUT_OF_SERVICE
        occupied = getattr(obj, "_occupied_tonight", None)
        reserved = getattr(obj, "_reserved_tonight", None)
        if occupied is None and reserved is None:
            return obj.status
        if occupied:
            return Room.Status.OCCUPIED
        if reserved:
            return Room.Status.RESERVED
        return Room.Status.AVAILABLE

    def _apply_image(self, room, image_file):
        if image_file is None:
            return None

        def _create():
            return RoomImage.objects.create(
                room=room, image=image_file, alt_text=f"Room {room.room_number}",
                is_primary=True, is_active=True,
            )

        return save_upload(_create, context=f"room #{room.pk} image")

    def create(self, validated_data):
        image_file = validated_data.pop("image", None)
        created_image = None
        try:
            with transaction.atomic():
                room = super().create(validated_data)
                created_image = self._apply_image(room, image_file)
        except Exception:
            delete_quietly(getattr(created_image, "image", None))
            raise
        return room

    def update(self, instance, validated_data):
        image_file = validated_data.pop("image", None)
        created_image = None
        try:
            with transaction.atomic():
                room = super().update(instance, validated_data)
                created_image = self._apply_image(room, image_file)
        except Exception:
            delete_quietly(getattr(created_image, "image", None))
            raise
        return room

    class Meta:
        model = Room
        fields = [
            "id", "room_number", "room_type", "room_type_name", "room_type_slug",
            "room_type_base_price", "room_type_max_guests", "room_type_is_active", "floor",
            "status", "effective_status", "housekeeping_status", "notes", "is_active",
            "images", "image", "primary_image_url", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PublicRoomOptionSerializer(serializers.ModelSerializer):
    """Public, deliberately minimal view of one physical room.

    Only what a guest needs to pick "Room 203": its stable id, its number, its
    floor and — when dates are supplied — whether the availability engine says
    it is free. Housekeeping state, notes and internal flags stay private.
    """

    available = serializers.SerializerMethodField()

    class Meta:
        model = Room
        fields = ["id", "room_number", "floor", "available"]
        read_only_fields = fields

    def get_available(self, obj):
        window = self.context.get("window")
        if not window:
            return None

        # RoomTypeRoomsView supplies one precomputed set for the entire list.
        # Keep the authoritative fallback for direct serializer callers, where
        # no view-level context exists, rather than silently changing behavior.
        blocked = self.context.get("blocked_room_ids")
        if blocked is None:
            check_in, check_out, now = window
            from apps.bookings.services import availability

            blocked = set(
                availability.blocked_room_ids(
                    room_type_id=obj.room_type_id,
                    check_in=check_in,
                    check_out=check_out,
                    now=now,
                )
            )
        return obj.pk not in blocked
