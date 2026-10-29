"""Verified guest menu orders, atomically linked to the existing POS workflow.

A guest order is a price-snapshotted POS *draft* for staff review, not a
payment and not a physical stock reservation. Open POS drafts count as soft
commitments and the POS rechecks them at submission and delivery.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from rest_framework.exceptions import NotFound

from apps.audit.services import log_action
from apps.bookings.models import Booking, RoomOccupant
from apps.portal.eligibility import INELIGIBLE_BOOKING_STATUSES, active_stay_rooms_for_email
from apps.pos.models import MenuCategory, MenuItem, PosOrder
from apps.pos.services.order_service import create_order
from apps.stays.models import Stay

from ..models import ServiceRequest
from .request_service import create_service_request, scoped_idempotency_key


def _require_safe_isolation():
    """Demand is read after balance locks; it must see the latest commits.

    Django defaults MySQL to READ COMMITTED. A deployment explicitly using a
    repeatable snapshot can otherwise miss an order committed while waiting for
    the stock lock. SQLite needs BEGIN IMMEDIATE (the development setting).
    Refuse guest ordering rather than risk overselling under another mode.
    """
    if connection.vendor == "mysql":
        variable = "@@tx_isolation" if connection.mysql_is_mariadb else "@@transaction_isolation"
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {variable}")
            level = str(cursor.fetchone()[0]).replace("-", " ").lower()
        if level != "read committed":
            raise ValidationError("Ordering is temporarily unavailable. Please contact the hotel.")
    elif connection.vendor == "sqlite":
        if getattr(connection, "transaction_mode", None) not in {"IMMEDIATE", "EXCLUSIVE"}:
            raise ValidationError("Ordering is temporarily unavailable. Please contact the hotel.")
    elif connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            level = str(cursor.fetchone()[0]).replace("-", " ").lower()
        if level != "read committed":
            raise ValidationError("Ordering is temporarily unavailable. Please contact the hotel.")
    else:
        raise ValidationError("Ordering is temporarily unavailable. Please contact the hotel.")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fingerprints(*, stay, room, lines, notes, payment_method):
    canonical_lines = sorted(
        [{"menu_item_id": line["menu_item_id"], "quantity": line["quantity"],
          "expected_unit_price": str(line["expected_unit_price"])} for line in lines],
        key=lambda line: (line["menu_item_id"], line["quantity"], line["expected_unit_price"]),
    )
    quantities = defaultdict(int)
    for line in lines:
        quantities[line["menu_item_id"]] += line["quantity"]
    identity = {"stay": stay.pk, "room": room.pk, "items": sorted(quantities.items())}
    intent = _digest(identity)
    fingerprint = _digest({**identity, "lines": canonical_lines, "notes": notes, "payment_method": payment_method})
    return fingerprint, intent


def _context(*, email, stay_reference, room_id):
    # Lock booking then stay, so a concurrent checkout cannot become effective
    # between the eligibility check and the order commit. Caller controls
    # neither the guest nor the folio; both come from the locked stay.
    candidate = Stay.objects.filter(reference=stay_reference).values("pk", "booking_id").first()
    if candidate is None:
        raise NotFound("An active stay matching this portal session was not found.")
    booking = Booking.objects.select_for_update().get(pk=candidate["booking_id"])
    stay = Stay.objects.select_for_update().select_related("guest").get(pk=candidate["pk"])
    if stay.status != Stay.Status.IN_HOUSE or booking.status in INELIGIBLE_BOOKING_STATUSES:
        raise NotFound("An active stay matching this portal session was not found.")

    eligible = active_stay_rooms_for_email(email).filter(stay_id=stay.pk).select_related("room", "booking_room").distinct()
    rooms = list(eligible.order_by("room_id"))
    if not rooms:
        raise NotFound("An active stay matching this portal session was not found.")
    if room_id:
        selected = next((entry for entry in rooms if entry.room_id == room_id), None)
        if selected is None:
            raise ValidationError({"room_id": "Select a room assigned to you for this active stay."})
    elif len(rooms) == 1:
        selected = rooms[0]
    else:
        raise ValidationError({"room_id": "Select the room where this order should be delivered."})

    is_contact = str(stay.guest.email or "").strip().lower() == email
    occupant = None
    if not is_contact:
        occupant = RoomOccupant.objects.filter(
            booking_room_id=selected.booking_room_id,
            booking_room__checked_in_at__isnull=False,
            email__iexact=email,
        ).first()
        if occupant is None:
            raise NotFound("An active stay matching this portal session was not found.")
    return stay, selected.room, occupant


def _existing_or_error(existing, *, fingerprint, stay, room, email):
    if (
        existing.channel != ServiceRequest.Channel.PORTAL
        or existing.stay_id != stay.pk or existing.room_id != room.pk
        or existing.portal_email.strip().lower() != email
        or (existing.metadata or {}).get("portal_order_fingerprint") != fingerprint
    ):
        raise ValidationError({"idempotency_key": "This retry key belongs to a different request. Start a new order."})
    if not PosOrder.objects.filter(service_request=existing).exists():
        # Never fabricate a second order for inconsistent or historical data.
        raise ValidationError("This request needs staff review before it can be retried. Please contact the hotel.")
    return existing, False


@transaction.atomic
def submit_portal_room_order(*, email, data, request=None):
    """Create the guest request and one linked POS draft, or neither.

    The unique guest-scoped key and one-to-one POS relationship handle retry
    races. Replaying a key with a changed payload is rejected; a new key for
    an identical still-open order is rejected rather than making a duplicate.
    """
    _require_safe_isolation()
    email = str(email or "").strip().lower()
    stay, room, occupant = _context(email=email, stay_reference=data["stay_reference"], room_id=data.get("room_id"))
    lines = list(data["lines"])
    notes = data.get("notes", "")
    fingerprint, intent = _fingerprints(stay=stay, room=room, lines=lines, notes=notes, payment_method=data["payment_method"])
    key = scoped_idempotency_key(scope=f"portal:{email}", raw_key=data["idempotency_key"])
    existing = ServiceRequest.objects.select_for_update().filter(idempotency_key=key).first()
    if existing:
        return _existing_or_error(existing, fingerprint=fingerprint, stay=stay, room=room, email=email)

    if PosOrder.objects.filter(
        service_request__stay=stay,
        service_request__room=room,
        service_request__portal_email__iexact=email,
        service_request__metadata__portal_order_intent=intent,
        status__in=[PosOrder.Status.DRAFT, PosOrder.Status.SUBMITTED, PosOrder.Status.PREPARING, PosOrder.Status.READY],
    ).exists():
        raise ValidationError({"lines": "An identical menu order is already in progress. Check Service requests or contact staff before ordering it again."})

    item_ids = sorted({line["menu_item_id"] for line in lines})
    items = {
        item.pk: item for item in MenuItem.objects.select_for_update().select_related("category")
        .filter(pk__in=item_ids, is_active=True, is_available=True, category__is_active=True).order_by("pk")
    }
    if len(items) != len(item_ids):
        raise ValidationError({"lines": "One or more menu items are no longer available. Refresh the menu."})
    enabled = set(getattr(settings, "PORTAL_ENABLED_SERVICE_CATEGORIES", None) or ServiceRequest.Category.values)
    category_for_area = {
        MenuCategory.ServiceArea.RESTAURANT: ServiceRequest.Category.ROOM_SERVICE,
        MenuCategory.ServiceArea.BAR: ServiceRequest.Category.FOOD_BEVERAGE,
    }
    categories = {category_for_area.get(item.category.service_area) for item in items.values()}
    if None in categories or not categories.issubset(enabled):
        raise ValidationError({"lines": "One or more selected menu items cannot be ordered through the portal."})
    category = ServiceRequest.Category.ROOM_SERVICE if ServiceRequest.Category.ROOM_SERVICE in categories else ServiceRequest.Category.FOOD_BEVERAGE
    summary = ("Menu order: " + ", ".join(f"{line['quantity']} × {items[line['menu_item_id']].name}" for line in lines))[:255]
    service_request, created = create_service_request(
        guest=stay.guest, stay=stay, room=room, occupant=occupant,
        category=category, priority=ServiceRequest.Priority.NORMAL, channel=ServiceRequest.Channel.PORTAL,
        summary=summary, detail=notes, portal_email=email, idempotency_key=key,
    )
    if not created:
        return _existing_or_error(service_request, fingerprint=fingerprint, stay=stay, room=room, email=email)
    service_request.metadata = {"portal_order_fingerprint": fingerprint, "portal_order_intent": intent}
    service_request.save(update_fields=["metadata", "updated_at"])
    order, order_created = create_order(
        mode=PosOrder.Mode.ROOM_SERVICE, actor=None, stay=stay, lines=lines,
        service_request=service_request, portal_email=email,
        delivery_location=f"Room {room.room_number}", notes=notes,
        idempotency_key=f"portal-order:{service_request.reference}",
    )
    if not order_created:
        raise ValidationError("This request already has an order. Please contact the hotel.")
    log_action(
        actor=None, action="PORTAL_ROOM_SERVICE_ORDER_CREATED", instance=service_request, request=request,
        metadata={"request_reference": service_request.reference, "pos_order_reference": order.reference,
                  "stay_reference": stay.reference, "room_id": room.pk},
    )
    return service_request, True
