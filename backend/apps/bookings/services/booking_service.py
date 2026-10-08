# apps/bookings/services/booking_service.py
"""Booking business operations.

All multi-step, integrity-sensitive operations live here — views stay thin.
Every state change is audited; every public notification is emitted here.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.utils import timezone

from apps.audit.services import log_action
from apps.core.emails import queue_email
from apps.core.exceptions import (
    BookingExpiredError,
    BookingStateError,
    CancellationNotAllowedError,
    InvalidDatesError,
    OutstandingBalanceError,
    RoomUnavailableError,
)
from apps.core.utils import combine_hotel_datetime, generate_booking_reference, hotel_today
from apps.hotel.models import HotelSettings
from apps.notifications.services import notify_staff, notify_users
from apps.offers import services as offer_services
from apps.rooms.models import Room, RoomType, RoomTypeImage

from ..models import Booking, BookingRoom, Guest
from . import availability, pricing
from .pricing import calculate_quote

logger = logging.getLogger("apps")

GUEST_BOOKING_LINK = "/my-bookings.html"
STAFF_BOOKING_LINK = "/dashboard/booking-details.html"

# Staff are warned this many minutes before a checked-in guest's scheduled
# checkout time (spec: 30-minute checkout warning).
CHECKOUT_WARNING_MINUTES = 30


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def resolve_room_type(value, *, active_only=True, queryset=None):
    qs = queryset or RoomType.objects.all()
    if active_only:
        qs = qs.filter(is_active=True)
    obj = qs.filter(slug=str(value)).first()
    if obj is None and str(value).isdigit():
        obj = qs.filter(pk=int(value)).first()
    return obj


def get_booking_by_reference_or_id(lookup):
    booking = Booking.objects.filter(booking_reference=lookup).first()
    if booking is None and str(lookup).isdigit():
        booking = Booking.objects.filter(pk=int(lookup)).first()
    return booking


def guest_booking_link(booking, token=None):
    """Absolute self-service link for the guest.

    Used in emails, so it must be a full URL (a relative path is dead in an
    email client). The raw access token is only ever available on the instance
    right after creation — persisted bookings carry only its hash, so links
    built later are reference-only and the guest supplies their saved token.
    """
    from django.conf import settings

    link = f"{settings.FRONTEND_URL}{GUEST_BOOKING_LINK}?ref={booking.booking_reference}"
    token = token or getattr(booking, "guest_access_token", None)
    return link + (f"&token={token}" if token else "")


def staff_booking_link(booking):
    return f"{STAFF_BOOKING_LINK}?ref={booking.booking_reference}"


def _unique_booking_reference():
    for _ in range(10):
        reference = generate_booking_reference()
        if not Booking.objects.filter(booking_reference=reference).exists():
            return reference
    raise RuntimeError("Could not allocate a unique booking reference.")


def refresh_expired_pending(booking):
    """Lazily flip an expired PENDING booking and release its hold."""
    if booking.is_expired_pending:
        expire_pending_booking(booking)
        booking.refresh_from_db()
    return booking


# ---------------------------------------------------------------------------
# Availability search (public)
# ---------------------------------------------------------------------------
def search_availability(*, check_in, check_out, guests=1, rooms=1, room_type_value=None, request=None):
    from apps.rooms.serializers import RoomTypeListSerializer

    settings_obj = HotelSettings.get_settings()
    types_qs = (
        RoomType.objects.filter(is_active=True)
        .prefetch_related("amenities", Prefetch("images", queryset=RoomTypeImage.objects.filter(is_active=True)))
        .order_by("display_order", "name")
    )
    if room_type_value:
        selected = resolve_room_type(room_type_value, queryset=types_qs)
        types_qs = types_qs.filter(pk=selected.pk) if selected else types_qs.none()

    if guests and guests > rooms * 12:  # hard sanity cap on abuse
        from apps.core.exceptions import CapacityExceededError

        raise CapacityExceededError("Guest count is too large for the requested rooms.")

    # Materialise the catalog once so its amenities/images use the queryset's
    # prefetch cache. Availability and public-offer candidates are then batched
    # across the same stable list instead of adding work per room type.
    room_types = list(types_qs)
    now = timezone.now()
    available_counts = availability.available_room_counts(
        room_type_ids=[room_type.pk for room_type in room_types],
        check_in=check_in,
        check_out=check_out,
        now=now,
    )
    offer_candidates = offer_services.public_offer_candidates_for_room_types(
        room_types=room_types,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
    )

    results = []
    for room_type in room_types:
        available_count = available_counts.get(room_type.pk, 0)
        capacity_error = None
        pricing = None
        bookable = False
        try:
            quote = calculate_quote(
                room_type=room_type, check_in=check_in, check_out=check_out,
                rooms=rooms, adults=guests, children=0, settings_obj=settings_obj,
                offer_candidates=offer_candidates.get(room_type.pk, ()),
            )
            pricing = quote.to_api_dict()
            bookable = available_count >= rooms
        except InvalidDatesError:
            raise
        except Exception as exc:  # capacity overflow → report but keep other types
            capacity_error = str(getattr(exc, "detail", exc))

        results.append(
            {
                "room_type": RoomTypeListSerializer(room_type, context={"request": request}).data,
                "available_rooms": available_count,
                "requested_rooms": rooms,
                "max_guests_per_room": room_type.max_guests,
                "extra_guest_allowed": room_type.extra_guest_allowed,
                "bookable": bookable and capacity_error is None,
                "pricing": pricing,
                "message": capacity_error
                or (None if bookable else "Not enough rooms available for these dates."),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Guest records
# ---------------------------------------------------------------------------
def upsert_guest(*, user=None, guest_data=None) -> Guest:
    """Find-or-create the Guest record used for a booking.

    Logged-in guests reuse their profile-linked record; contact details sent
    with the booking may update it. Staff-supplied data (no user account)
    matches an existing standalone record by email.
    """
    data = guest_data or {}
    if user is not None:
        guest = getattr(user, "guest_profile", None)
    else:
        guest = Guest.objects.filter(
            email__iexact=data.get("email", "").strip(), user__isnull=True
        ).first() if data.get("email") else None

    values = {
        "first_name": data.get("first_name") or (user.first_name if user else ""),
        "last_name": data.get("last_name") or (user.last_name if user else ""),
        "email": data.get("email") or (user.email if user else ""),
        "phone": data.get("phone") or (user.phone if user else ""),
        "address": data.get("address", ""),
        "city": data.get("city", ""),
        "state": data.get("state", ""),
        "country": data.get("country") or "Nigeria",
        "identification_type": data.get("identification_type", ""),
        "identification_number": data.get("identification_number", ""),
        "special_requests": data.get("special_requests", ""),
    }
    if guest is None:
        guest = Guest(user=user)
        # A brand new record fills every field.
        for field_name, value in values.items():
            setattr(guest, field_name, value)
        guest.save()
        return guest

    changed = False
    for field_name, value in values.items():
        if value not in (None, "") and getattr(guest, field_name) != value:
            setattr(guest, field_name, value)
            changed = True
    if user is not None and guest.user_id is None:
        guest.user = user
        changed = True
    if changed:
        guest.save()
    return guest


# ---------------------------------------------------------------------------
# Booking creation (public guests + staff manual bookings)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_booking(*, room_type_value, check_in, check_out, rooms, adults, children,
                   offer_code=None, special_requests="", user=None, guest_data=None,
                   source=Booking.Source.WEBSITE, require_payment=True, actor=None,
                   request=None, room_id=None, idempotency_key=None) -> Booking:
    """Create a reservation.

    ``room_id`` requests one EXACT physical room ("Book this room — Room 203").
    The backend never trusts it blindly: the room must belong to the requested
    room type, be in service and be free for the stay. When it is not, a
    deterministic substitute is selected by the availability engine and
    exposed to the caller as ``booking.room_substitution`` so the guest can be
    told — a room is never swapped silently.
    """
    is_staff = bool(actor and actor.is_staff_member)
    settings_obj = HotelSettings.get_settings()

    candidate = resolve_room_type(room_type_value, active_only=not is_staff)
    if candidate is None:
        from rest_framework.exceptions import NotFound

        raise NotFound("Room type not found.")
    # Lock the room type row: all bookings for the same type serialize here,
    # which makes the availability re-check below race-free.
    room_type = RoomType.objects.select_for_update().get(pk=candidate.pk)

    if rooms < 1:
        raise InvalidDatesError("At least one room must be requested.")

    # The guest row is resolved BEFORE pricing: a personal (per-guest) discount
    # is part of the authoritative price, so the pricing engine must know who
    # is booking. Everything here runs inside the same atomic block, so an
    # unsuccessful booking never leaves a stray guest behind.
    guest = upsert_guest(user=user, guest_data=guest_data)

    quote = calculate_quote(
        room_type=room_type, check_in=check_in, check_out=check_out, rooms=rooms,
        adults=adults, children=children, offer_code=offer_code,
        settings_obj=settings_obj, for_staff=is_staff, guest=guest,
    )

    # Re-verify inventory INSIDE the lock; this check is authoritative —
    # an earlier availability search is never trusted at creation time.
    free_rooms = list(
        availability.available_rooms_queryset(
            room_type=room_type, check_in=check_in, check_out=check_out, for_update=True
        )
    )
    substitution = None

    # --- Exact physical room requested ("Book this room") -------------------
    requested_room = None
    if room_id:
        requested_room = (
            Room.objects.filter(pk=room_id, is_active=True, room_type=room_type)
            .exclude(status__in=availability.OPERATIONALLY_BLOCKED)
            .first()
        )
        if requested_room is None:
            raise RoomUnavailableError(
                "That room is not available for the selected dates. "
                "Please choose another room."
            )
        if requested_room in free_rooms:
            # Happy path: the exact room the guest clicked is still free.
            free_rooms = [requested_room] + [r for r in free_rooms if r.pk != requested_room.pk]
        else:
            substitute = availability.find_substitute_room(
                room_type=room_type,
                check_in=check_in,
                check_out=check_out,
                guests=int(adults or 1) + int(children or 0),
                exclude_room_ids=[requested_room.pk],
            )
            if substitute is None:
                raise RoomUnavailableError(
                    f"Room {requested_room.room_number} is no longer available for your "
                    f"selected dates and no equivalent room of the same type could be "
                    f"held. Please choose different dates or another room type."
                )
            if substitute["price_changed"]:
                # Never change what the guest owes without their consent.
                raise RoomUnavailableError(
                    f"Room {requested_room.room_number} is no longer available for your "
                    f"selected dates. Please choose another room."
                )
            alt_type = substitute["room_type"]
            if alt_type.pk != room_type.pk:
                # Cross-type relocation (identical nightly rate) — re-lock the
                # new type and re-price so the snapshot stays consistent.
                room_type = RoomType.objects.select_for_update().get(pk=alt_type.pk)
                quote = calculate_quote(
                    room_type=room_type, check_in=check_in, check_out=check_out, rooms=rooms,
                    adults=adults, children=children, offer_code=offer_code,
                    settings_obj=settings_obj, for_staff=is_staff, guest=guest,
                )
            free_rooms = [substitute["room"]] + [
                r for r in free_rooms if r.pk != substitute["room"].pk
            ]
            substitution = {
                "requested_room_id": requested_room.pk,
                "requested_room_number": requested_room.room_number,
                "assigned_room_id": substitute["room"].pk,
                "assigned_room_number": substitute["room"].room_number,
                "room_type": room_type.name,
                "room_type_id": room_type.pk,
                "price_changed": False,
                "reason": substitute["reason"],
            }

    chosen_rooms = free_rooms[:rooms]
    if len(chosen_rooms) < rooms:
        remaining = availability.available_room_count(
            room_type=room_type, check_in=check_in, check_out=check_out
        )
        raise RoomUnavailableError(
            f"Only {remaining} room(s) of this type remain for the selected dates."
        )
    free_rooms = chosen_rooms

    pending = require_payment
    booking = Booking.objects.create(
        booking_reference=_unique_booking_reference(),
        idempotency_key=idempotency_key,
        guest=guest,
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        number_of_rooms=rooms,
        adults=adults,
        children=children,
        currency=quote.currency,
        price_per_night=quote.price_per_night,
        subtotal=quote.subtotal,
        discount_amount=quote.discount,
        extra_guest_fee_amount=quote.extra_guest_fee,
        tax_amount=quote.tax,
        fee_amount=quote.service_fee,
        total_amount=quote.total,
        required_payment=quote.required_payment if require_payment else 0,
        offer=quote.offer,
        status=Booking.Status.PENDING if pending else Booking.Status.CONFIRMED,
        source=source,
        special_requests=special_requests or "",
        expires_at=(
            timezone.now() + timedelta(minutes=settings_obj.pending_booking_minutes)
            if pending
            else None
        ),
        created_by=actor if is_staff or (user and user.is_staff_member) else None,
    )
    # The raw token is returned only once to the guest; only its digest is stored.
    booking.guest_access_token = booking.issue_guest_access_token()
    BookingRoom.objects.bulk_create(
        [
            BookingRoom(booking=booking, room=room, check_in=check_in, check_out=check_out)
            for room in free_rooms
        ]
    )
    # Immutable snapshot of a personal discount that was applied. Written once,
    # never re-read from the GuestDiscount row, so deactivating or editing the
    # discount later can never change this booking's historical amounts.
    if quote.guest_discount is not None and quote.discount > 0:
        from apps.offers.models import GuestDiscountApplication

        GuestDiscountApplication.objects.create(
            booking=booking,
            guest_discount=quote.guest_discount,
            guest=guest,
            discount_type=quote.guest_discount.discount_type,
            discount_value=quote.guest_discount.discount_value,
            amount=quote.discount,
            currency=booking.currency,
            reason=quote.guest_discount.reason,
        )

    # Transient (not persisted) hand-off to the API layer: when the exact room
    # the guest picked could not be held, callers MUST surface this to them.
    booking.room_substitution = substitution

    log_action(
        actor=actor or user,
        action="BOOKING_CREATED",
        instance=booking,
        metadata={
            "reference": booking.booking_reference,
            "source": booking.source,
            "total": str(booking.total_amount),
            "rooms": [r.room_number for r in free_rooms],
            **({
                "room_substitution": {
                    "requested": substitution["requested_room_number"],
                    "assigned": substitution["assigned_room_number"],
                    "reason": substitution["reason"],
                }
            } if substitution else {}),
        },
        request=request,
        summary=f"Booking {booking.booking_reference} created ({booking.source})",
    )

    guest_name = booking.guest.full_name
    notify_staff(
        type="BOOKING_CREATED",
        title=f"New booking {booking.booking_reference}",
        message=(
            f"{guest_name} booked {booking.number_of_rooms} × {room_type.name}, "
            f"{booking.check_in} → {booking.check_out} ({booking.nights} night(s))."
        ),
        link=staff_booking_link(booking),
    )
    if booking.guest.user_id:
        notify_users(
            [booking.guest.user],
            type="BOOKING_CREATED",
            title=f"Booking {booking.booking_reference} created",
            message="Your reservation was created and is awaiting payment."
            if pending
            else "Your reservation is confirmed.",
            link=guest_booking_link(booking),
        )

    # Emails are delivered strictly AFTER the booking transaction commits
    # (queue_email uses transaction.on_commit) and are sent SYNCHRONOUSLY in
    # this same request — no Celery, no Redis, no worker. A rolled-back
    # booking sends nothing.
    if booking.guest.email:
        if pending:
            from apps.core.email_design import render_notice_email
            from apps.core.formatting import format_date, format_datetime, format_money

            hotel = HotelSettings.get_settings()
            hold_until = format_datetime(booking.expires_at) if booking.expires_at else ""
            text_body, html_body = render_notice_email(
                category="Reservation on hold",
                title="Complete your booking",
                greeting=f"Hello {booking.guest.first_name},",
                paragraphs=[
                    (
                        f"Your reservation at {hotel.hotel_name} has been created and is "
                        + (f"on hold until {hold_until}. " if hold_until else "awaiting payment. ")
                        + "Please complete the payment below to confirm your stay."
                    ),
                ],
                details=[
                    {"label": "Booking reference", "value": booking.booking_reference},
                    {"label": "Room", "value": f"{booking.number_of_rooms} × {room_type.name}"},
                    {"label": "Check-in", "value": format_date(booking.check_in)},
                    {"label": "Check-out", "value": f"{format_date(booking.check_out)} · {booking.nights} night(s)"},
                    {"label": "Total", "value": format_money(booking.total_amount, booking.currency)},
                    {"label": "Amount to confirm", "value": format_money(booking.required_payment, booking.currency)},
                ],
                cta_label="Complete Payment",
                cta_url=guest_booking_link(booking),
                footnote=(
                    "If the hold expires before payment is received, the rooms are "
                    "released automatically and the reservation cannot be confirmed."
                ),
                preheader=f"Complete payment to confirm booking {booking.booking_reference}.",
            )
            queue_email(
                kind="BOOKING_PENDING",
                booking_reference=booking.booking_reference,
                subject=f"Complete your booking {booking.booking_reference} — {hotel.hotel_name}",
                message=text_body,
                html_message=html_body,
                recipients=[booking.guest.email],
            )
        else:
            _send_confirmation_email(booking)

    logger.info(
        "Booking %s created: %s x%s %s→%s total=%s source=%s",
        booking.booking_reference, room_type.slug, rooms, check_in, check_out,
        booking.total_amount, source,
    )
    return booking


def _send_confirmation_email(booking, hotel=None):
    """Send the authoritative branded receipt after verified payment.

    The payment service calls this only after its atomic reconciliation has
    committed.  A payment reference is the automatic-send idempotency key;
    staff may still intentionally send another copy from the receipt screen.
    """
    if not booking.guest.email:
        return
    from apps.bookings.serializers import ReceiptSerializer
    from apps.bookings.services.receipt_email import render_receipt_email
    from apps.notifications.models import EmailLog

    receipt = ReceiptSerializer().to_representation(booking)
    payment_reference = receipt.get("receipt_reference") or booking.booking_reference
    # Interrupted synchronous sends (deploy/timeout mid-request) leave rows in
    # PENDING/SENDING with no worker to ever resolve them. Resolve stale rows
    # to FAILED first, otherwise one interrupted attempt would silently block
    # this guest's automatic receipt forever.
    EmailLog.resolve_stale(
        EmailLog.objects.filter(booking_reference=booking.booking_reference)
    )
    if EmailLog.objects.filter(
        kind=EmailLog.Kind.RECEIPT,
        booking_reference=booking.booking_reference,
        payment_reference=payment_reference,
        created_by__isnull=True,
        status__in=[EmailLog.Status.PENDING, EmailLog.Status.SENDING,
                    EmailLog.Status.SENT],
    ).exists():
        return
    try:
        subject, text_body, html_body = render_receipt_email(receipt)
    except Exception as exc:  # payment remains successful; failure stays observable
        logger.exception("Automatic receipt render failed for %s", booking.booking_reference)
        EmailLog.objects.create(
            to_email=booking.guest.email,
            subject=f"Payment Receipt — {booking.booking_reference}",
            kind=EmailLog.Kind.RECEIPT,
            booking_reference=booking.booking_reference,
            payment_reference=payment_reference,
            booking_id=booking.id,
            attach_receipt_pdf=True,
            status=EmailLog.Status.FAILED,
            failure_stage=EmailLog.FailureStage.RENDER,
            error_class=exc.__class__.__name__,
            error_message="The automatic receipt could not be generated. No email was sent.",
            failed_at=timezone.now(),
        )
        return
    queue_email(
        subject, text_body, [booking.guest.email],
        html_message=html_body,
        kind=EmailLog.Kind.RECEIPT,
        booking_reference=booking.booking_reference,
        payment_reference=payment_reference,
        booking_id=booking.id,
        attach_receipt_pdf=True,
    )


# ---------------------------------------------------------------------------
# Payments → booking state (called from the payments service in one transaction)
# ---------------------------------------------------------------------------
def register_successful_payment(booking: Booking, amount, *, request=None):
    """Credit a verified payment and (re-)derive payment status + confirmation."""
    booking.amount_paid = booking.amount_paid + amount
    if booking.amount_paid >= booking.total_amount:
        booking.payment_status = Booking.PaymentStatus.PAID
    elif booking.amount_paid > 0:
        booking.payment_status = Booking.PaymentStatus.PARTIALLY_PAID
    update_fields = ["amount_paid", "payment_status", "updated_at"]

    if (
        booking.status == Booking.Status.PENDING
        and booking.amount_paid >= booking.required_payment
    ):
        # Deposit requirement satisfied → confirm and release the hold timer.
        booking.status = Booking.Status.CONFIRMED
        booking.expires_at = None
        update_fields += ["status", "expires_at"]
        notify_staff(
            type="BOOKING_CONFIRMED",
            title=f"Booking {booking.booking_reference} confirmed",
            message="Payment received; booking confirmed automatically.",
            link=staff_booking_link(booking),
        )
    booking.save(update_fields=update_fields)
    return booking


# ---------------------------------------------------------------------------
# Expiration
# ---------------------------------------------------------------------------
def expire_pending_booking(booking: Booking):
    """Single-object expiration, safe to call twice (idempotent) AND safe under
    concurrent confirmation.

    The status flip happens under a row lock with a re-check, so the
    interleaving "beat task reads PENDING → Paystack verification commits
    CONFIRMED → beat task writes EXPIRED" can never clobber a booking that a
    verified payment confirmed in the meantime. Payment verification takes the
    same row lock (payments service), so whichever transaction wins, the loser
    sees the new state and backs off.
    """
    if booking.status != Booking.Status.PENDING:
        return
    with transaction.atomic():
        locked = (
            Booking.objects.select_for_update()
            .select_related("guest", "guest__user")
            .get(pk=booking.pk)
        )
        # Re-check under the lock: a concurrent payment/confirmation may have
        # confirmed (or staff may have cancelled) this booking already — and a
        # payment may even have pushed expires_at away by clearing it.
        if not locked.is_expired_pending:
            booking.refresh_from_db(fields=["status", "expires_at"])
            return
        locked.status = Booking.Status.EXPIRED
        locked.save(update_fields=["status", "updated_at"])
        log_action(
            actor=None, action="BOOKING_EXPIRED", instance=locked,
            summary=f"Pending booking {locked.booking_reference} expired (unpaid hold released)",
        )
        if locked.guest.user_id:
            notify_users(
                [locked.guest.user],
                type="BOOKING_CANCELLED",
                title=f"Booking {locked.booking_reference} expired",
                message="Your reserved hold expired because payment was not completed in time.",
                link=guest_booking_link(locked),
            )
    booking.status = locked.status
    logger.info("Pending booking expired: %s", booking.booking_reference)


def expire_stale_pending_bookings(now=None):
    """Batch job used by Celery beat; also invoked lazily by hot paths."""
    now = now or timezone.now()
    stale = list(
        Booking.objects.filter(status=Booking.Status.PENDING, expires_at__lte=now).order_by("pk")[:500]
    )
    for booking in stale:
        expire_pending_booking(booking)
    return len(stale)


# Opportunistic fallback sweep. Celery beat remains the primary expiration
# driver; this simply guarantees the staff bookings screen can never show
# long-dead PENDING holds when the worker/beat services are down (e.g. a
# hosting plan where they are not running). Throttled through the shared
# cache so at most one sweep runs per interval across all web workers.
LAZY_EXPIRY_SWEEP_CACHE_KEY = "bookings:lazy-expiry-sweep"
LAZY_EXPIRY_SWEEP_INTERVAL = 60  # seconds


def maybe_expire_stale_pending_bookings():
    """Run the stale-pending sweep at most once per interval (cheap no-op otherwise)."""
    try:
        from django.core.cache import cache

        if not cache.add(LAZY_EXPIRY_SWEEP_CACHE_KEY, "1", LAZY_EXPIRY_SWEEP_INTERVAL):
            return 0
    except Exception:  # cache trouble must never break a bookings request
        return 0
    try:
        return expire_stale_pending_bookings()
    except Exception:
        logger.exception("Opportunistic pending-booking sweep failed")
        return 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def calculate_cancellation_policy(booking: Booking, *, now=None, settings_obj=None):
    """Return policy math without changing booking/payment/refund state.

    The calculated refund is an amount due for staff review; it is NOT an
    indication that any refund has been submitted or processed. Actual refund
    lifecycle lives on payments.Refund and Paystack webhooks.
    """
    from decimal import Decimal

    settings_obj = settings_obj or HotelSettings.get_settings()
    now = now or timezone.now()
    check_in_dt = combine_hotel_datetime(booking.check_in, settings_obj.check_in_time)
    deadline = check_in_dt - timedelta(hours=settings_obj.cancellation_deadline_hours)
    amount_paid = Decimal(booking.amount_paid or 0).quantize(Decimal("0.01"))
    fee_percent = Decimal(settings_obj.cancellation_fee_percent or 0).quantize(Decimal("0.01"))
    fee = (amount_paid * fee_percent / Decimal("100")).quantize(Decimal("0.01"))
    refund_amount = max(amount_paid - fee, Decimal("0.00"))
    return {
        "deadline": deadline,
        "within_free_cancellation_window": now <= deadline,
        "deadline_hours": settings_obj.cancellation_deadline_hours,
        "fee_percent": fee_percent,
        "amount_paid": amount_paid,
        "cancellation_fee": fee,
        "refund_amount": refund_amount,
    }


@transaction.atomic
def cancel_booking(
    booking: Booking, *, reason="", by_user=None, staff=False, request=None,
    send_guest_email=True, cancellation_request=None,
):
    """Cancel a booking safely without pretending refunds are complete.

    Guests no longer cancel directly. Public cancellation requests are captured
    through the Contact page and reviewed by staff. This routine is therefore
    for staff/system use and only updates BOOKING state. Payment/refund state is
    reconciled separately by the payments service and Paystack webhooks.
    """
    if not staff:
        raise CancellationNotAllowedError(
            "Online self-cancellation is no longer available. Please submit a Cancellation / Refund Request from the Contact page."
        )

    booking = Booking.objects.select_for_update().select_related("guest", "guest__user", "room_type").get(pk=booking.pk)
    booking = refresh_expired_pending(booking)
    if booking.status == Booking.Status.CANCELLED:
        return booking  # idempotent: cancelling twice is a no-op
    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
        raise BookingStateError(
            f"A booking with status {booking.get_status_display()} cannot be cancelled."
        )

    policy = calculate_cancellation_policy(booking)
    refund_due = policy["refund_amount"]
    previous_status = booking.status

    booking.status = Booking.Status.CANCELLED
    booking.cancelled_at = timezone.now()
    booking.cancellation_reason = reason or "Cancelled by staff"
    booking.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])

    log_action(
        actor=by_user,
        action="BOOKING_CANCELLED",
        instance=booking,
        metadata={
            "reference": booking.booking_reference,
            "by": "staff",
            "previous_status": previous_status,
            "new_status": booking.status,
            "calculated_refund_due": str(refund_due),
            "calculated_cancellation_fee": str(policy["cancellation_fee"]),
            "cancellation_request": getattr(cancellation_request, "cancellation_reference", ""),
        },
        request=request,
        summary=f"Booking {booking.booking_reference} cancelled (refund due for review: {refund_due})",
    )
    notify_staff(
        type="BOOKING_CANCELLED",
        title=f"Booking {booking.booking_reference} cancelled",
        message=(
            "Cancelled by staff."
            + (f" Calculated refund due for review: {booking.currency} {refund_due}." if refund_due > 0 else "")
        ),
        link=staff_booking_link(booking),
    )
    if booking.guest.user_id:
        notify_users(
            [booking.guest.user],
            type="BOOKING_CANCELLED",
            title=f"Booking {booking.booking_reference} cancelled",
            message="Your booking has been cancelled. Refund status is tracked separately.",
            link=guest_booking_link(booking),
        )
    if send_guest_email and booking.guest.email:
        from apps.core.email_design import render_notice_email
        from apps.core.formatting import format_date

        hotel = HotelSettings.get_settings()
        text_body, html_body = render_notice_email(
            category="Booking update",
            title="Your booking has been cancelled",
            greeting=f"Hello {booking.guest.first_name},",
            paragraphs=[
                f"Booking {booking.booking_reference} at {hotel.hotel_name} has been cancelled.",
                "Refunds, if applicable, are reviewed and processed separately. "
                "You will receive a separate update only when a refund is submitted or confirmed.",
            ],
            details=[
                {"label": "Booking reference", "value": booking.booking_reference},
                {"label": "Room", "value": f"{booking.number_of_rooms} × {booking.room_type.name}"},
                {"label": "Check-in", "value": format_date(booking.check_in)},
                {"label": "Check-out", "value": format_date(booking.check_out)},
                {"label": "Status", "value": "Cancelled"},
            ],
            footnote=(
                f"If you have any questions about this cancellation, please contact "
                f"{hotel.phone or hotel.email}."
            ),
            preheader=f"Booking {booking.booking_reference} has been cancelled.",
        )
        queue_email(
            kind="CANCELLATION",
            booking_reference=booking.booking_reference,
            subject=f"Booking cancelled: {booking.booking_reference} — {hotel.hotel_name}",
            message=text_body,
            html_message=html_body,
            recipients=[booking.guest.email],
        )
    logger.info("Booking cancelled: %s (staff=%s, calculated_refund=%s)", booking.booking_reference, staff, refund_due)
    return booking


# ---------------------------------------------------------------------------
# Front-desk operations
# ---------------------------------------------------------------------------
def can_check_in_today(booking, *, today=None, settings_obj=None):
    """Whether the front desk may check this booking in right now.

    Returns ``(allowed, reason)``. The rules, in the order staff see them:

    * only CONFIRMED bookings (CHECKED_IN is treated as an idempotent yes);
    * never before the booked check-in date when the hotel restricts early
      arrivals (HotelSettings.restrict_check_in_to_booked_date, default on);
    * LATE ARRIVALS ARE ALLOWED: a guest who missed the first night may still
      check in on any later day of the booked range;
    * never once the booked check-out date has passed.
    """
    settings_obj = settings_obj or HotelSettings.get_settings()
    today = today or hotel_today()

    if booking.status == Booking.Status.CHECKED_IN:
        return True, ""
    if booking.status != Booking.Status.CONFIRMED:
        return False, "Only CONFIRMED bookings can be checked in."
    if today > booking.check_out:
        return False, (
            f"This booking's stay ended on {booking.check_out}; it can no longer "
            "be checked in."
        )
    if today < booking.check_in and settings_obj.restrict_check_in_to_booked_date:
        return False, f"Check-in is scheduled for {booking.check_in}."
    return True, ""


@transaction.atomic
def check_in_booking(booking: Booking, *, staff_user, request=None):
    booking = refresh_expired_pending(booking)
    if booking.status == Booking.Status.CHECKED_IN:
        return booking  # idempotent retry
    allowed, reason = can_check_in_today(booking)
    if not allowed:
        raise BookingStateError(reason)

    assignments = list(booking.room_assignments.select_related("room").select_for_update())
    if len(assignments) < booking.number_of_rooms:
        # Fallback safeguard (should not happen): allocate the missing rooms now.
        missing = booking.number_of_rooms - len(assignments)
        free = list(
            availability.available_rooms_queryset(
                room_type=booking.room_type,
                check_in=booking.check_in,
                check_out=booking.check_out,
                for_update=True,
                exclude_booking_id=booking.pk,
            )[:missing]
        )
        if len(free) < missing:
            raise RoomUnavailableError("Not enough rooms available to complete check-in.")
        BookingRoom.objects.bulk_create(
            [BookingRoom(booking=booking, room=r, check_in=booking.check_in, check_out=booking.check_out) for r in free]
        )
        assignments = list(booking.room_assignments.select_related("room"))

    for assignment in assignments:
        room = assignment.room
        if room.status in (Room.Status.MAINTENANCE, Room.Status.OUT_OF_SERVICE):
            raise RoomUnavailableError(
                f"Room {room.room_number} is {room.get_status_display().lower()} and cannot be used."
            )
        room.status = Room.Status.OCCUPIED
        room.save(update_fields=["status", "updated_at"])

    booking.status = Booking.Status.CHECKED_IN
    booking.checked_in_at = timezone.now()
    booking.save(update_fields=["status", "checked_in_at", "updated_at"])

    # Reservation state remains authoritative for compatibility, while the
    # additive Stay preserves the operational occupancy history needed by
    # folios, nightly revenue, housekeeping, and room moves.
    from apps.stays.services import ensure_stay_for_check_in

    ensure_stay_for_check_in(booking=booking, assignments=assignments, actor=staff_user)

    log_action(
        actor=staff_user, action="CHECK_IN", instance=booking,
        metadata={"reference": booking.booking_reference,
                  "rooms": [a.room.room_number for a in assignments]},
        request=request,
        summary=f"Guest checked in: {booking.booking_reference}",
    )
    notify_staff(
        type="CHECK_IN",
        title=f"Checked in: {booking.booking_reference}",
        message=f"{booking.guest.full_name} checked in ({booking.room_type.name}).",
        link=staff_booking_link(booking),
    )
    logger.info("Check-in: %s by staff %s", booking.booking_reference, staff_user.id)
    return booking


def _release_rooms_for_checkout(booking):
    """Release every assigned room back to inventory and flag housekeeping."""
    assignments = list(booking.room_assignments.select_related("room").select_for_update())
    for assignment in assignments:
        room = assignment.room
        room.status = Room.Status.AVAILABLE
        room.housekeeping_status = Room.HousekeepingStatus.DIRTY
        room.save(update_fields=["status", "housekeeping_status", "updated_at"])
    return assignments


def _perform_checkout(booking: Booking, *, actor=None, automatic=False,
                      allow_balance_due=False, request=None):
    """Single authoritative checkout routine shared by the manual staff action
    and the automatic (scheduled) checkout task. Legacy booking balance
    projections remain intact; the additive Stay bridge posts only any elapsed,
    idempotent nightly accommodation revenue before operational closure."""
    if booking.status == Booking.Status.CHECKED_OUT:
        return booking  # idempotent retry
    if booking.status != Booking.Status.CHECKED_IN:
        raise BookingStateError("Only checked-in bookings can be checked out.")
    if booking.amount_due > 0 and not allow_balance_due:
        raise OutstandingBalanceError(
            f"Outstanding balance of {booking.currency} {booking.amount_due} must be settled before checkout."
        )

    assignments = _release_rooms_for_checkout(booking)

    booking.status = Booking.Status.CHECKED_OUT
    booking.checked_out_at = timezone.now()
    booking.save(update_fields=["status", "checked_out_at", "updated_at"])

    # Bridge any pre-existing in-house booking lazily, then append immutable
    # Stay/room-release evidence in the same checkout transaction.
    from apps.stays.services import close_stay_for_checkout

    close_stay_for_checkout(
        booking=booking,
        assignments=assignments,
        actor=actor,
        automatic=automatic,
    )

    action = "AUTO_CHECK_OUT" if automatic else "CHECK_OUT"
    summary = (
        f"Guest automatically checked out: {booking.booking_reference}"
        if automatic else f"Guest checked out: {booking.booking_reference}"
    )
    log_action(
        actor=actor, action=action, instance=booking,
        metadata={"reference": booking.booking_reference,
                  "rooms": [a.room.room_number for a in assignments],
                  "balance_outstanding": str(booking.amount_due),
                  "automatic": automatic},
        request=request,
        summary=summary,
    )
    rooms_label = ", ".join(a.room.room_number for a in assignments) or booking.room_type.name
    if automatic:
        balance_note = (
            f" Outstanding balance: {booking.currency} {booking.amount_due}."
            if booking.amount_due > 0 else ""
        )
        notify_staff(
            type="CHECKOUT_AUTO",
            title=f"Auto checkout: {booking.booking_reference}",
            message=(
                f"{booking.guest.full_name} (room {rooms_label}) was automatically "
                f"checked out at {timezone.localtime(booking.checked_out_at):%H:%M}."
                + balance_note
            ),
            link=staff_booking_link(booking),
        )
    else:
        notify_staff(
            type="CHECK_OUT",
            title=f"Checked out: {booking.booking_reference}",
            message=f"{booking.guest.full_name} checked out (room {rooms_label}).",
            link=staff_booking_link(booking),
        )
    logger.info(
        "Check-out%s: %s by %s", " (auto)" if automatic else "",
        booking.booking_reference, actor.id if actor else "system",
    )
    # Post-stay review invitation — queued after the checkout transaction
    # commits; a delivery problem can never affect the checkout itself.
    from apps.reviews.services import send_review_invitation

    send_review_invitation(booking)
    return booking


@transaction.atomic
def check_out_booking(booking: Booking, *, staff_user, allow_balance_due=False, request=None):
    return _perform_checkout(
        booking, actor=staff_user, automatic=False,
        allow_balance_due=allow_balance_due, request=request,
    )


# ---------------------------------------------------------------------------
# Automatic checkout + 30-minute warning (Celery; hotel timezone aware)
# ---------------------------------------------------------------------------
def _scheduled_checkout_datetime(booking, settings_obj):
    """checkout date + configured checkout time in the hotel's timezone."""
    return combine_hotel_datetime(booking.check_out, settings_obj.check_out_time)


