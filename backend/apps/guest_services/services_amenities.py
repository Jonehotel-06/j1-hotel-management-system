"""Receptionist guest-amenity issuance and complementary-breakfast services.

Both features integrate with the EXISTING inventory ledger rather than
creating a parallel stock database:

* Amenity allocation is an ordinary stock TRANSFER into the Reception
  Department's stock location, so the allocated quantity becomes the
  Reception location's own balance (deducted from the issuing store).
* Amenity issuance deducts from the Reception location's balance via the
  immutable ``StockMovement`` ledger and records a human-readable
  ``GuestAmenityIssue`` beside it.
* Breakfast is a guest-accommodation benefit: it records its own row and never
  creates a POS order, a folio charge, restaurant revenue or an inventory
  movement.
"""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from apps.core.utils import hotel_today
from apps.inventory.models import StockBalance, StockDepartment, StockItem, StockLocation, StockMovement
from apps.inventory.services.inventory_service import record_stock_movement, scoped_key, transfer_stock

from .models import BreakfastBenefitConfig, BreakfastIssuance, GuestAmenityIssue

QTY = Decimal("0.0001")

#: Stock-location code that holds the Reception Department's allocated stock.
RECEPTION_LOCATION_CODE = "RECEPTION"


def _reference(prefix):
    return f"{prefix}-{timezone.now():%Y%m%d}-{secrets.token_hex(6).upper()}"


def _quantity(value, *, field="quantity", positive=False):
    try:
        amount = Decimal(str(value)).quantize(QTY, rounding=ROUND_HALF_UP)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Enter a valid {field}.") from exc
    if positive and amount <= Decimal("0.0000"):
        raise ValueError(f"{field} must be greater than zero.")
    return amount


def reception_location():
    """The Reception Department's stock location (by code), else None."""
    return StockLocation.objects.filter(code__iexact=RECEPTION_LOCATION_CODE, is_active=True).first()


def reception_amenity_stock(item):
    """Current allocated balance of one item at the Reception location."""
    location = reception_location()
    if location is None:
        return None, Decimal("0.0000")
    balance = StockBalance.objects.filter(item=item, location=location).first()
    return location, Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")


@transaction.atomic
def allocate_reception_amenities(
    *, item, quantity, from_location, unit=None, conversion_factor=None,
    actor, notes="", idempotency_key="",
):
    """Store Keeper allocates amenity stock to the Reception Department.

    The allocation is a stock TRANSFER from ``from_location`` into the
    Reception location, so the quantity leaves the issuing store and becomes
    the Reception Department's own balance. Returns ``(outbound, inbound)``.
    """
    from apps.inventory.services.inventory_service import resolve_entered_quantity

    if from_location is None:
        raise ValueError({"from_location_id": "Choose the store the amenities are issued from."})
    target = reception_location()
    if target is None:
        raise ValueError({
            "location": (
                "The Reception Department has no active stock location. Create one with the "
                f"code '{RECEPTION_LOCATION_CODE}' and then allocate amenity stock to it."
            )
        })
    if from_location.pk == target.pk:
        raise ValueError({"from_location_id": "The Reception Department already holds this stock."})

    base_quantity = _quantity(quantity, positive=True)
    if unit and str(unit).strip():
        base_quantity, _evidence = resolve_entered_quantity(
            quantity, item=item, unit=unit, conversion_factor=conversion_factor,
            allow_declared_pack_size=True,
        )

    raw_key = str(idempotency_key or "").strip()
    key = scoped_key(prefix="reception-amenity-alloc", scope=f"staff:{getattr(actor, 'pk', 'system')}", raw_key=raw_key)
    if key is None:
        key = f"reception-amenity-alloc:{item.pk}:{from_location.pk}:{timezone.now():%Y%m%d%H%M%S%f}"
    return transfer_stock(
        item=item, from_location=from_location, to_location=target,
        quantity=base_quantity, actor=actor, transfer_key=key, notes=notes or "Amenity allocation to Reception",
    )


