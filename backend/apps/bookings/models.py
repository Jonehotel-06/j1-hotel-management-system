# apps/bookings/models.py
"""Guests, reservations and per-room assignments.

Design notes
------------
* ``Booking`` is the reservation header (dates, guests, money, status).
* ``BookingRoom`` rows physically block inventory: every active assignment
  reserves ONE concrete room for [check_in, check_out). Double-booking is
  impossible because rows are created inside a transaction after locking the
  room type and re-checking overlaps.
* Monetary fields are Decimals; totals are snapshots calculated server-side at
  creation (a frontend-supplied total is never trusted).
"""
from decimal import Decimal
import hashlib
import secrets

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel
from apps.offers.models import Offer
from apps.rooms.models import Room, RoomType


class Guest(TimeStampedModel):
    """A person who stays at the hotel.

    May be linked to a registered account (``user``) or created standalone by
    staff for walk-in/phone reservations.
    """

    class IdentificationType(models.TextChoices):
        NATIONAL_ID = "NATIONAL_ID", "National ID"
        NIN = "NIN", "NIN Slip"
        DRIVERS_LICENSE = "DRIVERS_LICENSE", "Driver's License"
        PASSPORT = "PASSPORT", "International Passport"
        VOTERS_CARD = "VOTERS_CARD", "Voter's Card"
        OTHER = "OTHER", "Other"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="guest_profile",
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=20)
    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="Nigeria")
    identification_type = models.CharField(max_length=30, choices=IdentificationType.choices,
                                           blank=True, default="")
    # Kept only when the hotel's identification policy requires it.
    identification_number = models.CharField(max_length=64, blank=True, default="")
    special_requests = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class Booking(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending payment"
        CONFIRMED = "CONFIRMED", "Confirmed"
        CHECKED_IN = "CHECKED_IN", "Checked in"
        CHECKED_OUT = "CHECKED_OUT", "Checked out"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired (unpaid)"
        NO_SHOW = "NO_SHOW", "No show"

    class PaymentStatus(models.TextChoices):
        UNPAID = "UNPAID", "Unpaid"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        PAID = "PAID", "Paid"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        REFUNDED = "REFUNDED", "Refunded"
        FAILED = "FAILED", "Payment failed"

    class Source(models.TextChoices):
        WEBSITE = "WEBSITE", "Website"
        WALK_IN = "WALK_IN", "Walk-in"
        PHONE = "PHONE", "Phone"

    booking_reference = models.CharField(max_length=40, unique=True, db_index=True)
    guest = models.ForeignKey(Guest, on_delete=models.PROTECT, related_name="bookings")
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="bookings")
    check_in = models.DateField(db_index=True)
    check_out = models.DateField(db_index=True)
    number_of_rooms = models.PositiveSmallIntegerField(default=1,
                                                       validators=[MinValueValidator(1)])
    adults = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    children = models.PositiveSmallIntegerField(default=0)

    # --- Pricing snapshot (all calculated server-side at creation) ----------
    currency = models.CharField(max_length=3, default="NGN")
    price_per_night = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    extra_guest_fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Amount needed online to confirm the booking (deposit rules come from settings).
    required_payment = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    offer = models.ForeignKey(Offer, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="bookings")

    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices,
                                      default=PaymentStatus.UNPAID, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.WEBSITE)

    special_requests = models.TextField(blank=True, default="")
    internal_notes = models.TextField(blank=True, default="")
    cancellation_reason = models.TextField(blank=True, default="")

    expires_at = models.DateTimeField(null=True, blank=True,
                                      help_text="While PENDING, inventory is held until this time.")
    guest_access_token_hash = models.CharField(max_length=128, blank=True, default="", db_index=True,
                                              help_text="Hash of the unguessable token used for guest self-service access.")
    guest_access_expires_at = models.DateTimeField(null=True, blank=True)
    # Client-generated key that makes POST /api/bookings/ idempotent: if the
    # browser loses the 201 response (timeout, network drop) and the user
    # presses Retry, the SAME key returns the ORIGINAL booking instead of
    # creating a duplicate reservation. Null for bookings created without one
    # (staff manual bookings, older clients).
    idempotency_key = models.CharField(max_length=64, null=True, blank=True, unique=True,
                                       db_index=True,
                                       help_text="Client-supplied idempotency key for safe retries of booking creation.")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="bookings_created")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["room_type", "check_in", "check_out"]),
            models.Index(fields=["status", "check_in"]),
            models.Index(fields=["-created_at"]),
            # Revenue reports reconcile legacy booking-level refund totals by
            # cancellation timestamp using a half-open range predicate.
            models.Index(fields=["cancelled_at"]),
        ]

    def __str__(self):
        return f"Booking {self.booking_reference} ({self.get_status_display()})"

    @staticmethod
    def hash_guest_access_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue_guest_access_token(self):
        """Return a one-time generated bearer token; only its hash is stored."""
        token = secrets.token_urlsafe(32)
        self.guest_access_token_hash = self.hash_guest_access_token(token)
        from django.utils import timezone
        from datetime import timedelta
        self.guest_access_expires_at = timezone.now() + timedelta(days=90)
        self.save(update_fields=["guest_access_token_hash", "guest_access_expires_at", "updated_at"])
        return token

    def guest_token_matches(self, token):
        from django.utils import timezone
        return bool(token and self.guest_access_token_hash and
                    self.guest_access_expires_at and self.guest_access_expires_at > timezone.now() and
                    secrets.compare_digest(self.guest_access_token_hash, self.hash_guest_access_token(token)))

    # --- Convenience properties ---------------------------------------------
    @property
    def nights(self):
        return (self.check_out - self.check_in).days if self.check_in and self.check_out else 0

    @property
    def number_of_guests(self):
        return self.adults + self.children

    @property
    def amount_due(self):
        due = (self.total_amount or Decimal("0.00")) - (self.amount_paid or Decimal("0.00"))
        return max(due, Decimal("0.00"))

    @property
    def is_expired_pending(self):
        from django.utils import timezone

        return (
            self.status == self.Status.PENDING
            and self.expires_at is not None
            and self.expires_at <= timezone.now()
        )