def auto_checkout_due_bookings(now=None):
    """Check out every in-house booking whose scheduled checkout time has
    passed. Idempotent and safe against concurrent workers: each booking is
    re-locked and re-checked inside its own transaction. Balances are
    preserved (allow_balance_due=True) — no financial record is altered."""
    now = now or timezone.now()
    settings_obj = HotelSettings.get_settings()
    candidate_ids = list(
        Booking.objects.filter(
            status=Booking.Status.CHECKED_IN,
            check_out__lte=timezone.localdate(now),
        ).values_list("pk", flat=True)
    )
    processed = 0
    for pk in candidate_ids:
        with transaction.atomic():
            booking = (
                Booking.objects.select_for_update()
                .select_related("guest", "room_type")
                .get(pk=pk)
            )
            if booking.status != Booking.Status.CHECKED_IN:
                continue  # another worker already handled it
            if now < _scheduled_checkout_datetime(booking, settings_obj):
                continue  # not due yet — never check out early
            _perform_checkout(booking, actor=None, automatic=True, allow_balance_due=True)
            processed += 1
    return processed


def send_checkout_due_soon_notifications(now=None, warning_minutes=CHECKOUT_WARNING_MINUTES):
    """Notify staff once per stay, ~30 minutes before scheduled checkout.

    Deduplication: a CHECKOUT_DUE_SOON notification whose link targets this
    booking and that was created after check-in means the warning was already
    sent — frequent Celery runs never re-notify the same stay."""
    from apps.notifications.models import Notification

    now = now or timezone.now()
    settings_obj = HotelSettings.get_settings()
    window_end = now + timedelta(minutes=warning_minutes)
    sent = 0
    candidates = (
        Booking.objects.filter(
            status=Booking.Status.CHECKED_IN,
            check_out__lte=timezone.localdate(window_end),
        )
        .select_related("guest", "room_type")
        .prefetch_related("room_assignments__room")
    )
    for booking in candidates:
        due_at = _scheduled_checkout_datetime(booking, settings_obj)
        if not (now <= due_at <= window_end):
            continue  # not inside the warning window (past-due handled by auto checkout)
        link = staff_booking_link(booking)
        already = Notification.objects.filter(
            type="CHECKOUT_DUE_SOON", link=link,
            created_at__gte=booking.checked_in_at or booking.created_at,
        ).exists()
        if already:
            continue
        rooms_label = ", ".join(
            a.room.room_number for a in booking.room_assignments.all()
        ) or booking.room_type.name
        notify_staff(
            type="CHECKOUT_DUE_SOON",
            title=f"Checkout due soon: {booking.booking_reference}",
            message=(
                f"{booking.guest.full_name} (room {rooms_label}) is scheduled to check out "
                f"at {timezone.localtime(due_at):%H:%M} today. Prepare for checkout, "
                f"contact the guest, or extend the stay."
            ),
            link=link,
        )
        sent += 1
    return sent


