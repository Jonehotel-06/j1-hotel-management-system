"""Receptionist guest-amenity issuance and complementary-breakfast services.

Both features integrate with the EXISTING inventory ledger rather than
creating a parallel stock database:

* Amenity allocation is an ordinary stock TRANSFER into the Reception
  Department's stock location, so the allocated quantity becomes the
  Reception location's own balance (deducted from the issuing store).
* Amenity issuance deducts from the Reception location's balance via the
  immutable ``StockMovement`` ledger and records a human-readable
  ``GuestAmenityIssue`` beside it.
* Breakfast remains a guest-accommodation benefit: it records its own row and
  never creates a POS order, a folio charge or restaurant revenue. When the
  kitchen names real stock items, their consumption is written to the SAME
  immutable inventory ledger as any other internal issue. No second expense or
  revenue journal is posted; the goods were already expensed at receipt.
"""
from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict, deque
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q, prefetch_related_objects
from django.utils import timezone

from apps.accounts.capabilities import has_capability
from apps.core.utils import hotel_today
from apps.inventory.models import StockBalance, StockDepartment, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import (
    canonical_unit, is_whole_count_unit, record_stock_movement, resolve_entered_quantity, scoped_key,
    transfer_stock, unit_label,
)

from apps.inventory.services.stock_routing import legacy_location_role, location_role

from .models import BreakfastBenefitConfig, BreakfastIssuance, GuestAmenityIssue, MiniMartProduct
from .services.breakfast_occupancy import breakfast_bookings, breakfast_room_numbers

QTY = Decimal("0.0001")

#: Legacy location code retained ONLY to discover an unflagged installation.
#: The authoritative configured shelf is ``StockLocation.is_reception``.
RECEPTION_LOCATION_CODE = "RECEPTION"


def _reference(prefix):
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def reception_location():
    """The active configured Reception shelf, with a legacy-code fallback."""
    configured = StockLocation.objects.filter(is_reception=True, is_active=True).order_by("pk").first()
    if configured is not None:
        return configured
    return StockLocation.objects.filter(code__iexact=RECEPTION_LOCATION_CODE, is_active=True).order_by("pk").first()


def amenity_items_filter(prefix=""):
    return Q(**{
        f"{prefix}is_active": True,
        f"{prefix}item_type": StockItem.ItemType.CONSUMABLE,
        f"{prefix}minimart_product__isnull": True,
    })


def amenity_shelf(location=None):
    target = reception_location()
    if location is not None and (target is None or location.pk != target.pk):
        raise ValueError({"location_id": "Guest amenities may only use the configured Reception shelf."})
    return target


AMENITY_TRANSFER_PREFIX = "transfer:reception-amenity-alloc:"


def eligible_amenity_lots(location, item_ids, *, for_update=False):
    """Reconcile eligible lots against every shelf depletion, in posting order.

    StockBalance is still the sole stock projection. A matching transfer pair
    and the old dedicated amenity source key are evidence of issuance; ordinary
    transfers, opening stock, returns and adjustments never create eligibility.
    Debits consume eligible stock first (conservative for a mixed legacy shelf).
    There is no separate mutable ordered/issued balance that can drift.
    """
    if not item_ids:
        return {}
    movements = StockMovement.objects.filter(location_id=location.pk, item_id__in=item_ids).filter(
        Q(quantity_delta__lt=0) | Q(type=StockMovement.Type.TRANSFER_IN, source_key__startswith=AMENITY_TRANSFER_PREFIX)
    ).order_by("pk")
    if for_update:
        movements = movements.select_for_update()
    movements = list(movements)
    inbound = [m for m in movements if m.type == StockMovement.Type.TRANSFER_IN and m.source_key.endswith(":in")]
    outbound = StockMovement.objects.filter(
        source_key__in=[m.source_key.removesuffix(":in") + ":out" for m in inbound],
        type=StockMovement.Type.TRANSFER_OUT,
    )
    if for_update:
        # Current reads matter on MySQL: another transaction can have posted
        # both legs while this transaction waited on the item lock.
        outbound = outbound.select_for_update()
    matched = list(outbound)
    prefetch_related_objects(matched, "location", "actor")
    by_key = {m.source_key: m for m in matched}
    lots = defaultdict(deque)
    for m in movements:
        queue = lots[m.item_id]
        if m.quantity_delta < 0:
            remaining = -m.quantity_delta
            while queue and remaining:
                incoming, outgoing, available = queue[0]
                used = min(remaining, available)
                remaining -= used
                if used == available:
                    queue.popleft()
                else:
                    queue[0] = (incoming, outgoing, available - used)
            continue
        original = by_key.get(m.source_key.removesuffix(":in") + ":out")
        if not (original and original.item_id == m.item_id and original.quantity_delta == -m.quantity_delta
                and original.location_id != location.pk and original.actor_id == m.actor_id):
            continue
        role = (original.metadata or {}).get("amenity_source_role")
        # Older dedicated allocations lack a store-role snapshot. A generic
        # transfer or a custom store newly classified as MAIN is not evidence.
        legacy_main = (not role and original.actor_id is not None and
                       legacy_location_role(original.location) == StockLocation.OperationalRole.MAIN)
        if role == StockLocation.OperationalRole.MAIN or legacy_main:
            queue.append((m, original, m.quantity_delta))
    return lots


