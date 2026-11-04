"""Guest-only receipt confirmation; never captures or settles money."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.portal.eligibility import active_stay_rooms_for_email
from apps.pos.models import PosOrder
from apps.pos.services.order_service import _lock_portal_stay_before_order
from ..models import ServiceRequest, ServiceRequestEvent


@transaction.atomic
def confirm_guest_receipt(*, service_request, portal_session):
    email = str(portal_session.email).strip().lower()
    # Booking -> stay -> order -> request, consistent with checkout/fulfilment.
    candidate = PosOrder.objects.filter(service_request_id=service_request.pk).first()
    if candidate:
        _lock_portal_stay_before_order(candidate, require_active=True)
        order = PosOrder.objects.select_for_update().get(pk=candidate.pk)
    else:
        from apps.bookings.models import Booking
        from apps.stays.models import Stay
        identity = ServiceRequest.objects.values("stay_id", "stay__booking_id").get(pk=service_request.pk)
        if not identity["stay_id"]:
            raise PermissionDenied("Guest confirmation requires an authenticated in-house stay.")
        Booking.objects.select_for_update().get(pk=identity["stay__booking_id"])
        Stay.objects.select_for_update().get(pk=identity["stay_id"])
        order = None
    current = ServiceRequest.objects.select_for_update().select_related("guest").get(pk=service_request.pk)
    owner = (current.portal_email or getattr(current.guest, "email", "") or "").strip().lower()
    if owner != email or not active_stay_rooms_for_email(email).filter(stay_id=current.stay_id, room_id=current.room_id).exists():
        raise PermissionDenied("Only the ordering guest in the associated active stay may confirm receipt.")
    if current.metadata.get("guest_delivery_confirmation"):
        raise ValidationError("Receipt was already confirmed; no duplicate confirmation was recorded.")
    if order and order.status != PosOrder.Status.DELIVERED:
        raise ValidationError("The department must record physical delivery before guest confirmation.")
    if not order and current.status != ServiceRequest.Status.ACKNOWLEDGED:
        raise ValidationError("The department must acknowledge fulfilment before guest confirmation.")
    previous = current.status
    if previous in {ServiceRequest.Status.CLOSED, ServiceRequest.Status.CANCELLED}:
        raise ValidationError("This request is no longer open for confirmation.")
    now = timezone.now()
    confirmation = {"confirmed_at": now.isoformat(), "guest_email": email,
                    "portal_session_id": portal_session.pk, "guest_id": current.guest_id}
    current.metadata = {**current.metadata, "guest_delivery_confirmation": confirmation}
    current.status = ServiceRequest.Status.RESOLVED
    current.resolved_at = now
    current.save(update_fields=["metadata", "status", "resolved_at", "updated_at"])
    ServiceRequestEvent.objects.create(
        request=current, type=ServiceRequestEvent.Type.STATUS_CHANGED,
        actor_label=email, message="Guest confirmed receipt.", guest_visible=True,
        previous_status=previous, new_status=current.status,
        details={"guest_delivery_confirmation": confirmation, "pos_order_reference": order.reference if order else ""},
    )
    return current