@transaction.atomic
def mark_no_show(booking: Booking, *, staff_user, request=None):
    if booking.status != Booking.Status.CONFIRMED:
        raise BookingStateError("Only CONFIRMED bookings can be marked as no-show.")
    if hotel_today() <= booking.check_in:
        raise BookingStateError("A booking can only be marked no-show after its check-in date.")
    booking.status = Booking.Status.NO_SHOW
    booking.save(update_fields=["status", "updated_at"])
    for assignment in booking.room_assignments.select_related("room"):
        pass  # assignment rows stop blocking automatically (status is NO_SHOW)
    log_action(
        actor=staff_user, action="NO_SHOW", instance=booking,
        metadata={"reference": booking.booking_reference}, request=request,
        summary=f"Booking {booking.booking_reference} marked as no-show",
    )
    logger.info("No-show: %s", booking.booking_reference)
    return booking


@transaction.atomic
def confirm_manual_booking(booking: Booking, *, staff_user, request=None):
    """Staff confirms a booking without an online payment (pay at hotel)."""
    booking = refresh_expired_pending(booking)
    if booking.status == Booking.Status.CONFIRMED:
        return booking
    if booking.status != Booking.Status.PENDING:
        raise BookingStateError("Only PENDING bookings can be confirmed.")
    booking.status = Booking.Status.CONFIRMED
    booking.expires_at = None
    booking.save(update_fields=["status", "expires_at", "updated_at"])
    log_action(
        actor=staff_user, action="BOOKING_CONFIRMED", instance=booking,
        metadata={"reference": booking.booking_reference, "method": "manual (staff)"},
        request=request,
        summary=f"Booking {booking.booking_reference} confirmed by staff",
    )
    if booking.guest.user_id:
        notify_users([booking.guest.user], type="BOOKING_CONFIRMED",
                     title=f"Booking {booking.booking_reference} confirmed",
                     message="Your booking has been confirmed by the hotel.",
                     link=guest_booking_link(booking))
    _send_confirmation_email(booking)
    logger.info("Manual confirmation: %s by staff %s", booking.booking_reference, staff_user.id)
    return booking


