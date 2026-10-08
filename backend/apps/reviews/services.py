# apps/reviews/services.py
"""Review business logic — verification, eligibility, creation, invitations.

Security model
--------------
A requester proves they are the genuine guest of a booking by ANY of:

* presenting the booking reference together with the guest's email address
  (the pair a guest holds from their confirmation email);
* presenting the booking-scoped ``X-Guest-Access-Token`` issued at checkout;
* being signed in as the account that owns the booking.

Reference-only knowledge is NEVER enough (IDOR defence), and every failure —
unknown reference, wrong email, someone else's booking — collapses into the
same generic "no eligible stay" NotFound so references cannot be probed.
"""
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.audit.services import log_action
from apps.bookings.access import GUEST_TOKEN_HEADER
from apps.bookings.models import Booking
from apps.core.email_assets import logo_context
from apps.core.emails import queue_email
from apps.core.exceptions import JOneAPIError
from apps.core.formatting import format_date
from apps.hotel.models import HotelSettings
from apps.notifications.services import notify_staff

from .models import Review

logger = logging.getLogger("apps")

REVIEW_PAGE_LINK = "/review.html"
STAFF_REVIEWS_LINK = "/dashboard/reviews.html"

NOT_ELIGIBLE_MESSAGE = (
    "We couldn't find an eligible stay for those details. Reviews can be "
    "submitted once you have checked in, using the booking reference and the "
    "email on the booking."
)

# A stay may be reviewed from check-in onward (during the stay) and continues
# to be reviewable after checkout.
REVIEWABLE_STATUSES = (
    Booking.Status.CHECKED_IN,
    Booking.Status.CHECKED_OUT,
)


class AlreadyReviewedError(JOneAPIError):
    status_code = 409
    default_detail = "Thank you — this stay has already been reviewed."
    default_code = "ALREADY_REVIEWED"


class StayNotCompletedError(JOneAPIError):
    status_code = 409
    default_detail = "Reviews can be submitted once you have checked in."
    default_code = "STAY_NOT_STARTED"


def _requester_owns_booking(request, booking, email):
    """True only when the requester genuinely belongs to this booking."""
    supplied_email = (email or "").strip().lower()
    if supplied_email and booking.guest.email.strip().lower() == supplied_email:
        return True
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and booking.guest.user_id == user.id:
        return True
    token = request.headers.get(GUEST_TOKEN_HEADER, "") if request is not None else ""
    if token and booking.guest_token_matches(token):
        return True
    return False


def get_verified_booking(*, booking_reference, email, request):
    """Resolve + authorize a booking for the review flow, or raise NotFound.

    Failures never reveal whether the reference exists.
    """
    booking = (
        Booking.objects.select_related("guest", "room_type")
        .filter(booking_reference__iexact=(booking_reference or "").strip())
        .first()
    )
    if booking is None or not _requester_owns_booking(request, booking, email):
        raise NotFound(NOT_ELIGIBLE_MESSAGE)
    return booking


def check_eligibility(booking):
    """Raise the precise (safe) domain error when the stay cannot be reviewed.

    A guest becomes eligible to review once they have CHECKED IN, and remains
    eligible after CHECKED OUT.
    """
    if hasattr(booking, "review"):
        raise AlreadyReviewedError()
    if booking.status not in REVIEWABLE_STATUSES:
        raise StayNotCompletedError()


def eligible_stay_payload(booking):
    return {
        "booking_reference": booking.booking_reference,
        "room_type_name": booking.room_type.name,
        "check_in": booking.check_in,
        "check_out": booking.check_out,
        "nights": booking.nights,
        "guest_first_name": booking.guest.first_name,
        "already_reviewed": hasattr(booking, "review"),
    }


@transaction.atomic
def create_review(*, booking, rating, comment, request=None):
    """Create the one-and-only review for a completed stay."""
    check_eligibility(booking)
    try:
        review = Review.objects.create(
            booking=booking,
            guest=booking.guest,
            guest_name=booking.guest.full_name,
            rating=int(rating),
            comment=comment,
        )
    except IntegrityError:
        # Race between two concurrent submissions — the DB constraint wins.
        raise AlreadyReviewedError()

    log_action(
        actor=None, action="REVIEW_SUBMITTED", instance=review,
        metadata={"reference": booking.booking_reference, "rating": review.rating},
        request=request,
        summary=f"Guest review submitted for {booking.booking_reference} ({review.rating} star)",
    )
    # Administrators only — review content stays private to the top role.
    notify_staff(
        type="REVIEW_NEW",
        title="New customer review received",
        message=f"{review.guest_name} submitted a {review.rating}-star review.",
        link=f"{STAFF_REVIEWS_LINK}?id={review.pk}",
        roles=("ADMIN",),
    )
    logger.info("Review created for booking %s (rating=%s)", booking.booking_reference, review.rating)
    return review


def send_review_invitation(booking):
    """Post-stay 'how was your stay?' email with a direct review link.

    Called from the checkout routine; must never break checkout; delivery is deferred to after the
    checkout transaction commits via queue_email and errors are swallowed).
    No sensitive data in the link.
    """
    if booking.status != Booking.Status.CHECKED_OUT:
        return
    if hasattr(booking, "review"):
        return
    link = f"{settings.FRONTEND_URL}{REVIEW_PAGE_LINK}?ref={booking.booking_reference}"
    hotel = HotelSettings.get_settings()
    hotel_location = ", ".join(
        part for part in (hotel.address, hotel.city, hotel.state, hotel.country) if part
    )
    context = {
        "hotel_name": hotel.hotel_name,
        "hotel_location": hotel_location,
        "hotel_phone": hotel.phone,
        "hotel_email": hotel.email,
        **logo_context(),
        "guest_first_name": booking.guest.first_name,
        "guest_name": booking.guest.full_name,
        "booking_reference": booking.booking_reference,
        "room_type": booking.room_type.name,
        "rooms_count": booking.number_of_rooms,
        "check_in": format_date(booking.check_in),
        "check_out": format_date(booking.check_out),
        "nights": booking.nights,
        "review_url": link,
    }
    queue_email(
        kind="REVIEW_INVITE",
        booking_reference=booking.booking_reference,
        subject="How was your stay at J-ONE HOTEL & LODGE?",
        message=render_to_string("emails/review_invitation.txt", context),
        html_message=render_to_string("emails/review_invitation.html", context),
        recipients=[booking.guest.email],
    )


def mark_review_handled(review, *, staff_user, status=None, internal_notes=None, request=None):
    """Administrator status/notes update with audit trail."""
    changes = {}
    if status is not None and status != review.status:
        changes["status"] = [review.status, status]
        review.status = status
        if status == Review.Status.REVIEWED:
            review.reviewed_by = staff_user
            review.reviewed_at = timezone.now()
        else:
            review.reviewed_by = None
            review.reviewed_at = None
    if internal_notes is not None and internal_notes != review.internal_notes:
        changes["internal_notes"] = ["(updated)", "(updated)"]  # content itself is not logged
        review.internal_notes = internal_notes
    if changes:
        review.save()
        log_action(
            actor=staff_user, action="REVIEW_UPDATED", instance=review,
            changes=changes,
            metadata={"reference": review.booking.booking_reference},
            request=request,
        )
    return review


def delete_review(review, *, staff_user, request=None):
    reference = review.booking.booking_reference
    rating = review.rating
    log_action(
        actor=staff_user, action="REVIEW_DELETED", instance=review,
        metadata={"reference": reference, "rating": rating},
        request=request,
        summary=f"Review for {reference} deleted",
    )
    review.delete()