def amenity_lot_evidence(inbound, outbound, available):
    meta = outbound.metadata or {}
    return {
        "issued_movement": outbound.reference, "received_movement": inbound.reference,
        "source_store": meta.get("amenity_source_code") or outbound.location.code,
        "source_store_name": meta.get("amenity_source_name") or outbound.location.name,
        "issued_by": meta.get("amenity_issuer_name") or getattr(outbound.actor, "full_name", ""),
        "issued_by_id": outbound.actor_id, "issued_at": outbound.occurred_at.isoformat(),
        "allocated_quantity": str(inbound.quantity_delta), "remaining_quantity": str(available),
    }


def eligible_amenity_quantity(item, balance, lots):
    available = max(Decimal("0"), min(balance, sum((lot[2] for lot in lots), Decimal("0"))))
    # Existing partial counts remain on the ledger but cannot be handed out as
    # a fraction of a physical unit. This is an issue limit, not a stock edit.
    return Decimal(int(available)) if is_whole_count_unit(item.base_unit) else available


@transaction.atomic
def allocate_reception_amenities(
    *, item, quantity, from_location, to_location=None, unit=None, conversion_factor=None,
    actor, notes="", idempotency_key="",
):
    """Store Keeper allocates amenity stock to the Reception Department.

    The allocation is a stock TRANSFER from ``from_location`` into the
    Reception location, so the quantity leaves the issuing store and becomes
    the Reception Department's own balance. Returns ``(outbound, inbound)``.
    """
    if not has_capability(actor, "guest_amenity.allocate"):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("Only authorized staff can allocate guest amenities from Main Store.")
    target = amenity_shelf(to_location)
    if target is None:
        raise ValueError({"location_id": "Ask an administrator to configure the Reception shelf."})
    if from_location is None or from_location.pk == target.pk:
        raise ValueError({"from_location_id": "Choose the Main Store as the source."})
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    locked = {row.pk: row for row in StockLocation.objects.select_for_update()
              .filter(pk__in=[from_location.pk, target.pk]).order_by("pk")}
    from_location, target = locked[from_location.pk], locked[target.pk]
    if not from_location.is_active or from_location.is_reception or location_role(from_location) != StockLocation.OperationalRole.MAIN:
        raise ValueError({"from_location_id": "Guest amenity stock must be issued from an active Main Store."})
    if not amenity_shelf(target) or not target.is_active:
        raise ValueError({"location_id": "The configured Reception shelf changed; reload and retry."})
    if not item.is_active or item.item_type != StockItem.ItemType.CONSUMABLE or MiniMartProduct.objects.filter(item=item).exists():
        raise ValueError({"item_id": "Choose an active non-retail consumable approved for guest amenities."})
    base_quantity, evidence = resolve_entered_quantity(
        quantity, item=item, unit=unit or item.base_unit, conversion_factor=conversion_factor,
        allow_declared_pack_size=True,
    )

    raw_key = str(idempotency_key or "").strip()
    key = scoped_key(prefix="reception-amenity-alloc", scope=f"staff:{getattr(actor, 'pk', 'system')}", raw_key=raw_key)
    if key is None:
        key = f"reception-amenity-alloc:{item.pk}:{from_location.pk}:{timezone.now():%Y%m%d%H%M%S%f}"
    return transfer_stock(
        item=item, from_location=from_location, to_location=target,
        quantity=base_quantity, actor=actor, transfer_key=key, notes=notes or "Amenity allocation to Reception",
        department=StockDepartment.FRONT_DESK,
        metadata={"amenity_source_role": StockLocation.OperationalRole.MAIN,
                  "amenity_source_code": from_location.code, "amenity_source_name": from_location.name,
                  "amenity_issuer_name": actor.full_name,
                  "quantity_evidence": evidence},
    )