@transaction.atomic
def assign_room(assignment: BookingRoom, *, new_room: Room, staff_user, request=None):
    """Re-point one assignment at a different physical room (validated).

    Serializes on the room-type row (same lock as create_booking) so two staff
    members assigning the same room at nearly the same time cannot both pass
    the overlap re-check below."""
    booking = assignment.booking
    if new_room.room_type_id != booking.room_type_id:
        raise BookingStateError("The room must match the booking's room type.")
    RoomType.objects.select_for_update().get(pk=booking.room_type_id)
    new_room.refresh_from_db()
    if not new_room.is_active or new_room.status in availability.OPERATIONALLY_BLOCKED:
        raise RoomUnavailableError(f"Room {new_room.room_number} is not in service.")
    conflicts = availability.blocked_room_ids(
        room_type_id=booking.room_type_id,
        check_in=assignment.check_in,
        check_out=assignment.check_out,
        exclude_booking_id=booking.pk,
    )
    if new_room.pk in set(conflicts):
        raise RoomUnavailableError(
            f"Room {new_room.room_number} is already reserved for overlapping dates."
        )
    old_number = assignment.room.room_number
    assignment.room = new_room
    assignment.save(update_fields=["room", "updated_at"])
    log_action(
        actor=staff_user, action="ROOM_ASSIGNED", instance=booking,
        changes={"room": [old_number, new_room.room_number]},
        metadata={"reference": booking.booking_reference},
        request=request,
        summary=f"Booking {booking.booking_reference}: room {old_number} → {new_room.room_number}",
    )
    logger.info("Assignment changed on %s: %s -> %s", booking.booking_reference, old_number, new_room.room_number)
    return assignment


