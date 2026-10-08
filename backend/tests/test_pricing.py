# tests/test_pricing.py
"""PRICING ENGINE tests (spec §26–§27, §30): nights, offers, taxes, fees,
deposit rules, quote endpoint integrity."""
from datetime import timedelta
from decimal import Decimal

from apps.bookings.services.pricing import calculate_quote
from apps.core.utils import hotel_today
from apps.offers.models import Offer
from apps.offers.services import public_offer_candidates_for_room_types
from apps.rooms.models import RoomType

from .base import BaseAPITestCase
from .factories import hotel_settings, make_room, make_room_type


class PricingEngineTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="25000.00", max_guests=2)
        make_room(self.room_type, "201")
        self.today = hotel_today()
        self.ci = self.today + timedelta(days=5)
        self.co = self.today + timedelta(days=8)  # 3 nights

    def test_base_quote_math(self):
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=2, adults=4, children=0,
        )
        self.assertEqual(quote.nights, 3)
        self.assertEqual(quote.subtotal, Decimal("150000.00"))
        self.assertEqual(quote.discount, Decimal("0.00"))
        self.assertEqual(quote.total, Decimal("150000.00"))
        self.assertEqual(quote.required_payment, Decimal("150000.00"))  # deposit 100% default

    def test_percentage_offer_applies(self):
        Offer.objects.create(
            title="Ten off", discount_type=Offer.DiscountType.PERCENTAGE, discount_value=Decimal("10"),
            start_date=self.today, end_date=self.today + timedelta(days=60),
        )
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        self.assertEqual(quote.subtotal, Decimal("75000.00"))
        self.assertEqual(quote.discount, Decimal("7500.00"))
        self.assertEqual(quote.total, Decimal("67500.00"))

    def test_best_offer_wins_and_fixed_amount_capped(self):
        Offer.objects.create(
            title="Big fixed", discount_type=Offer.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("999999"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        Offer.objects.create(
            title="Small percent", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("10"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        # Fixed discount capped at the subtotal: entirely free stay wins.
        self.assertLessEqual(quote.discount, quote.subtotal)
        self.assertEqual(quote.offer.title, "Big fixed")

    def test_offer_minimum_nights_enforced(self):
        Offer.objects.create(
            title="Long stay only", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("50"), start_date=self.today,
            end_date=self.today + timedelta(days=60), min_nights=5,
        )
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        self.assertEqual(quote.discount, Decimal("0.00"))

    def test_offer_room_type_restriction(self):
        other_type = make_room_type("Other", price="10000.00")
        offer = Offer.objects.create(
            title="Other-only", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("50"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        offer.room_types.add(other_type)
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        self.assertEqual(quote.discount, Decimal("0.00"))

    def test_preloaded_public_offer_candidates_match_authoritative_quote_and_ties(self):
        other_type = make_room_type("Candidate other", price="25000.00")
        first = Offer.objects.create(
            title="A tie first", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("10"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        Offer.objects.create(
            title="Z tie second", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("10"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        restricted = Offer.objects.create(
            title="Other only", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("90"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        restricted.room_types.add(other_type)

        authoritative = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        candidates = public_offer_candidates_for_room_types(
            room_types=[self.room_type, other_type],
            check_in=self.ci, check_out=self.co, nights=3,
        )
        batched = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
            offer_candidates=candidates[self.room_type.pk],
        )

        self.assertEqual(authoritative.offer.pk, first.pk)
        self.assertEqual(batched.offer.pk, authoritative.offer.pk)
        self.assertEqual(batched.discount, authoritative.discount)
        self.assertEqual(batched.total, authoritative.total)
        self.assertNotIn(restricted, candidates[self.room_type.pk])
        self.assertIn(restricted, candidates[other_type.pk])

    def test_promo_code_validation(self):
        offer = Offer.objects.create(
            title="Code ten", code="SAVE10", discount_type=Offer.DiscountType.PERCENTAGE,
            discount_value=Decimal("10"), start_date=self.today,
            end_date=self.today + timedelta(days=60),
        )
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0, offer_code="save10",
        )
        self.assertEqual(quote.offer.pk, offer.pk)
        self.assertEqual(quote.discount, Decimal("7500.00"))

    def test_invalid_promo_code_raises(self):
        from apps.core.exceptions import OfferNotApplicableError

        with self.assertRaises(OfferNotApplicableError):
            calculate_quote(
                room_type=self.room_type, check_in=self.ci, check_out=self.co,
                rooms=1, adults=2, children=0, offer_code="NOPE",
            )

    def test_tax_and_service_fee_math(self):
        hotel_settings(tax_rate_percent=Decimal("7.50"), service_fee=Decimal("2000.00"))
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        self.assertEqual(quote.tax, Decimal("5625.00"))  # 75000 * 7.5%
        self.assertEqual(quote.service_fee, Decimal("2000.00"))
        self.assertEqual(quote.total, Decimal("75000.00") + Decimal("5625.00") + Decimal("2000.00"))

    def test_partial_deposit_required_payment(self):
        hotel_settings(deposit_percent=Decimal("50"))
        quote = calculate_quote(
            room_type=self.room_type, check_in=self.ci, check_out=self.co,
            rooms=1, adults=2, children=0,
        )
        self.assertEqual(quote.total, Decimal("75000.00"))
        self.assertEqual(quote.required_payment, Decimal("37500.00"))

    def test_extra_guest_fee(self):
        suite = make_room_type("Suite", price="40000.00", max_guests=2,
                               extra_guest_allowed=True, extra_guest_fee=Decimal("5000.00"))
        make_room(suite, "301")
        quote = calculate_quote(
            room_type=suite, check_in=self.ci, check_out=self.co,
            rooms=1, adults=3, children=0,
        )
        self.assertEqual(quote.extra_guests, 1)
        self.assertEqual(quote.extra_guest_fee, Decimal("15000.00"))  # 5000 x 3 nights
        self.assertEqual(quote.total, quote.subtotal + Decimal("15000.00"))

    def test_capacity_exceeded_raises(self):
        from apps.core.exceptions import CapacityExceededError

        room_type = make_room_type("Tiny", price="1000.00", max_guests=1)
        with self.assertRaises(CapacityExceededError):
            calculate_quote(
                room_type=room_type, check_in=self.ci, check_out=self.co,
                rooms=1, adults=3, children=0,
            )

    def test_stay_length_rules(self):
        from apps.core.exceptions import InvalidDatesError

        hotel_settings(min_stay_nights=2, max_stay_nights=4)
        with self.assertRaises(InvalidDatesError):
            calculate_quote(room_type=self.room_type, check_in=self.ci,
                            check_out=self.ci + timedelta(days=1), rooms=1, adults=2, children=0)
        with self.assertRaises(InvalidDatesError):
            calculate_quote(room_type=self.room_type, check_in=self.ci,
                            check_out=self.ci + timedelta(days=5), rooms=1, adults=2, children=0)


class QuoteEndpointTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room_type = make_room_type("Deluxe", price="25000.00", max_guests=2)
        make_room(self.room_type, "201")
        self.today = hotel_today()

    def test_quote_endpoint_returns_authoritative_breakdown(self):
        response = self.client.post("/api/bookings/quote/", {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
            "rooms": 1, "adults": 2, "children": 0,
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["nights"], 2)
        self.assertEqual(data["subtotal"], "50000.00")
        self.assertEqual(data["total"], "50000.00")
        self.assertEqual(data["required_payment"], "50000.00")
        self.assertIn("policies", data)
        # Money travels as strings (exact Decimal representation).
        self.assertIsInstance(data["total"], str)

    def test_quote_does_not_persist_anything(self):
        from apps.bookings.models import Booking

        self.client.post("/api/bookings/quote/", {
            "room_type": self.room_type.slug,
            "check_in": (self.today + timedelta(days=5)).isoformat(),
            "check_out": (self.today + timedelta(days=7)).isoformat(),
        })
        self.assertEqual(Booking.objects.count(), 0)
