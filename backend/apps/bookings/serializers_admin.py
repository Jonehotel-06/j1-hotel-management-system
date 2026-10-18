# apps/bookings/serializers_admin.py
"""Staff serializers for booking and guest management."""
from decimal import Decimal

from rest_framework import serializers

from apps.core.utils import money
from apps.rooms.models import Room
from apps.rooms.serializers import RoomSerializer

from .models import Booking, BookingRoom, Guest
from .serializers import (
    BookingRoomSerializer,
    RoomGuestInputSerializer,
    GuestReadSerializer,
    serializer_folio_charge_map,
)


class AdminBookingListSerializer(serializers.ModelSerializer):
    """Row shape for the staff bookings table — one request for the whole page."""

    guest_name = serializers.CharField(source="guest.full_name", read_only=True)
    guest_phone = serializers.CharField(source="guest.phone", read_only=True)
    guest_email = serializers.CharField(source="guest.email", read_only=True)
    room_type_name = serializers.CharField(source="room_type.name", read_only=True)
    room_numbers = serializers.SerializerMethodField()
    nights = serializers.IntegerField(read_only=True)
    amount_due = serializers.SerializerMethodField()
    # The desk must see what the guest actually owes: the stay balance PLUS the
    # posted restaurant/bar/room-service/other charges sitting on the guest's
    # folio. `amount_due` alone was ₦25,500 short on an audited booking (D4).
    # The charge balance is batched per page by the view.
    folio_charges = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()
    # Per-room arrival state, so a LIST row can say "1 of 3 rooms in house"
    # without loading every booking's assignments. Views annotate these counts
    # in one query (see views_admin._with_room_arrival_counts); the method
    # falls back to the prefetched rows if a caller did not annotate.
    rooms_checked_in = serializers.SerializerMethodField()
    rooms_awaiting_arrival = serializers.SerializerMethodField()

    def _folio_charge_balance(self, obj):
        # Batched per page by the view's context, or lazily resolved for the
        # whole serialization in one query (see serializer_folio_charge_map).
        return serializer_folio_charge_map(self).get(obj.pk, Decimal("0.00"))

    def get_folio_charges(self, obj) -> str:
        return money(self._folio_charge_balance(obj))

    def get_outstanding_balance(self, obj) -> str:
        from apps.finance.services.folio_service import booking_outstanding_balance

        return money(
            booking_outstanding_balance(
                booking=obj, folio_charge_balance=self._folio_charge_balance(obj)
            )
        )

    class Meta:
        model = Booking
        fields = [
            "id", "booking_reference", "guest_name", "guest_phone", "guest_email",
            "room_type_name", "room_numbers", "check_in", "check_out", "nights",
            "number_of_rooms", "number_of_guests", "total_amount", "amount_paid",
            "amount_due", "folio_charges", "outstanding_balance",
            "rooms_checked_in", "rooms_awaiting_arrival",
            "currency", "status", "payment_status", "source", "created_at",
        ]

    def get_room_numbers(self, obj) -> list:
        return [a.room.room_number for a in getattr(obj, "_assignments", obj.room_assignments.all())]

    def get_amount_due(self, obj) -> str:
        return money(obj.amount_due)

    def _arrival_counts(self, obj):
        annotated = getattr(obj, "rooms_checked_in_count", None)
        if annotated is not None:
            return annotated, getattr(obj, "rooms_awaiting_count", 0)
        assignments = list(getattr(obj, "_assignments", None) or obj.room_assignments.all())
        checked = sum(1 for a in assignments if a.checked_in_at is not None)
        awaiting = sum(
            1 for a in assignments if a.checked_in_at is None and a.not_arrived_at is None
        )
        return checked, awaiting

    def get_rooms_checked_in(self, obj) -> int:
        return self._arrival_counts(obj)[0]

    def get_rooms_awaiting_arrival(self, obj) -> int:
        return self._arrival_counts(obj)[1]