@transaction.atomic
def modify_booking(booking: Booking, *, staff_user, data: dict, request=None):
    """Staff edit. Changing dates/rooms re-runs availability + repricing
    inside the room-type lock so integrity is preserved."""
    booking = refresh_expired_pending(booking)
    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
        raise BookingStateError(
            f"A {booking.get_status_display().lower()} booking cannot be modified."
        )

    changes = {}
    room_type = RoomType.objects.select_for_update().get(pk=booking.room_type_id)

    # Simple text fields first.
    for field_name in ("special_requests", "internal_notes"):
        if field_name in data and data[field_name] is not None:
            old = getattr(booking, field_name)
            if old != data[field_name]:
                setattr(booking, field_name, data[field_name])
                changes[field_name] = [old or "", data[field_name]]

    stays = {"check_in", "check_out", "number_of_rooms", "adults", "children"}
    if stays & data.keys():
        new_check_in = data.get("check_in", booking.check_in)
        new_check_out = data.get("check_out", booking.check_out)
        new_rooms = int(data.get("number_of_rooms", booking.number_of_rooms))
        new_adults = int(data.get("adults", booking.adults))
        new_children = int(data.get("children", booking.children))

        quote = calculate_quote(
            room_type=room_type, check_in=new_check_in, check_out=new_check_out,
            rooms=new_rooms, adults=new_adults, children=new_children, for_staff=True,
        )
        current_room_ids = list(
            booking.room_assignments.values_list("room_id", flat=True)
        )
        dates_changed = (new_check_in, new_check_out) != (booking.check_in, booking.check_out)
        if dates_changed and current_room_ids:
            # Kept rooms must be free of OTHER bookings for the NEW dates.
            conflicts = set(
                availability.blocked_room_ids(
                    room_type_id=booking.room_type_id,
                    check_in=new_check_in,
                    check_out=new_check_out,
                    exclude_booking_id=booking.pk,
                )
            )
            clashing = conflicts & set(current_room_ids)
            if clashing:
                numbers = ", ".join(
                    Room.objects.filter(pk__in=clashing).values_list("room_number", flat=True)
                )
                raise RoomUnavailableError(
                    f"Room(s) {numbers} are already reserved by another booking for the new dates."
                )
        if new_rooms <= len(current_room_ids):
            keep_ids = current_room_ids[:new_rooms]
            # Freed rooms (when shrinking) must still be free of OTHER bookings.
            booking.room_assignments.exclude(room_id__in=keep_ids).delete()
            new_room_ids = keep_ids
        else:
            extra_needed = new_rooms - len(current_room_ids)
            free = list(
                availability.available_rooms_queryset(
                    room_type=room_type, check_in=new_check_in, check_out=new_check_out,
                    for_update=True, exclude_booking_id=booking.pk,
                ).exclude(pk__in=current_room_ids)[:extra_needed]
            )
            if len(free) < extra_needed:
                raise RoomUnavailableError(
                    "Not enough rooms of this type are available for the new stay."
                )
            BookingRoom.objects.bulk_create(
                [BookingRoom(booking=booking, room=r, check_in=new_check_in, check_out=new_check_out) for r in free]
            )
            new_room_ids = current_room_ids + [r.pk for r in free]
        booking.room_assignments.filter(room_id__in=new_room_ids).update(
            check_in=new_check_in, check_out=new_check_out
        )

        snapshot = {
            "check_in": (booking.check_in, new_check_in),
            "check_out": (booking.check_out, new_check_out),
            "number_of_rooms": (booking.number_of_rooms, new_rooms),
            "adults": (booking.adults, new_adults),
            "children": (booking.children, new_children),
            "total_amount": (booking.total_amount, quote.total),
        }
        for key, (old, new) in snapshot.items():
            if str(old) != str(new):
                changes[key] = [str(old), str(new)]

        booking.check_in, booking.check_out = new_check_in, new_check_out
        booking.number_of_rooms, booking.adults, booking.children = new_rooms, new_adults, new_children
        booking.price_per_night = quote.price_per_night
        booking.subtotal = quote.subtotal
        booking.discount_amount = quote.discount
        booking.extra_guest_fee_amount = quote.extra_guest_fee
        booking.tax_amount = quote.tax
        booking.fee_amount = quote.service_fee
        booking.total_amount = quote.total
        booking.required_payment = quote.required_payment
        booking.offer = quote.offer
        booking.payment_status = (
            Booking.PaymentStatus.PAID
            if booking.amount_paid >= booking.total_amount
            else (Booking.PaymentStatus.PARTIALLY_PAID if booking.amount_paid > 0 else Booking.PaymentStatus.UNPAID)
        )

    booking.save()
    if changes:
        log_action(
            actor=staff_user, action="BOOKING_MODIFIED", instance=booking, changes=changes,
            metadata={"reference": booking.booking_reference}, request=request,
            summary=f"Booking {booking.booking_reference} modified by staff",
        )
        notify_staff(
            type="BOOKING_MODIFIED",
            title=f"Booking {booking.booking_reference} modified",
            message="; ".join(f"{k}: {v[0]} → {v[1]}" for k, v in changes.items()),
            link=staff_booking_link(booking),
        )
    logger.info("Booking modified: %s changes=%s", booking.booking_reference, list(changes))
    return booking


