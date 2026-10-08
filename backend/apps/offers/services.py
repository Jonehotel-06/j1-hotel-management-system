# apps/offers/services.py
"""Offer eligibility + discount maths.

The BACKEND decides whether an offer applies to a stay; the frontend only
displays what these functions return.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.core.exceptions import (
    OfferCodeInvalidError,
    OfferDoesNotCoverStayError,
    OfferExpiredError,
    OfferNotApplicableError,
    OfferNotStartedError,
    OfferRoomTypeNotEligibleError,
    OfferStayTooLongError,
    OfferStayTooShortError,
)

from .models import Offer

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def discount_amount(offer: Offer, subtotal: Decimal) -> Decimal:
    """Absolute discount for a given subtotal (never exceeding the subtotal)."""
    if offer.discount_type == Offer.DiscountType.PERCENTAGE:
        amount = subtotal * (Decimal(offer.discount_value) / Decimal("100"))
    else:
        amount = Decimal(offer.discount_value)
    return max(_ZERO, min(_q(amount), subtotal))


def eligible_offers_queryset(room_type, check_in, check_out, nights):
    """Offers whose window fully covers the stay and meet stay-length rules.

    Window rule: check_in must fall on/after start_date and the LAST NIGHT
    (check_out - 1 day) must fall on/before end_date.
    """
    from datetime import timedelta

    last_night = check_out - timedelta(days=1)
    qs = Offer.objects.filter(
        is_active=True,
        start_date__lte=check_in,
        end_date__gte=last_night,
        min_nights__lte=nights,
    )
    qs = qs.filter(models_q_max_nights(nights))
    # Empty M2M means "applies to all room types".
    qs = qs.filter(models_q_room_types(room_type))
    return qs.distinct()


def models_q_max_nights(nights):
    from django.db.models import Q

    return Q(max_nights__isnull=True) | Q(max_nights__gte=nights)


def models_q_room_types(room_type):
    from django.db.models import Q

    return Q(room_types__isnull=True) | Q(room_types=room_type)


def public_offer_candidates_for_room_types(*, room_types, check_in, check_out, nights):
    """Preload eligible public offers and partition them by room type.

    ``search_availability`` quotes every visible room type. Querying the normal
    room-type eligibility queryset inside each quote turns a single search into
    an offer-query N+1. This helper fetches valid public offers once, prefetches
    their M2M restrictions once, and returns ordered candidate lists. Empty
    ``room_types`` on an offer means it applies to every requested type.

    The explicit ordering is not cosmetic: :func:`best_offer` keeps the first
    candidate on equal discounts, matching the existing queryset behavior and
    ``Offer.Meta.ordering`` exactly.
    """
    room_type_ids = list(dict.fromkeys(
        getattr(room_type, "pk", room_type) for room_type in room_types
        if getattr(room_type, "pk", room_type)
    ))
    candidates = {room_type_id: [] for room_type_id in room_type_ids}
    if not room_type_ids:
        return candidates

    from datetime import timedelta

    last_night = check_out - timedelta(days=1)
    offers = (
        Offer.objects.filter(
            is_active=True,
            start_date__lte=check_in,
            end_date__gte=last_night,
            min_nights__lte=nights,
        )
        .filter(models_q_max_nights(nights))
        .prefetch_related("room_types")
        .order_by(*Offer._meta.ordering)
    )
    for offer in offers:
        allowed_ids = {room_type.pk for room_type in offer.room_types.all()}
        for room_type_id in room_type_ids:
            if not allowed_ids or room_type_id in allowed_ids:
                candidates[room_type_id].append(offer)
    return candidates


def best_offer(room_type, check_in, check_out, nights, subtotal, candidates=None):
    """Return (offer, discount) producing the largest discount, or (None, 0).

    ``candidates`` is an optional ordered list from
    :func:`public_offer_candidates_for_room_types`. The default remains the
    authoritative per-room-type eligibility queryset for quote/booking flows.
    """
    best, best_discount = None, _ZERO
    offers = candidates if candidates is not None else eligible_offers_queryset(
        room_type, check_in, check_out, nights
    )
    for offer in offers:
        amount = discount_amount(offer, subtotal)
        if amount > best_discount:
            best, best_discount = offer, amount
    return best, best_discount


def validate_offer_code(code, room_type, check_in, check_out, nights, subtotal):
    """Resolve a promo code to an ``(offer, discount)`` pair.

    Raises the SPECIFIC ``OFFER_*`` error describing why the code does not
    apply, so the guest is told what to change (stay longer, pick other dates,
    choose a different room type) rather than just "not valid".
    """
    from datetime import timedelta

    cleaned = (code or "").strip()
    # Match on the code alone first: an inactive/expired offer must report why
    # it was rejected, not masquerade as a typo.
    offer = Offer.objects.filter(code__iexact=cleaned).first()
    if offer is None:
        raise OfferCodeInvalidError(f"\u201c{cleaned}\u201d is not a valid offer code.")
    if not offer.is_active:
        raise OfferExpiredError("This offer is no longer available.")

    last_night = check_out - timedelta(days=1)
    if offer.end_date < check_in:
        raise OfferExpiredError(
            f"This offer ended on {offer.end_date:%d %b %Y}."
        )
    if offer.start_date > last_night:
        raise OfferNotStartedError(
            f"This offer starts on {offer.start_date:%d %b %Y}."
        )
    if offer.start_date > check_in or offer.end_date < last_night:
        raise OfferDoesNotCoverStayError(
            f"This offer only covers {offer.start_date:%d %b %Y} to "
            f"{offer.end_date:%d %b %Y}, which does not include your whole stay."
        )
    if nights < offer.min_nights:
        raise OfferStayTooShortError(
            f"This offer needs a minimum stay of {offer.min_nights} "
            f"night{'s' if offer.min_nights != 1 else ''}; your stay is {nights}."
        )
    if offer.max_nights is not None and nights > offer.max_nights:
        raise OfferStayTooLongError(
            f"This offer allows a maximum stay of {offer.max_nights} "
            f"night{'s' if offer.max_nights != 1 else ''}; your stay is {nights}."
        )
    allowed = offer.room_types.all()
    if allowed.exists() and room_type not in allowed:
        names = ", ".join(t.name for t in allowed)
        raise OfferRoomTypeNotEligibleError(
            f"This offer applies to {names}, not {room_type.name}."
        )

    # Defensive: the queryset is the single source of truth for eligibility.
    if not eligible_offers_queryset(room_type, check_in, check_out, nights).filter(pk=offer.pk).exists():
        raise OfferNotApplicableError("This offer cannot be applied to the selected stay.")
    return offer, discount_amount(offer, subtotal)


def offers_for_room_type(room_type, limit=5):
    """Public display helper: currently active offers usable for a room type."""
    today = timezone.localdate()
    qs = (
        Offer.objects.filter(is_active=True, start_date__lte=today, end_date__gte=today)
        .filter(models_q_room_types(room_type))
        .distinct()
        .order_by("-is_featured", "-discount_value")[:limit]
    )
    return [
        {
            "id": o.pk,
            "slug": o.slug,
            "title": o.title,
            "short_description": o.short_description,
            "discount_type": o.discount_type,
            "discount_value": f"{_q(o.discount_value)}",
            "end_date": o.end_date.isoformat(),
        }
        for o in qs
    ]


# ---------------------------------------------------------------------------
# Individual guest discounts
#
# PRECEDENCE RULE (single, deterministic, backend-only):
#   A public Offer and a personal GuestDiscount NEVER stack. The backend
#   computes both candidate discounts against the same subtotal and applies
#   whichever is LARGER for the guest; ties go to the public offer so a booking
#   keeps its advertised offer link. The applied source is reported to the
#   caller so staff and the guest can always see which one was used.
# ---------------------------------------------------------------------------
def guest_discount_amount(discount, subtotal: Decimal) -> Decimal:
    """Absolute discount for a subtotal (never more than the subtotal)."""
    from .models import GuestDiscount

    if discount.discount_type == GuestDiscount.DiscountType.PERCENTAGE:
        amount = subtotal * (Decimal(discount.discount_value) / Decimal("100"))
    else:
        amount = Decimal(discount.discount_value)
    return max(_ZERO, min(_q(amount), subtotal))


def active_guest_discount(guest, on_date=None):
    """The best currently-valid personal discount for a guest, or None.

    ``guest`` may be a Guest instance or None (guest-checkout before the row
    exists). Validity is judged against ``on_date`` (defaults to today) so a
    discount that has not started or has expired is never applied.
    """
    from .models import GuestDiscount

    if guest is None or getattr(guest, "pk", None) is None:
        return None
    day = on_date or timezone.localdate()
    qs = GuestDiscount.objects.filter(
        guest=guest,
        is_active=True,
        start_date__lte=day,
    ).filter(Q_end_date_open(day))
    # Deterministic pick: the largest percentage/amount, newest first on ties.
    return qs.order_by("-discount_value", "-created_at").first()


def Q_end_date_open(day):
    from django.db.models import Q

    return Q(end_date__isnull=True) | Q(end_date__gte=day)


def best_discount_for_stay(*, guest, room_type, check_in, check_out, nights,
                           subtotal, offer_code=None, offer_candidates=None):
    """THE authoritative discount decision for a stay.

    Returns ``(offer, guest_discount, amount, source)`` where ``source`` is one
    of ``"OFFER"``, ``"GUEST_DISCOUNT"`` or ``""`` (no discount).

    * An explicit promo code always resolves the public offer (and raises when
      it is not applicable) — but a bigger personal discount still wins.
    * Otherwise the best eligible public offer competes with the guest's
      personal discount and the larger of the two is applied. They never stack.
    * ``offer_candidates`` is an internal optimization for anonymous public
      availability search only; explicit promo-code validation always uses the
      authoritative lookup path.
    """
    if offer_code:
        offer, offer_discount = validate_offer_code(
            offer_code, room_type, check_in, check_out, nights, subtotal
        )
    else:
        offer, offer_discount = best_offer(
            room_type, check_in, check_out, nights, subtotal,
            candidates=offer_candidates,
        )

    discount = active_guest_discount(guest, on_date=check_in)
    personal_amount = guest_discount_amount(discount, subtotal) if discount else _ZERO

    # Larger wins; ties keep the public offer so the booking retains its link.
    if personal_amount > offer_discount:
        return None, discount, personal_amount, "GUEST_DISCOUNT"
    if offer is not None and offer_discount > _ZERO:
        return offer, None, offer_discount, "OFFER"
    return None, None, _ZERO, ""