class AdminBookingDetailSerializer(AdminBookingListSerializer):
    guest = GuestReadSerializer(read_only=True)
    room_assignments = BookingRoomSerializer(many=True, read_only=True)
    offer_title = serializers.SerializerMethodField()
    # Lets the desk UI disable the Check in action with the real reason
    # instead of letting staff click into a 409.
    can_check_in = serializers.SerializerMethodField()
    check_in_blocked_reason = serializers.SerializerMethodField()
    # Multi-room record keeping: how many of the booked rooms actually arrived.
    rooms_checked_in = serializers.SerializerMethodField()
    rooms_awaiting_arrival = serializers.SerializerMethodField()
    rooms_not_arrived = serializers.SerializerMethodField()

    class Meta(AdminBookingListSerializer.Meta):
        fields = AdminBookingListSerializer.Meta.fields + [
            "guest", "room_assignments", "adults", "children", "price_per_night",
            "subtotal", "discount_amount", "extra_guest_fee_amount", "tax_amount",
            "fee_amount", "required_payment", "refund_amount", "offer_title",
            "special_requests", "internal_notes", "cancellation_reason",
            "expires_at", "checked_in_at", "checked_out_at", "cancelled_at", "updated_at",
            "can_check_in", "check_in_blocked_reason",
            "rooms_checked_in", "rooms_awaiting_arrival", "rooms_not_arrived",
        ]

    def get_offer_title(self, obj):
        return obj.offer.title if obj.offer_id else None

    def _check_in_state(self, obj):
        from apps.bookings.services.booking_service import can_check_in_today

        cache = self.context.setdefault("_check_in_cache", {})
        if obj.pk not in cache:
            cache[obj.pk] = can_check_in_today(obj)
        return cache[obj.pk]

    def get_can_check_in(self, obj) -> bool:
        return self._check_in_state(obj)[0]

    def get_check_in_blocked_reason(self, obj) -> str:
        return self._check_in_state(obj)[1]

    def _assignments(self, obj):
        return getattr(obj, "_assignments", None) or list(obj.room_assignments.all())

    def get_rooms_not_arrived(self, obj) -> int:
        return sum(1 for assignment in self._assignments(obj) if assignment.not_arrived_at is not None)


class RoomGuestsRoomSerializer(serializers.Serializer):
    """The replacement guest list for ONE room of the booking."""

    assignment_id = serializers.IntegerField(min_value=1)
    guests = RoomGuestInputSerializer(many=True, required=False, default=list)


class AdminRoomGuestsSerializer(serializers.Serializer):
    """PUT the complete per-room guest list.

    Replace-in-full, not append: the desk edits one dialog, and a half-applied
    list would misstate who is expected. Removing a room from the payload
    removes its guests.
    """

    rooms = RoomGuestsRoomSerializer(many=True)