# ---------------------------------------------------------------------------
# Occupancy calendar (staff)
# ---------------------------------------------------------------------------
# Bookings that never became a stay. Excluded from any "how many bookings does
# this guest have" figure shown to staff.
UNCOUNTED_BOOKING_STATUSES = (
    Booking.Status.CANCELLED,
    Booking.Status.EXPIRED,
)

# Statuses that represent REAL occupancy of a physical room. Cancelled, expired
# and no-show bookings never occupy a room; PENDING holds are excluded too —
# an unpaid hold is inventory pressure, not an occupied room. This mirrors the
# availability engine's BLOCKING_STATUSES minus the transient pending hold.
OCCUPANCY_STATUSES = (
    Booking.Status.CONFIRMED,
    Booking.Status.CHECKED_IN,
    Booking.Status.CHECKED_OUT,
)


def occupancy_calendar(*, year, month, include_pending=False):
    """Room-night occupancy for one month, grouped by date.

    Returns ``{"days": {"YYYY-MM-DD": [entry, ...]}, ...}`` where every entry is
    ONE physical room assigned to one booking — so a 5-room booking contributes
    five entries to every night of its stay.

    The data comes from the authoritative ``BookingRoom`` assignment rows (not
    payments, not the booking header), so multi-room bookings are represented
    exactly as they were assigned.

    Efficiency: ONE query with select_related over the whole month, then an
    in-memory fan-out across each assignment's nights. No per-day queries and
    no N+1 on room/guest/booking.
    """
    from calendar import monthrange
    from datetime import date as _date

    year, month = int(year), int(month)
    first_day = _date(year, month, 1)
    last_day = _date(year, month, monthrange(year, month)[1])
    # A stay occupies [check_in, check_out) — the checkout day is NOT occupied.
    day_after_last = last_day + timedelta(days=1)

    statuses = list(OCCUPANCY_STATUSES)
    if include_pending:
        statuses.append(Booking.Status.PENDING)

    assignments = (
        BookingRoom.objects.filter(
            booking__status__in=statuses,
            check_in__lt=day_after_last,
            check_out__gt=first_day,
        )
        .select_related("room", "room__room_type", "booking", "booking__guest")
        .order_by("room__room_number")
    )

    days: dict[str, list] = {}
    now = timezone.now()
    for assignment in assignments:
        booking = assignment.booking
        # An expired, unpaid hold must never look like occupancy.
        if (
            booking.status == Booking.Status.PENDING
            and booking.expires_at is not None
            and booking.expires_at <= now
        ):
            continue
        entry = {
            "booking_id": booking.pk,
            "booking_reference": booking.booking_reference,
            "room_number": assignment.room.room_number,
            "room_type_name": assignment.room.room_type.name,
            "guest_name": booking.guest.full_name,
            "status": booking.status,
            "check_in": assignment.check_in.isoformat(),
            "check_out": assignment.check_out.isoformat(),
        }
        night = max(assignment.check_in, first_day)
        stay_end = min(assignment.check_out, day_after_last)
        while night < stay_end:
            days.setdefault(night.isoformat(), []).append(entry)
            night += timedelta(days=1)

    for key in days:
        days[key].sort(key=lambda e: e["room_number"])

    return {
        "year": year,
        "month": month,
        "start_date": first_day.isoformat(),
        "end_date": last_day.isoformat(),
        "days": days,
    }