@transaction.atomic
def issue_guest_amenity(
    *, guest, booking, room_number, item, quantity, actor, notes="", idempotency_key="", location=None,
):
    """Issue one amenity line to a guest from the Reception allocation.

    Deducts from the Reception location's balance (NOT Main Store) and records
    the immutable movement plus the ``GuestAmenityIssue`` evidence row. Raises
    ``ValueError`` when the Reception stock is insufficient.
    """
    if booking is not None and booking.guest_id != guest.pk:
        raise ValueError({"booking_id": "That booking does not belong to this guest."})
    if booking is not None:
        rooms = list(booking.room_assignments.values_list("room__room_number", flat=True))
        if rooms:
            if room_number and room_number not in rooms:
                raise ValueError({"room_number": "Choose a room assigned to this booking."})
            room_number = room_number or (rooms[0] if len(rooms) == 1 else "")
    if not has_capability(actor, "guest_amenity.issue"):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("You are not authorized to issue guest amenities.")
    location = amenity_shelf(location)
    if location is None:
        raise ValueError({"location_id": "Ask an administrator to configure the Reception shelf."})

    # All stock writers serialize on this item/shelf before any movement.
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    location = StockLocation.objects.select_for_update().get(pk=location.pk)
    amenity_shelf(location)
    balance = StockBalance.objects.select_for_update().filter(item=item, location=location).first()
    amount, _ = resolve_entered_quantity(quantity, item=item, unit=item.base_unit)
    raw_key = str(idempotency_key or "").strip()
    if raw_key:
        key = scoped_key(prefix="guest-amenity-issue", scope=f"guest:{guest.pk}:item:{item.pk}", raw_key=raw_key)
    else:
        key = f"guest-amenity-issue:{guest.pk}:{item.pk}:{amount}:{timezone.now():%Y%m%d%H%M%S%f}"

    existing = GuestAmenityIssue.objects.select_for_update().filter(source_key=key).first()
    if existing is not None:
        if (existing.quantity != amount or existing.booking_id != getattr(booking, "pk", None)
                or existing.room_number != (room_number or "") or existing.location_id != location.pk):
            raise ValueError({"idempotency_key": "This key already identifies a different amenity issuance."})
        return existing, False

    # During rollout the old check-in modal's booking/day/item key may have
    # already deducted stock. Respect single-room evidence and refuse to
    # apportion a historical combined-room hand-over to individual rooms.
    if booking and room_number and raw_key.startswith(f"checkin-room:{booking.booking_reference}:{room_number}:{item.pk}:"):
        from datetime import date
        day = raw_key.rsplit(":", 1)[-1]
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError({"idempotency_key": "The check-in key requires a service date."}) from exc
        legacy_keys = [scoped_key(prefix="guest-amenity-issue", scope=f"guest:{guest.pk}:item:{item.pk}",
                       raw_key=f"checkin:{lookup}:{item.pk}:{day}") for lookup in (booking.booking_reference, booking.pk)]
        legacy = GuestAmenityIssue.objects.select_for_update().filter(
            source_key__in=legacy_keys, booking=booking, guest=guest, item=item,
        ).first()
        if legacy and (not legacy.room_number or room_number in legacy.room_number.split(", ")):
            if legacy.room_number == room_number and legacy.quantity == amount:
                return legacy, False
            raise ValueError({"idempotency_key": f"Review the previous check-in issuance {legacy.reference} before recording another hand-over."})

    if not item.is_active or item.item_type != StockItem.ItemType.CONSUMABLE or MiniMartProduct.objects.filter(item=item).exists():
        raise ValueError({"item_id": "This item is not eligible for guest amenities."})
    lots = eligible_amenity_lots(location, [item.pk], for_update=True).get(item.pk, ())
    physical_before = balance.quantity_on_hand if balance else Decimal("0")
    eligible_before = eligible_amenity_quantity(item, physical_before, lots)
    if eligible_before < amount:
        raise ValueError({"quantity": f"Only {eligible_before:f} {item.base_unit} of {item.sku} remain from Main Store amenity issuance."})

    needed = amount
    sources = []
    for inbound, outbound, available in lots:
        used = min(needed, available)
        sources.append({**amenity_lot_evidence(inbound, outbound, available), "quantity": str(used)})
        needed -= used
        if not needed:
            break

    movement, _created = record_stock_movement(
        item=item, location=location, movement_type=StockMovement.Type.ISSUE,
        quantity_delta=-amount, source_key=key, actor=actor,
        source_reference=booking.booking_reference if booking else "",
        notes=notes or f"Guest amenity issued to {guest.full_name}",
        department=StockDepartment.FRONT_DESK, recipient=guest.full_name,
        metadata={"guest_amenity_issue": True, "guest_id": guest.pk, "main_store_issuances": sources,
                  "eligible_balance_before": str(eligible_before), "eligible_balance_after": str(eligible_before - amount)},
    )

    record = GuestAmenityIssue.objects.create(
        reference=_reference("AMY"), source_key=key, guest=guest, booking=booking,
        room_number=(room_number or "")[:40], receptionist=actor, item=item, quantity=amount,
        location=location, departmental_balance_before=physical_before, departmental_balance_after=physical_before - amount,
        occurred_at=timezone.now(), notes=(notes or "")[:500], movement=movement,
    )
    return record, True