class AdminBookingCreateSerializer(serializers.Serializer):
    """Staff manual booking (walk-in/phone). Guest details are mandatory."""

    room_type = serializers.CharField()
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    rooms = serializers.IntegerField(min_value=1, max_value=50, default=1)
    adults = serializers.IntegerField(min_value=1, max_value=100, default=1)
    children = serializers.IntegerField(min_value=0, max_value=100, default=0)
    source = serializers.ChoiceField(
        choices=[Booking.Source.WALK_IN, Booking.Source.PHONE], default=Booking.Source.WALK_IN
    )
    status = serializers.ChoiceField(
        choices=[Booking.Status.PENDING, Booking.Status.CONFIRMED], default=Booking.Status.CONFIRMED
    )
    offer_code = serializers.CharField(required=False, allow_blank=True, default="")
    special_requests = serializers.CharField(required=False, allow_blank=True, default="")
    internal_notes = serializers.CharField(required=False, allow_blank=True, default="")
    guest = serializers.DictField(child=serializers.CharField(allow_blank=True), required=True)
    # People staying in the additional rooms (same contract as the public
    # booking form). Optional and only meaningful when rooms > 1.
    room_guests = RoomGuestInputSerializer(many=True, required=False, default=list)
    # Optional exact physical room ("Room 203") the receptionist promised.
    # Advisory: availability is re-verified server-side and an equivalent room
    # of the same type may be assigned instead (reported as room_substitution).
    room_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate_room_type(self, value):
        from .services.booking_service import resolve_room_type

        if resolve_room_type(value, active_only=False) is None:
            raise serializers.ValidationError("Unknown room type.")
        return value

    def validate_room_id(self, value):
        if not value:
            return None
        if not Room.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError("That room does not exist or is out of service.")
        return value

    def validate_guest(self, value):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import validate_email

        if not isinstance(value, dict):
            raise serializers.ValidationError("Guest details must be an object.")
        cleaned = {
            k: (str(v).strip() if v is not None else "")
            for k, v in value.items()
        }
        required = ["first_name", "last_name", "email", "phone"]
        errors = {
            key: ["This field is required."]
            for key in required
            if not cleaned.get(key)
        }

        if cleaned.get("first_name") and len(cleaned["first_name"]) > 100:
            errors["first_name"] = ["First name must be 100 characters or fewer."]
        if cleaned.get("last_name") and len(cleaned["last_name"]) > 100:
            errors["last_name"] = ["Last name must be 100 characters or fewer."]

        if cleaned.get("email"):
            try:
                validate_email(cleaned["email"])
            except DjangoValidationError:
                errors["email"] = ["Enter a valid email address."]
            else:
                if len(cleaned["email"]) > 254:
                    errors["email"] = ["Email address must be 254 characters or fewer."]
                else:
                    cleaned["email"] = cleaned["email"].lower()

        if cleaned.get("phone"):
            raw_phone = cleaned["phone"]
            compact_phone = "".join(ch for ch in raw_phone if not ch.isspace())
            digits_only = "".join(ch for ch in compact_phone if ch.isdigit())
            if len(digits_only) < 7:
                errors["phone"] = ["Enter a valid phone number (at least 7 digits)."]
            elif len(compact_phone) > 20:
                errors["phone"] = ["Phone number must be 20 characters or fewer."]
            else:
                cleaned["phone"] = compact_phone if (" " in raw_phone or len(raw_phone) > 20) else raw_phone

        if errors:
            raise serializers.ValidationError(errors)
        return cleaned

    def validate(self, attrs):
        room_id = attrs.get("room_id")
        if room_id:
            from .services.booking_service import resolve_room_type

            room_type = resolve_room_type(attrs["room_type"], active_only=False)
            if room_type is None or not Room.objects.filter(
                pk=room_id, room_type=room_type
            ).exists():
                raise serializers.ValidationError(
                    {"room_id": ["That room does not belong to the selected room type."]}
                )

        room_guests = attrs.get("room_guests") or []
        if not room_guests:
            return attrs
        rooms = int(attrs.get("rooms") or 1)
        if rooms < 2:
            raise serializers.ValidationError(
                {"room_guests": ["Room guests can only be added when booking more than one room."]}
            )
        contact_email = str((attrs.get("guest") or {}).get("email") or "").strip().lower()
        seen = set()
        for entry in room_guests:
            email = entry["email"]
            if email == contact_email:
                raise serializers.ValidationError(
                    {"room_guests": [f"{email} is the booking contact — the contact does not need a room guest entry."]}
                )
            if email in seen:
                raise serializers.ValidationError(
                    {"room_guests": [f"{email} is listed twice. Each guest can only be in one room of this booking."]}
                )
            seen.add(email)
            index = entry.get("room_index")
            if index is not None and index >= rooms:
                raise serializers.ValidationError(
                    {"room_guests": [f"Room {index + 1} does not exist on a {rooms}-room booking."]}
                )
        if len(room_guests) > rooms:
            raise serializers.ValidationError(
                {"room_guests": [f"Only {rooms} room(s) were booked, but {len(room_guests)} room guests were supplied."]}
            )
        return attrs


class AdminBookingModifySerializer(serializers.Serializer):
    check_in = serializers.DateField(required=False)
    check_out = serializers.DateField(required=False)
    number_of_rooms = serializers.IntegerField(min_value=1, max_value=50, required=False)
    adults = serializers.IntegerField(min_value=1, max_value=100, required=False)
    children = serializers.IntegerField(min_value=0, max_value=100, required=False)
    special_requests = serializers.CharField(required=False, allow_blank=True)
    internal_notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if "check_in" in attrs and "check_out" in attrs and attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError({"check_out": ["Must be after check-in."]})
        return attrs


class AssignRoomSerializer(serializers.Serializer):
    assignment_id = serializers.IntegerField(min_value=1)
    room_id = serializers.IntegerField(min_value=1)


class RecordActionSerializer(serializers.Serializer):
    """For actions accepting an optional note/flag."""

    reason = serializers.CharField(required=False, allow_blank=True, default="")
    allow_balance_due = serializers.BooleanField(required=False, default=False)


class AdminGuestListSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    bookings_count = serializers.IntegerField(read_only=True)
    last_booking_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = Guest
        fields = [
            "id", "first_name", "last_name", "full_name", "email", "phone",
            "city", "state", "country", "bookings_count", "last_booking_at", "created_at",
        ]