# ---------------------------------------------------------------------------
# Missed / no-show bookings
# ---------------------------------------------------------------------------
def missed_bookings_queryset(*, now=None, settings_obj=None):
    """Bookings the guest booked but never arrived for.

    A booking qualifies only when the backend can establish ALL of:

    * it is still CONFIRMED (paid/held) or already flagged NO_SHOW — a
      CHECKED_IN, CHECKED_OUT, CANCELLED or EXPIRED booking never qualifies;
    * the arrival deadline has passed. That deadline is the hotel's check-out
      time on the check-in date: the guest had the whole arrival day, and only
      once the stay's first day is over is a no-show established.

    A legitimately checked-in guest can never appear here because CHECKED_IN /
    CHECKED_OUT are excluded outright.
    """
    settings_obj = settings_obj or HotelSettings.get_settings()
    now = now or timezone.now()
    today = hotel_today()

    qs = (
        Booking.objects.select_related("guest", "room_type")
        .prefetch_related(
            Prefetch("room_assignments",
                     queryset=BookingRoom.objects.select_related("room"))
        )
        .filter(status__in=(Booking.Status.CONFIRMED, Booking.Status.NO_SHOW))
    )

    # Deadline: end of the check-in day (hotel check-out time on that date).
    deadline_passed = Q(check_in__lt=today)
    if now.time() >= settings_obj.check_out_time:
        deadline_passed |= Q(check_in=today)
    return qs.filter(deadline_passed).filter(checked_in_at__isnull=True).order_by("check_in")