# ---------------------------------------------------------------------------
# Complementary breakfast
# ---------------------------------------------------------------------------


def get_breakfast_config():
    config, _created = BreakfastBenefitConfig.objects.get_or_create(pk=1)
    return config


def breakfast_eligibility(*, guest, booking, service_date):
    """One meal entitlement per booking guest/day, backed by actual occupancy."""
    config = get_breakfast_config()
    payload = {
        "enabled": config.is_enabled, "eligible": False, "reason": "", "eligible_guests": 0,
        "check_in": booking.check_in if booking else None,
        "check_out": booking.check_out if booking else None,
        "status": booking.status if booking else None, "room_numbers": [],
    }
    if not config.is_enabled:
        payload["reason"] = "The complementary breakfast benefit is currently disabled."
    elif booking is None:
        payload["reason"] = "No currently checked-in room is linked to this guest."
    elif guest is not None and booking.guest_id != guest.pk:
        payload["reason"] = "That booking does not belong to this guest."
    elif service_date > hotel_today():
        payload["reason"] = "Breakfast cannot be recorded for a future service date."
    elif booking.check_out < hotel_today() or booking.status == booking.Status.CHECKED_OUT:
        payload["reason"] = "Breakfast benefits end at check-out."
    elif booking.check_in > hotel_today() and not booking.checked_in_at:
        payload["reason"] = "Breakfast requires actual check-in, not a future reservation."
    elif not breakfast_bookings(service_date).filter(pk=booking.pk).exists():
        payload["reason"] = "Breakfast requires an active, physically assigned checked-in room."
    else:
        rooms = breakfast_room_numbers([booking.pk], service_date).get(booking.pk, [])
        if not rooms:
            payload["reason"] = "There is no active physical room for this booking."
        else:
            payload.update(eligible=True, room_numbers=rooms,
                           eligible_guests=max(int(booking.adults or 0) + int(booking.children or 0), 1))
    return payload