class AdminGuestDetailSerializer(serializers.ModelSerializer):
    """Complete guest profile for the staff profile modal.

    Every non-sensitive field the ``Guest`` model actually stores is exposed so
    the dashboard can show it. The one sensitive column (``identification_number``)
    is masked for receptionists — full value only for managers and admins.
    """

    full_name = serializers.CharField(read_only=True)
    bookings = serializers.SerializerMethodField()
    user_id = serializers.IntegerField(read_only=True, allow_null=True)
    has_account = serializers.SerializerMethodField()
    identification_type_label = serializers.SerializerMethodField()
    identification_number = serializers.SerializerMethodField()
    bookings_count = serializers.SerializerMethodField()
    last_booking_at = serializers.SerializerMethodField()
    total_stays = serializers.SerializerMethodField()
    total_spent = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()

    class Meta:
        model = Guest
        fields = [
            "id", "user_id", "has_account", "first_name", "last_name", "full_name",
            "email", "phone", "address", "city", "state", "country",
            "identification_type", "identification_type_label", "identification_number",
            "special_requests", "bookings", "bookings_count", "last_booking_at",
            "total_stays", "total_spent", "outstanding_balance",
            "created_at", "updated_at",
        ]

    # --- privacy ---------------------------------------------------------
    def _can_see_sensitive(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.role in ("ADMIN", "MANAGER"))

    def get_identification_number(self, obj):
        value = obj.identification_number or ""
        if not value:
            return ""
        if self._can_see_sensitive():
            return value
        # Receptionists see enough to recognise the document, not the number.
        return ("*" * max(0, len(value) - 4)) + value[-4:]

    def get_identification_type_label(self, obj):
        return obj.get_identification_type_display() if obj.identification_type else ""

    # --- metadata --------------------------------------------------------
    def get_has_account(self, obj):
        return obj.user_id is not None

    def get_bookings_count(self, obj):
        """Bookings that count as real history (cancelled/expired excluded).

        Uses the list endpoint's annotation when present so the detail view is
        the only place that pays for a query.
        """
        from apps.bookings.services.booking_service import UNCOUNTED_BOOKING_STATUSES

        annotated = getattr(obj, "bookings_count", None)
        if annotated is not None:
            return annotated
        return obj.bookings.exclude(status__in=UNCOUNTED_BOOKING_STATUSES).count()

    def get_last_booking_at(self, obj):
        value = getattr(obj, "last_booking_at", None)
        if value is None:
            # Only the list endpoint annotates this; the detail view fetches it
            # from the already-prefetched rows (no extra query).
            value = obj.bookings.order_by("-created_at").values_list("created_at", flat=True).first()
        return value.isoformat() if value else None

    def get_total_stays(self, obj):
        return obj.bookings.count()

    def get_total_spent(self, obj):
        from django.db.models import Sum

        return money(
            obj.bookings.exclude(status="CANCELLED").aggregate(total=Sum("amount_paid"))["total"] or 0
        )

    def get_outstanding_balance(self, obj):
        from django.db.models import F, Sum

        balance = (
            obj.bookings.filter(status__in=("CONFIRMED", "CHECKED_IN"))
            .annotate(due=F("total_amount") - F("amount_paid"))
            .aggregate(total=Sum("due"))["total"]
        )
        return money(balance or 0)

    # --- history ---------------------------------------------------------
    def get_bookings(self, obj):
        # The detail view prefetches bookings + their rooms; slicing a *list* of
        # the already-fetched rows keeps this to zero extra queries (and avoids
        # a "already seen with a different queryset" prefetch clash).
        qs = obj.bookings.all()[:15]
        return [
            {
                "id": b.pk,
                "booking_reference": b.booking_reference,
                "room_type_name": b.room_type.name,
                "room_numbers": [a.room.room_number for a in b.room_assignments.all()],
                "check_in": b.check_in.isoformat(),
                "check_out": b.check_out.isoformat(),
                "nights": b.nights,
                "status": b.status,
                "payment_status": b.payment_status,
                "total_amount": money(b.total_amount),
                "amount_paid": money(b.amount_paid),
                "amount_due": money(b.amount_due),
                "currency": b.currency,
                "source": b.source,
                "created_at": b.created_at.isoformat(),
            }
            for b in qs
        ]


