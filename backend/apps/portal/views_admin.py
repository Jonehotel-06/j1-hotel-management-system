# apps/portal/views_admin.py
"""Staff endpoints for the automatic guest-portal invitation (status, copy text, resend)."""
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.audit.services import log_action
from apps.bookings.models import Booking
from apps.core.permissions import HasCapability
from apps.core.responses import success_response
from apps.stays.models import Stay

from .invitations import copy_text_for, mask_email, portal_login_url, resend_portal_invitation, sync_invitation_status
from .models import GuestPortalInvitation


def _stay_for_booking(lookup):
    booking = Booking.objects.filter(booking_reference=lookup).first()
    if booking is None:
        raise NotFound("Booking not found.")
    stay = Stay.objects.select_related("guest").filter(booking=booking).order_by("-pk").first()
    if stay is None or stay.status != Stay.Status.IN_HOUSE:
        raise NotFound("No in-house stay for this booking.")
    return stay


def _payload(stay, invitation):
    return {
        "stay_reference": stay.reference,
        "status": invitation.status if invitation else "NOT_QUEUED",
        "recipient_hint": mask_email(getattr(stay.guest, "email", "") or ""),
        "attempts": invitation.attempts if invitation else 0,
        "last_attempt_at": invitation.last_attempt_at if invitation else None,
        "last_error_code": invitation.last_error_code if invitation else "",
        "portal_url": portal_login_url(),
        # Copy fallback for the desk: never contains a sign-in token.
        "copy_text": copy_text_for(invitation, (stay.guest.first_name or "").strip()) if invitation else "",
    }


@extend_schema(tags=["Admin · Stays"], summary="Portal invitation status and copy text for an in-house booking")
class AdminBookingPortalInvitationView(APIView):
    permission_classes = [HasCapability]
    required_capability = "stay.check_in"

    def get(self, request, lookup):
        stay = _stay_for_booking(lookup)
        invitation = GuestPortalInvitation.objects.filter(stay=stay).first()
        if invitation is not None:
            invitation = sync_invitation_status(invitation)
        return success_response(_payload(stay, invitation))

    def post(self, request, lookup):
        stay = _stay_for_booking(lookup)
        with transaction.atomic():
            invitation = resend_portal_invitation(stay=stay, actor=request.user)
        log_action(
            actor=request.user, action="PORTAL_INVITATION_RESENT", instance=invitation, request=request,
            metadata={"stay_reference": stay.reference, "status": invitation.status},
        )
        return success_response(_payload(stay, invitation), message="Portal invitation status updated.")