@transaction.atomic
def issue_guest_amenity(
    *, guest, booking, room_number, item, quantity, actor, notes="", idempotency_key="",
):
    """Issue one amenity line to a guest from the Reception allocation.

    Deducts from the Reception location's balance (NOT Main Store) and records
    the immutable movement plus the ``GuestAmenityIssue`` evidence row. Raises
    ``ValueError`` when the Reception stock is insufficient.
    """
    location = reception_location()
    if location is None:
        raise ValueError({
            "location": (
                "The Reception Department has no active stock location. Ask the Store Keeper to "
                f"create one with the code '{RECEPTION_LOCATION_CODE}' and allocate amenity stock to it."
            )
        })
    amount = _quantity(quantity, positive=True)

    # Lock the item, location and balance before checking the available quantity.
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    location = StockLocation.objects.select_for_update().get(pk=location.pk)
    balance = StockBalance.objects.select_for_update().filter(item=item, location=location).first()
    before = Decimal(balance.quantity_on_hand) if balance else Decimal("0.0000")
    if before < amount:
        raise ValueError({
            "quantity": (
                f"Only {before.quantize(QTY)} {item.base_unit} of {item.sku} are allocated to the "
                f"Reception Department; {amount.quantize(QTY)} requested. Ask the Store Keeper to allocate more."
            )
        })

    raw_key = str(idempotency_key or "").strip()
    if raw_key:
        key = scoped_key(prefix="guest-amenity-issue", scope=f"guest:{guest.pk}:item:{item.pk}", raw_key=raw_key)
    else:
        key = f"guest-amenity-issue:{guest.pk}:{item.pk}:{amount}:{timezone.now():%Y%m%d%H%M%S%f}"

    existing = GuestAmenityIssue.objects.select_for_update().filter(source_key=key).first()
    if existing is not None:
        return existing, False

    movement, _created = record_stock_movement(
        item=item, location=location, movement_type=StockMovement.Type.ISSUE,
        quantity_delta=-amount, source_key=key, actor=actor,
        source_reference=booking.booking_reference if booking else "",
        notes=notes or f"Guest amenity issued to {guest.full_name}",
        department=StockDepartment.FRONT_DESK, recipient=guest.full_name,
        metadata={"guest_amenity_issue": True, "guest_id": guest.pk},
    )
    balance.refresh_from_db()
    after = Decimal(balance.quantity_on_hand)

    record = GuestAmenityIssue.objects.create(
        reference=_reference("AMY"), source_key=key, guest=guest, booking=booking,
        room_number=(room_number or "")[:40], receptionist=actor, item=item, quantity=amount,
        location=location, departmental_balance_before=before, departmental_balance_after=after,
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
    """Whether a guest may receive breakfast on a service date, and why not.

    The booking must be active for the whole day (check-in <= date <= check-out)
    and not checked out, cancelled or expired. The number of eligible guests is
    the booking's occupants (adults + children), not the number of rooms.
    """
    config = get_breakfast_config()
    payload = {
        "enabled": config.is_enabled,
        "eligible": False,
        "reason": "",
        "eligible_guests": 0,
        "check_in": None,
        "check_out": None,
        "status": None,
    }
    if not config.is_enabled:
        payload["reason"] = "The complementary breakfast benefit is currently disabled."
        return payload
    if booking is None:
        payload["reason"] = "No active booking is linked to this guest."
        return payload
    if booking.status in {"CHECKED_OUT", "CANCELLED", "EXPIRED", "NO_SHOW"}:
        payload["reason"] = (
            f"This booking is {booking.get_status_display().lower()}; breakfast benefits end at check-out."
        )
        payload["status"] = booking.status
        return payload
    if not (booking.check_in <= service_date <= booking.check_out):
        payload["reason"] = (
            f"Breakfast is only available between check-in ({booking.check_in}) and "
            f"check-out ({booking.check_out})."
        )
        payload["check_in"] = booking.check_in
        payload["check_out"] = booking.check_out
        payload["status"] = booking.status
        return payload
    payload.update({
        "eligible": True,
        "eligible_guests": max(int(booking.adults or 0) + int(booking.children or 0), 1),
        "check_in": booking.check_in,
        "check_out": booking.check_out,
        "status": booking.status,
    })
    return payload


@transaction.atomic
def record_breakfast(*, guest, booking, service_date, items=None, actor=None, notes="", idempotency_key=""):
    """Record a complementary breakfast issuance (no POS order, no charge).

    Raises ``ValueError`` when the guest is not eligible, when a duplicate claim
    for the same guest and service date already exists, or when the booking
    does not match the guest.
    """
    eligibility = breakfast_eligibility(guest=guest, booking=booking, service_date=service_date)
    if not eligibility["eligible"]:
        raise ValueError({"service_date": eligibility["reason"]})
    if booking is None or booking.guest_id != guest.pk:
        raise ValueError({"booking_id": "That booking does not belong to this guest."})

    raw_key = str(idempotency_key or "").strip()
    if raw_key:
        key = scoped_key(prefix="breakfast-issue", scope=f"guest:{guest.pk}", raw_key=raw_key)
    else:
        key = f"breakfast-issue:{guest.pk}:{service_date}"

    existing = BreakfastIssuance.objects.select_for_update().filter(source_key=key).first()
    if existing is not None:
        return existing, False

    # Duplicate claim for the same guest + service date is refused at the
    # database level as well, so a concurrent retry cannot slip past the
    # source-keyed check.
    if BreakfastIssuance.objects.filter(guest=guest, service_date=service_date).exists():
        raise ValueError({
            "service_date": (
                f"{guest.full_name} has already received breakfast for {service_date}. "
                "Duplicate claims for the same guest and service date are not allowed."
            )
        })

    normalized_items = []
    for raw in (items or []):
        if not isinstance(raw, dict):
            continue
        normalized_items.append({
            "item_id": raw.get("item_id"),
            "item_sku": raw.get("item_sku", ""),
            "item_name": raw.get("item_name", ""),
            "quantity": str(raw.get("quantity", "")),
            "unit": raw.get("unit", ""),
        })

    record = BreakfastIssuance.objects.create(
        reference=_reference("BKF"), source_key=key, guest=guest, booking=booking,
        service_date=service_date, issued_by=actor, eligible_guests=eligibility["eligible_guests"],
        items=normalized_items, occurred_at=timezone.now(), notes=(notes or "")[:500],
    )
    return record, True