def late_arrival_bookings_queryset(*, today=None):
    """Confirmed multi-night bookings that can STILL be checked in late.

    The guest missed the first night but the stay is not over, so the front
    desk must be able to welcome them. Qualifying bookings have:

    * a check-in date that has passed;
    * a check-out date that has not (arrival is still inside the booked range);
    * status CONFIRMED (never cancelled/expired/no-show/checked-out);
    * no check-in recorded yet;
    * more than one night.

    This deliberately overlaps ``missed_bookings_queryset``: the same booking
    is both "did not arrive on time" and "can still be rescued", and the two
    desk tables answer different questions.
    """
    today = today or hotel_today()
    return (
        Booking.objects.select_related("guest", "room_type")
        .prefetch_related(
            Prefetch("room_assignments",
                     queryset=BookingRoom.objects.select_related("room"))
        )
        .filter(
            status=Booking.Status.CONFIRMED,
            checked_in_at__isnull=True,
            check_in__lt=today,
            check_out__gte=today,
        )
        .exclude(check_out=F("check_in") + timedelta(days=1))
        .order_by("check_in")
    )


@transaction.atomic
def reschedule_booking(booking: Booking, *, check_in, check_out, staff_user, request=None):
    """Move a no-show/confirmed booking to new dates, re-checking availability.

    Preserves the SAME booking (and therefore its payment history): nothing new
    is created. Physical rooms are re-assigned through the authoritative
    availability engine, so overbooking is impossible and multi-room bookings
    keep their full room count.

    Money is never silently changed: the stay is re-priced and the difference is
    reported to the caller, but ``amount_paid`` and every historical payment row
    are left untouched.
    """
    booking = (
        Booking.objects.select_for_update()
        .select_related("guest", "room_type")
        .get(pk=booking.pk)
    )
    if booking.status not in (Booking.Status.CONFIRMED, Booking.Status.NO_SHOW):
        raise BookingStateError(
            f"A booking with status {booking.get_status_display()} cannot be rescheduled."
        )

    settings_obj = HotelSettings.get_settings()
    # Staff rules: a reschedule may legitimately start today.
    nights = pricing.validate_stay_dates(
        check_in, check_out, for_staff=True, settings_obj=settings_obj
    )
    if check_in == booking.check_in and check_out == booking.check_out:
        raise InvalidDatesError("The new dates are the same as the current booking dates.")

    previous = {
        "check_in": booking.check_in,
        "check_out": booking.check_out,
        "status": booking.status,
        "total_amount": booking.total_amount,
        "rooms": [a.room.room_number for a in booking.room_assignments.select_related("room")],
    }
    rooms_needed = booking.number_of_rooms

    # Lock the room type, then re-check inventory for the NEW window while
    # ignoring this booking's own current assignments.
    RoomType.objects.select_for_update().get(pk=booking.room_type_id)
    free_rooms = list(
        availability.available_rooms_queryset(
            room_type=booking.room_type,
            check_in=check_in,
            check_out=check_out,
            for_update=True,
            exclude_booking_id=booking.pk,
        )[:rooms_needed]
    )
    if len(free_rooms) < rooms_needed:
        raise RoomUnavailableError(
            f"Only {len(free_rooms)} room(s) of this type are available for the new dates; "
            f"{rooms_needed} are required."
        )

    # Re-price the stay on the backend. The guest discount/offer decision runs
    # through the SAME authoritative pricing path as a new booking.
    quote = pricing.calculate_quote(
        room_type=booking.room_type, check_in=check_in, check_out=check_out,
        rooms=rooms_needed, adults=booking.adults, children=booking.children,
        settings_obj=settings_obj, for_staff=True, guest=booking.guest,
    )

    booking.room_assignments.all().delete()
    BookingRoom.objects.bulk_create([
        BookingRoom(booking=booking, room=room, check_in=check_in, check_out=check_out)
        for room in free_rooms
    ])

    booking.check_in = check_in
    booking.check_out = check_out
    booking.price_per_night = quote.price_per_night
    booking.subtotal = quote.subtotal
    booking.discount_amount = quote.discount
    booking.extra_guest_fee_amount = quote.extra_guest_fee
    booking.tax_amount = quote.tax
    booking.fee_amount = quote.service_fee
    booking.total_amount = quote.total
    # A rescheduled no-show becomes a live reservation again.
    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=[
        "check_in", "check_out", "price_per_night", "subtotal", "discount_amount",
        "extra_guest_fee_amount", "tax_amount", "fee_amount", "total_amount",
        "status", "updated_at",
    ])
    booking.refresh_from_db()

    balance_due = booking.amount_due
    overpaid = max(
        Decimal(booking.amount_paid or 0) - Decimal(booking.total_amount or 0),
        Decimal("0.00"),
    )

    log_action(
        actor=staff_user,
        action="BOOKING_RESCHEDULED",
        instance=booking,
        changes={
            "check_in": [str(previous["check_in"]), str(booking.check_in)],
            "check_out": [str(previous["check_out"]), str(booking.check_out)],
            "status": [previous["status"], booking.status],
            "total_amount": [str(previous["total_amount"]), str(booking.total_amount)],
        },
        metadata={
            "reference": booking.booking_reference,
            "previous_rooms": previous["rooms"],
            "new_rooms": [r.room_number for r in free_rooms],
            "balance_due": str(balance_due),
            "overpaid_amount": str(overpaid),
        },
        request=request,
        summary=(
            f"Booking {booking.booking_reference} rescheduled "
            f"{previous['check_in']}→{previous['check_out']} to {check_in}→{check_out}"
        ),
    )
    notify_staff(
        type="BOOKING_UPDATED",
        title=f"Booking {booking.booking_reference} rescheduled",
        message=(
            f"{booking.guest.full_name} moved to {check_in} → {check_out} "
            f"({nights} night(s)), rooms "
            f"{', '.join(r.room_number for r in free_rooms)}."
        ),
        link=staff_booking_link(booking),
    )
    if booking.guest.user_id:
        notify_users(
            [booking.guest.user], type="BOOKING_UPDATED",
            title=f"Booking {booking.booking_reference} rescheduled",
            message=f"Your stay has been moved to {check_in} → {check_out}.",
            link=guest_booking_link(booking),
        )

    booking.reschedule_result = {
        "previous_check_in": previous["check_in"].isoformat(),
        "previous_check_out": previous["check_out"].isoformat(),
        "previous_rooms": previous["rooms"],
        "new_rooms": [r.room_number for r in free_rooms],
        "balance_due": str(balance_due),
        "overpaid_amount": str(overpaid),
    }
    logger.info(
        "Booking rescheduled: %s %s→%s by staff %s",
        booking.booking_reference, check_in, check_out, staff_user.id,
    )
    return booking