@transaction.atomic
def record_breakfast(*, guest, booking, service_date, items=None, actor=None, notes="",
                     idempotency_key="", location_id=None):
    """Record one complementary breakfast and its optional stock consumption.

    No POS order, guest folio charge, restaurant revenue or duplicate expense
    is created: the benefit is already included in accommodation. Real stock
    lines, when supplied, are deducted through source-keyed ISSUE movements on
    the existing inventory ledger so the kitchen's on-hand balances remain
    truthful. Descriptive-only lines remain evidence and never touch stock.
    """
    from apps.bookings.models import Booking, Guest
    if not has_capability(actor, "breakfast.issue"):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("You are not authorized to record breakfast.")
    Guest.objects.select_for_update().get(pk=guest.pk)
    if booking is not None:
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking is None or booking.guest_id != guest.pk:
        raise ValueError({"booking_id": "Choose a current checked-in booking for this guest."})

    raw_key = str(idempotency_key or "").strip()
    if raw_key:
        key = scoped_key(prefix="breakfast-issue", scope=f"guest:{guest.pk}", raw_key=raw_key)
    else:
        key = f"breakfast-issue:{guest.pk}:{service_date}"

    existing = BreakfastIssuance.objects.select_for_update().filter(source_key=key).first()
    if existing is not None:
        if existing.booking_id != booking.pk or existing.service_date != service_date:
            raise ValueError({"idempotency_key": "This key was already used for a different breakfast."})
        existing.stock_movement_count = StockMovement.objects.filter(
            source_reference=existing.reference,
            metadata__breakfast_issuance=True,
        ).count()
        return existing, False

    eligibility = breakfast_eligibility(guest=guest, booking=booking, service_date=service_date)
    if not eligibility["eligible"]:
        raise ValueError({"service_date": eligibility["reason"]})

    if BreakfastIssuance.objects.filter(guest=guest, service_date=service_date).exists():
        raise ValueError({
            "service_date": (
                f"{guest.full_name} has already received breakfast for {service_date}. "
                "Duplicate claims for the same guest and service date are not allowed."
            )
        })

    location = None
    stock_lines = []
    normalized_items = []
    for index, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            continue
        row = {
            "item_id": raw.get("item_id"),
            "item_sku": str(raw.get("item_sku") or ""),
            "item_name": str(raw.get("item_name") or ""),
            "quantity": str(raw.get("quantity") or ""),
            "unit": str(raw.get("unit") or ""),
            "notes": str(raw.get("notes") or "")[:255],
        }
        # A real stock line requires an item id and a positive quantity. Rows
        # without them are preserved as human-readable service evidence.
        if row["item_id"] and row["quantity"]:
            stock_lines.append((index, raw, row))
        normalized_items.append(row)

    if stock_lines:
        if not location_id:
            raise ValueError({"location_id": "Choose the store breakfast stock is issued from."})
        location = StockLocation.objects.filter(pk=location_id, is_active=True).first()
        if location is None:
            raise ValueError({"location_id": "Choose an active stock location for breakfast consumption."})

    # Reserve the immutable evidence reference BEFORE writing movements. The
    # issuance row is created only after every stock line has validated and
    # posted, so the append-only row is written once with its final evidence —
    # no post-create SQL update can bypass the model's immutability guard.
    reference = _reference("BKF")
    movement_count = 0
    stock_items = {item.pk: item for item in StockItem.objects.select_for_update().filter(
        pk__in=[row["item_id"] for _, _, row in stock_lines], is_active=True,
    ).order_by("pk")}
    for line_index, raw, row in stock_lines:
        item = stock_items.get(row["item_id"])
        if item is None:
            raise ValueError({"items": f"Stock item {row['item_id']} is unknown or inactive."})
        selected_unit = str(raw.get("unit") or item.base_unit).strip()
        conversion_factor = raw.get("conversion_factor")
        if (canonical_unit(selected_unit) != canonical_unit(item.base_unit)
                and conversion_factor in (None, "")):
            raise ValueError({
                "conversion_factor": (
                    f"Enter how many {unit_label(item.base_unit).lower()} one "
                    f"{unit_label(selected_unit).lower()} holds, or count in the item's base unit."
                )
            })
        try:
            base_quantity, evidence = resolve_entered_quantity(
                row["quantity"], item=item, unit=selected_unit,
                conversion_factor=conversion_factor, allow_declared_pack_size=False,
            )
            movement, created = record_stock_movement(
                item=item, location=location,
                movement_type=StockMovement.Type.ISSUE,
                quantity_delta=-base_quantity,
                source_key=f"breakfast-issue:{key}:{line_index}",
                actor=actor, source_reference=reference,
                notes=str(raw.get("notes") or f"Complementary breakfast for {guest.full_name}")[:500],
                department=StockDepartment.KITCHEN, recipient=guest.full_name,
                issue_method=StockMovement.IssueMethod.MANUAL,
                metadata={
                    "automated": True,
                    "breakfast_issuance": True,
                    "breakfast_reference": reference,
                    "guest_id": guest.pk,
                    "booking_reference": booking.booking_reference,
                    "quantity_evidence": evidence,
                    "no_revenue_posting": True,
                },
            )
        except DjangoValidationError as exc:
            raise ValueError({"items": str(exc)}) from exc
        if created:
            movement_count += 1
        row.update({
            "item_sku": item.sku,
            "item_name": item.name,
            "base_quantity": str(base_quantity),
            "base_unit": item.base_unit,
            "location_id": location.pk,
            "location_name": location.name,
            "stock_movement_reference": movement.reference,
            "quantity_evidence": evidence,
        })

    record = BreakfastIssuance.objects.create(
        reference=reference, source_key=key, guest=guest, booking=booking,
        service_date=service_date, issued_by=actor, eligible_guests=eligibility["eligible_guests"],
        room_numbers=eligibility["room_numbers"], items=normalized_items, occurred_at=timezone.now(), notes=(notes or "")[:500],
    )
    record.stock_movement_count = movement_count
    return record, True
