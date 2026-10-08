# apps/bookings/services/pricing.py
"""Authoritative pricing engine.

Every Naira amount on a booking originates here. The frontend may display
these numbers, but it can never supply them: totals are recomputed on the
server for quote requests AND again at booking creation.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from apps.core.exceptions import CapacityExceededError, InvalidDatesError, OfferNotApplicableError
from apps.core.utils import money
from apps.hotel.models import HotelSettings
from apps.offers import services as offer_services

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")


def q(value) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass
class Quote:
    room_type_id: int
    check_in: object
    check_out: object
    rooms: int
    adults: int
    children: int
    guests: int
    nights: int
    currency: str
    price_per_night: Decimal
    subtotal: Decimal
    offer: object = None
    discount: Decimal = Decimal("0.00")
    # Personal (per-guest) discount when it beat the public offer. Offers and
    # guest discounts never stack — see apps.offers.services.best_discount_for_stay.
    guest_discount: object = None
    discount_source: str = ""
    extra_guests: int = 0
    extra_guest_fee: Decimal = Decimal("0.00")
    tax: Decimal = Decimal("0.00")
    service_fee: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    required_payment: Decimal = Decimal("0.00")

    def to_api_dict(self):
        return {
            "nights": self.nights,
            "rooms": self.rooms,
            "adults": self.adults,
            "children": self.children,
            "guests": self.guests,
            "currency": self.currency,
            "price_per_night": money(self.price_per_night),
            "subtotal": money(self.subtotal),
            "discount": money(self.discount),
            "discount_source": self.discount_source,
            "guest_discount": (
                {
                    "id": self.guest_discount.pk,
                    "discount_type": self.guest_discount.discount_type,
                    "discount_value": money(self.guest_discount.discount_value),
                    "reason": self.guest_discount.reason,
                }
                if self.guest_discount
                else None
            ),
            "offer": (
                {
                    "id": self.offer.pk,
                    "title": self.offer.title,
                    "code": self.offer.code,
                    "discount_type": self.offer.discount_type,
                    "discount_value": money(self.offer.discount_value),
                }
                if self.offer
                else None
            ),
            "extra_guests": self.extra_guests,
            "extra_guest_fee": money(self.extra_guest_fee),
            "tax": money(self.tax),
            "service_fee": money(self.service_fee),
            "total": money(self.total),
            "required_payment": money(self.required_payment),
            "amount_due_online": money(self.required_payment),
        }


def validate_stay_dates(check_in, check_out, *, for_staff=False, settings_obj=None):
    """Hotel-wide date rules. Raises InvalidDatesError on violation."""
    from apps.core.utils import hotel_today

    settings_obj = settings_obj or HotelSettings.get_settings()
    if check_in is None or check_out is None:
        raise InvalidDatesError("Both check-in and check-out dates are required.")
    if check_out <= check_in:
        raise InvalidDatesError("Check-out date must be after the check-in date.")
    if not for_staff and check_in < hotel_today():
        raise InvalidDatesError("Check-in date cannot be in the past.")
    nights = (check_out - check_in).days
    if nights < settings_obj.min_stay_nights:
        raise InvalidDatesError(f"Minimum stay is {settings_obj.min_stay_nights} night(s).")
    if nights > settings_obj.max_stay_nights:
        raise InvalidDatesError(f"Maximum stay is {settings_obj.max_stay_nights} night(s).")
    return nights


def validate_capacity(room_type, rooms, guests):
    """Return number of extra (billable) guests or raise CapacityExceededError."""
    if guests < 1:
        raise CapacityExceededError("At least one guest is required.")
    base_capacity = room_type.max_guests * rooms
    if guests <= base_capacity:
        return 0
    overflow = guests - base_capacity
    if room_type.extra_guest_allowed and overflow <= rooms:
        return overflow  # up to one extra guest per room, charged
    raise CapacityExceededError(
        f"{room_type.name} accommodates {room_type.max_guests} guest(s) per room; "
        f"{rooms} room(s) cannot host {guests} guest(s)."
    )


def calculate_quote(*, room_type, check_in, check_out, rooms, adults, children,
                    offer_code=None, settings_obj=None, for_staff=False,
                    guest=None, offer_candidates=None) -> Quote:
    """Full server-side price computation for a stay."""
    settings_obj = settings_obj or HotelSettings.get_settings()
    nights = validate_stay_dates(check_in, check_out, for_staff=for_staff, settings_obj=settings_obj)
    guests = adults + children
    extra_guests = validate_capacity(room_type, rooms, guests)

    price_per_night = q(room_type.base_price)
    subtotal = q(price_per_night * nights * rooms)

    # ONE authoritative discount decision. A public offer and a personal guest
    # discount never stack: the backend applies whichever is larger and reports
    # which source won. An invalid promo code still raises here.
    offer, guest_discount, discount, discount_source = offer_services.best_discount_for_stay(
        guest=guest,
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        subtotal=subtotal,
        offer_code=offer_code,
        offer_candidates=offer_candidates,
    )

    extra_guest_fee = q((room_type.extra_guest_fee or Decimal("0.00")) * extra_guests * nights)
    taxable = q(subtotal - discount + extra_guest_fee)
    tax = q(taxable * (settings_obj.tax_rate_percent or Decimal("0.00")) / _HUNDRED)
    service_fee = q(settings_obj.service_fee or Decimal("0.00"))
    total = q(taxable + tax + service_fee)
    required = q(total * (settings_obj.deposit_percent or _HUNDRED) / _HUNDRED)

    return Quote(
        room_type_id=room_type.pk,
        check_in=check_in,
        check_out=check_out,
        rooms=rooms,
        adults=adults,
        children=children,
        guests=guests,
        nights=nights,
        currency=settings_obj.currency,
        price_per_night=price_per_night,
        subtotal=subtotal,
        offer=offer,
        guest_discount=guest_discount,
        discount_source=discount_source,
        discount=q(discount),
        extra_guests=extra_guests,
        extra_guest_fee=extra_guest_fee,
        tax=tax,
        service_fee=service_fee,
        total=total,
        required_payment=required,
    )