class AdminGuestUpdateSerializer(serializers.ModelSerializer):
    # Same guest-phone contract as the public checkout and walk-in creation:
    # compact formatted numbers before the 20-char column limit is enforced.
    # required/blank semantics are unchanged from the model-derived field
    # (Guest.phone is a non-blank column); only the raw max-length parse
    # validator is replaced by validate_phone's clear, normalising check.
    phone = serializers.CharField()

    class Meta:
        model = Guest
        fields = [
            "first_name", "last_name", "email", "phone", "address", "city",
            "state", "country", "identification_type", "identification_number",
            "special_requests",
        ]

    def validate_phone(self, value):
        from .serializers import validate_guest_phone

        return validate_guest_phone(value)


class RescheduleBookingSerializer(serializers.Serializer):
    """New dates for a no-show/confirmed booking. Availability is re-checked
    server-side; these values are a request, never an instruction."""

    check_in = serializers.DateField()
    check_out = serializers.DateField()

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError(
                {"check_out": ["Check-out date must be after the check-in date."]}
            )
        return attrs


class MissedBookingSerializer(AdminBookingListSerializer):
    """A booking whose guest never arrived, plus what staff need to act on it."""

    refundable_amount = serializers.SerializerMethodField()
    cancellation_fee = serializers.SerializerMethodField()
    nights_missed = serializers.SerializerMethodField()

    class Meta(AdminBookingListSerializer.Meta):
        fields = AdminBookingListSerializer.Meta.fields + [
            "refundable_amount", "cancellation_fee", "nights_missed", "checked_in_at",
        ]

    def _policy(self, obj):
        from apps.bookings.services.booking_service import calculate_cancellation_policy

        cache = self.context.setdefault("_policy_cache", {})
        if obj.pk not in cache:
            cache[obj.pk] = calculate_cancellation_policy(obj)
        return cache[obj.pk]

    def get_refundable_amount(self, obj) -> str:
        return money(self._policy(obj)["refund_amount"])

    def get_cancellation_fee(self, obj) -> str:
        return money(self._policy(obj)["cancellation_fee"])

    def get_nights_missed(self, obj) -> int:
        from apps.core.utils import hotel_today

        return max(0, (hotel_today() - obj.check_in).days)


class LateArrivalBookingSerializer(AdminBookingListSerializer):
    """A confirmed booking whose guest missed the first night but may still arrive."""

    nights_missed = serializers.SerializerMethodField()
    can_check_in = serializers.SerializerMethodField()
    check_in_blocked_reason = serializers.SerializerMethodField()

    class Meta(AdminBookingListSerializer.Meta):
        fields = AdminBookingListSerializer.Meta.fields + [
            "nights_missed", "can_check_in", "check_in_blocked_reason",
        ]

    def get_nights_missed(self, obj) -> int:
        from apps.core.utils import hotel_today

        return max(0, (hotel_today() - obj.check_in).days)

    def _check_in_state(self, obj):
        from apps.bookings.services.booking_service import can_check_in_today

        cache = self.context.setdefault("_check_in_cache", {})
        if obj.pk not in cache:
            cache[obj.pk] = can_check_in_today(obj)
        return cache[obj.pk]

    def get_can_check_in(self, obj) -> bool:
        return self._check_in_state(obj)[0]

    def get_check_in_blocked_reason(self, obj) -> str:
        return self._check_in_state(obj)[1]


class SettleGuestBalanceSerializer(serializers.Serializer):
    """Check-out settlement of the guest's whole bill (stay + folio charges).

    ``record_offline_payment`` only ever settled the stay projection, so money
    owed for a guest's requests (restaurant, bar, room service, laundry) had no
    way in once the room was paid. This serializer carries the money the desk
    actually received; whether it is *acceptable* is decided by the finance
    service against the database row, never against browser arithmetic.
    """

    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=1,
                                      max_value=99_999_999)
    provider = serializers.ChoiceField(choices=["CASH", "POS", "BANK_TRANSFER"])
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)
    # Default True: the figure the desk collected includes the guest's unbilled
    # in-house requests, so they are billed to the folio in the same transaction
    # (idempotent) instead of being silently left off the bill.
    include_unbilled = serializers.BooleanField(required=False, default=True)
    cash_session_reference = serializers.CharField(required=False, allow_blank=True, default="",
                                                   max_length=64)