class BookingRoom(models.Model):
    """One reservation ↔ one physical room for [check_in, check_out).

    Two assignments for the same room overlap iff
    ``a.check_in < b.check_out and b.check_in < a.check_out`` — a checkout day
    and a new check-in on that same date do NOT block each other.

    ``checked_in_at`` is PER ROOM, not per booking. A guest who reserves three
    rooms routinely arrives with one of them while the other guests are still
    travelling; recording the arrival room by room is what makes "did everyone
    actually check in?" answerable afterwards. It is NULL until that specific
    room is handed over at the desk.
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="room_assignments")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="reservations")
    check_in = models.DateField(db_index=True)
    check_out = models.DateField(db_index=True)
    checked_in_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                         help_text="When THIS room was handed over at the desk.")
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="booking_rooms_checked_in",
    )
    # Set at checkout for a room nobody ever occupied, so the booking closes
    # with an honest record instead of silently looking fully occupied.
    not_arrived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    arrival_note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["check_in", "room__room_number"]
        indexes = [
            models.Index(fields=["room", "check_in", "check_out"]),
            models.Index(fields=["booking", "room"]),
        ]

    def __str__(self):
        return f"{self.booking.booking_reference} → Room {self.room.room_number}"

    @property
    def arrival_status(self) -> str:
        """One of EXPECTED / AWAITING_ARRIVAL / CHECKED_IN / CHECKED_OUT / DID_NOT_ARRIVE.

        Derived from stored timestamps only — never from the booking's single
        status — so a three-room booking with one room in house reports that
        room as CHECKED_IN and the others as AWAITING_ARRIVAL.
        """
        if self.checked_in_at is None:
            return "DID_NOT_ARRIVE" if self.not_arrived_at else (
                "AWAITING_ARRIVAL" if self.booking.status == Booking.Status.CHECKED_IN else "EXPECTED"
            )
        if self.booking.status == Booking.Status.CHECKED_OUT:
            return "CHECKED_OUT"
        return "CHECKED_IN"


class RoomOccupant(TimeStampedModel):
    """A named guest staying in one reserved room, with their own email.

    Why this is separate from ``Booking.guest``: the booking contact pays and
    manages the reservation, but the people in the other rooms are different
    people. Their email is what lets them (a) sign in to the guest portal with
    their own address and raise/see their own service requests, and (b) gives
    the front desk a per-room record of who was expected and who arrived.

    Emails are stored lowercase so portal lookups and de-duplication are exact.
    """

    booking_room = models.ForeignKey(BookingRoom, on_delete=models.CASCADE, related_name="occupants")
    full_name = models.CharField(max_length=150, blank=True, default="")
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=20, blank=True, default="")
    # The occupant who is the contact for THIS room (usually the one who
    # collected the key). Purely descriptive; it grants nothing extra.
    is_lead_occupant = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True, default="")
    portal_invited_at = models.DateTimeField(null=True, blank=True,
                                             help_text="When the guest-portal sign-in note was emailed.")

    class Meta:
        ordering = ["booking_room_id", "created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["booking_room", "email"], name="room_occupant_unique_email_per_room",
            ),
        ]
        indexes = [models.Index(fields=["email"])]

    def save(self, *args, **kwargs):
        self.email = str(self.email or "").strip().lower()
        self.full_name = str(self.full_name or "").strip()
        self.phone = str(self.phone or "").strip()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name or 'Guest'} <{self.email}> · Room {self.booking_room.room.room_number}"
